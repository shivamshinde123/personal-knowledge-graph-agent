"""LLM provider layer: the abstraction every LLM call in the system goes through.

``ProviderInterface`` is the sole contract every LangGraph node and pipeline
stage talks to; ``local_provider`` (Ollama) and ``openrouter_provider`` each
construct a provider satisfying it. No other layer may import a concrete
provider, a LangChain chat model, or an LLM SDK directly — see
``docs/Component_Map.docx`` and the "Never bypass the LLM Provider
abstraction" ground rule in ``CLAUDE.md``.

Both concrete providers are backed by a shared LangChain chat model adapter
(``LangChainProvider``) rather than separate hand-rolled implementations,
since Ollama and OpenRouter are used identically here — only which chat
model backs the calls differs. See ``DECISIONS.md`` for the reasoning.

Typical use::

    from providers.base import get_provider

    provider = get_provider("answer")
    answer = provider.generate_answer(question, context_chunks)
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel

from config.settings import get_settings

logger = logging.getLogger(__name__)

Task = Literal["metadata", "relationship", "answer"]

_MAX_RETRIES = 3
_BASE_DELAY_SECONDS = 1.0

# Fixed vocabulary for RelationshipJudgment.label — see DECISIONS.md,
# 2026-08-24, for why this replaced free-form label generation.
_RELATIONSHIP_LABELS = ("implements", "discussed_in", "planned_in", "companion_to")


class ProviderError(Exception):
    """Raised when an LLM provider call fails after retries are exhausted."""


@dataclass(slots=True)
class ItemMetadata:
    """LLM-derived metadata for one piece of ingested content."""

    project_name: str | None
    topic: str | None


@dataclass(slots=True)
class RelationshipJudgment:
    """A confirmed relationship between two candidate items."""

    label: str
    confidence: float | None = None


@dataclass(slots=True)
class ContextChunk:
    """One retrieved chunk, with the metadata needed to cite it."""

    text: str
    source_type: str
    title: str | None = None
    url: str | None = None


class ProviderInterface(ABC):
    """The shared contract every concrete LLM provider implements."""

    @abstractmethod
    def generate_metadata(self, texts: Sequence[str]) -> list[ItemMetadata]:
        """Derive ``project_name``/``topic`` for each of a batch of texts.

        Args:
            texts: The texts to classify, in the order results are returned.

        Returns:
            One :class:`ItemMetadata` per input text, same order, same length.

        Raises:
            ProviderError: If the call fails after retries are exhausted.
        """

    @abstractmethod
    def generate_relationship(
        self, source_text: str, candidate_text: str
    ) -> RelationshipJudgment | None:
        """Confirm or reject a vector-narrowed relationship candidate.

        Args:
            source_text: The text of the item being related from.
            candidate_text: The text of the candidate item being related to.

        Returns:
            A judgment if the model confirms a relationship, else ``None``.

        Raises:
            ProviderError: If the call fails after retries are exhausted.
        """

    @abstractmethod
    def generate_answer(self, question: str, context: Sequence[ContextChunk]) -> str:
        """Synthesize an answer to a question from retrieved context.

        Args:
            question: The user's natural language question.
            context: Retrieved chunks to ground the answer in, ordered by
                relevance. The response cites them as ``[1]``, ``[2]``, …,
                matching this order — callers resolve those markers to
                ``context``'s ``title``/``url`` fields.

        Returns:
            The generated answer text.

        Raises:
            ProviderError: If the call fails after retries are exhausted.
        """


def _retry_with_backoff[T](call: Callable[[], T], *, provider_name: str) -> T:
    """Retry a provider call with exponential backoff, wrapping failures.

    Args:
        call: A zero-argument callable performing one attempt. Anything it
            raises — a transport error, a malformed/unparseable response —
            is treated as retryable.
        provider_name: Identifies the provider/model in the raised error.

    Returns:
        The result of the first successful attempt.

    Raises:
        ProviderError: If every attempt fails.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return call()
        except Exception as exc:
            last_exc = exc
            if attempt == _MAX_RETRIES:
                break
            delay = _BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "%s call failed (attempt %d/%d), retrying in %.1fs: %s",
                provider_name,
                attempt + 1,
                _MAX_RETRIES + 1,
                delay,
                exc,
            )
            time.sleep(delay)
    raise ProviderError(
        f"{provider_name} call failed after {_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc


def _build_metadata_prompt(texts: Sequence[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(texts))
    return (
        "For each numbered text below, identify its project name and topic "
        "if apparent from the content; use null for either field if it "
        "cannot be determined from the text alone. Respond with ONLY a JSON "
        f"array of exactly {len(texts)} objects, in the same order, each "
        'shaped {"project_name": string | null, "topic": string | null}. '
        f"No other text.\n\n{numbered}"
    )


def _parse_metadata_response(raw: str, expected_count: int) -> list[ItemMetadata]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or len(parsed) != expected_count:
        raise ValueError(
            f"Expected a JSON array of {expected_count} objects, got: {raw!r}"
        )
    return [
        ItemMetadata(project_name=entry.get("project_name"), topic=entry.get("topic"))
        for entry in parsed
    ]


def _build_relationship_prompt(source_text: str, candidate_text: str) -> str:
    labels = ", ".join(f'"{label}"' for label in _RELATIONSHIP_LABELS)
    return (
        "These two texts were surfaced by a similarity search, which means "
        "they may merely share vocabulary, formatting, or topic area "
        "without being meaningfully related — most candidate pairs found "
        "this way are NOT actually related. Default to unrelated unless "
        "you can point to a specific, concrete connection between what "
        "Text 2 actually says and what Text 1 actually says.\n\n"
        "If related, choose exactly one label:\n"
        '- "implements": Text 2 is a concrete realization of something '
        "specifically planned or designed in Text 1 (or vice versa).\n"
        '- "discussed_in": Text 2 explicitly discusses the same specific '
        "subject matter as Text 1 — not merely a related theme or shared "
        "domain vocabulary.\n"
        '- "planned_in": Text 1 or Text 2 proposes or plans something the '
        "other one carries out or refers back to.\n"
        '- "companion_to": the two are explicitly designated as a paired '
        "or companion document (e.g. one names the other by title as its "
        "companion) — not just two documents from the same project.\n\n"
        "Respond with ONLY JSON: if related, "
        '{"related": true, "label": one of '
        f'[{labels}], "confidence": number between 0 and 1 reflecting how '
        "certain you are}; if not related, or if you are unsure, "
        '{"related": false}. No other text.\n\n'
        f"Text 1:\n{source_text}\n\nText 2:\n{candidate_text}"
    )


def _parse_relationship_response(raw: str) -> RelationshipJudgment | None:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or "related" not in parsed:
        raise ValueError(f"Expected a JSON object with 'related', got: {raw!r}")
    if not parsed["related"]:
        return None
    label = parsed.get("label")
    if label not in _RELATIONSHIP_LABELS:
        raise ValueError(f"Related judgment has an unrecognized label: {raw!r}")
    confidence = parsed.get("confidence")
    return RelationshipJudgment(
        label=label, confidence=None if confidence is None else float(confidence)
    )


def _build_answer_prompt(question: str, context: Sequence[ContextChunk]) -> str:
    numbered = "\n\n".join(
        f"[{i + 1}] ({chunk.source_type}) {chunk.text}"
        for i, chunk in enumerate(context)
    )
    return (
        "Answer the question using only the numbered context below. Cite "
        "the context you use inline with its number in brackets, e.g. [1]. "
        "If the context doesn't answer the question, say so.\n\n"
        f"Context:\n{numbered}\n\nQuestion: {question}"
    )


class LangChainProvider(ProviderInterface):
    """A provider backed by any LangChain chat model.

    Both concrete providers construct one of these rather than reimplementing
    the interface — see the module docstring.
    """

    def __init__(self, chat_model: BaseChatModel, *, provider_name: str) -> None:
        """Initialize the provider.

        Args:
            chat_model: The LangChain chat model to route calls through.
            provider_name: Identifies this provider/model in error messages
                and retry logs (e.g. ``"ollama:llama3:8b"``).
        """
        self._chat_model = chat_model
        self._provider_name = provider_name

    def generate_metadata(self, texts: Sequence[str]) -> list[ItemMetadata]:
        """See :meth:`ProviderInterface.generate_metadata`."""
        if not texts:
            return []
        prompt = _build_metadata_prompt(texts)

        def call() -> list[ItemMetadata]:
            response = self._chat_model.invoke(prompt)
            return _parse_metadata_response(str(response.content), len(texts))

        return _retry_with_backoff(call, provider_name=self._provider_name)

    def generate_relationship(
        self, source_text: str, candidate_text: str
    ) -> RelationshipJudgment | None:
        """See :meth:`ProviderInterface.generate_relationship`."""
        prompt = _build_relationship_prompt(source_text, candidate_text)

        def call() -> RelationshipJudgment | None:
            response = self._chat_model.invoke(prompt)
            return _parse_relationship_response(str(response.content))

        return _retry_with_backoff(call, provider_name=self._provider_name)

    def generate_answer(self, question: str, context: Sequence[ContextChunk]) -> str:
        """See :meth:`ProviderInterface.generate_answer`."""
        prompt = _build_answer_prompt(question, context)

        def call() -> str:
            response = self._chat_model.invoke(prompt)
            return str(response.content)

        return _retry_with_backoff(call, provider_name=self._provider_name)


def get_provider(task: Task) -> ProviderInterface:
    """Select the configured provider for a given task.

    Reads ``settings.config.llm.provider_mode``:

    - ``fully_local``: every task runs through Ollama.
    - ``fully_cloud``: every task runs through OpenRouter.
    - ``mixed``: the low-frequency, quality-sensitive ``"answer"`` task runs
      through OpenRouter; the cheaper, high-frequency ``"metadata"`` and
      ``"relationship"`` ingestion tasks run through Ollama.

    Args:
        task: Which kind of call the returned provider will be used for.

    Returns:
        A configured provider instance.

    Raises:
        ProviderError: If the resolved provider is misconfigured (e.g. no
            OpenRouter API key set while cloud access is required).
    """
    from providers.local_provider import create_local_provider
    from providers.openrouter_provider import create_openrouter_provider

    mode = get_settings().config.llm.provider_mode
    if mode == "fully_local":
        return create_local_provider()
    if mode == "fully_cloud":
        return create_openrouter_provider()
    return create_openrouter_provider() if task == "answer" else create_local_provider()
