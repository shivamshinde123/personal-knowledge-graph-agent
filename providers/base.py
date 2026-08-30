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
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel

from config.settings import get_settings

logger = logging.getLogger(__name__)

Task = Literal["metadata", "relationship", "answer", "condense", "eval", "embedding"]
EvalCriterion = Literal["faithfulness", "relevance"]

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


@dataclass(slots=True)
class EvalJudgment:
    """An LLM-as-judge score for one evaluation criterion, in ``eval/``.

    Not used on the query path — only by ``eval/evaluators.py``'s
    faithfulness/relevance scorers.
    """

    score: float
    reasoning: str


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One prior turn in the conversation, for follow-up questions.

    Passed to :meth:`ProviderInterface.generate_answer` so the model can
    resolve references like "the second one" or "that" in the current
    question — not itself part of the cited context.
    """

    role: Literal["user", "agent"]
    text: str


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
    def generate_answer(
        self,
        question: str,
        context: Sequence[ContextChunk],
        history: Sequence[ConversationTurn] = (),
    ) -> str:
        """Synthesize an answer to a question from retrieved context.

        Args:
            question: The user's natural language question.
            context: Retrieved chunks to ground the answer in, ordered by
                relevance. The response cites them as ``[1]``, ``[2]``, …,
                matching this order — callers resolve those markers to
                ``context``'s ``title``/``url`` fields.
            history: Prior turns in this conversation, oldest first, for
                resolving follow-up references — not itself cited.

        Returns:
            The generated answer text.

        Raises:
            ProviderError: If the call fails after retries are exhausted.
        """

    @abstractmethod
    def generate_search_query(
        self, question: str, history: Sequence[ConversationTurn]
    ) -> str:
        """Rewrite a follow-up question into a standalone search query.

        Retrieval (the Query Router and the search nodes) only ever sees
        the current question's raw text, so a vague follow-up like "why was
        the second one chosen?" has no topical keywords of its own to
        retrieve on. This resolves references like "it", "that", or "the
        second one" against the prior conversation, producing a
        self-contained query for retrieval to run instead — the original
        ``question`` is still what's shown to the user and passed to
        :meth:`generate_answer`. See ``DECISIONS.md``.

        Args:
            question: The user's natural language question.
            history: Prior turns in this conversation, oldest first. Never
                called with empty history — see ``agent/graph.py``.

        Returns:
            A standalone rewrite of ``question``, suitable for retrieval.

        Raises:
            ProviderError: If the call fails after retries are exhausted.
        """

    @abstractmethod
    def generate_eval_judgment(
        self,
        criterion: EvalCriterion,
        question: str,
        answer: str,
        context: Sequence[str],
    ) -> EvalJudgment:
        """Score one answer against one evaluation criterion, LLM-as-judge.

        Used only by ``eval/evaluators.py`` — never on the query path. Not
        every argument is relevant to every criterion (``"faithfulness"``
        doesn't need ``question``; ``"relevance"`` doesn't need
        ``context``), but all three are always passed for a uniform
        signature; the prompt uses only what each criterion needs.

        Args:
            criterion: Which quality dimension to score —
                ``"faithfulness"`` (is everything in ``answer`` actually
                supported by ``context``, or does it hallucinate) or
                ``"relevance"`` (does ``answer`` actually address
                ``question``).
            question: The question that was asked.
            answer: The agent's synthesized answer.
            context: The retrieved chunk texts the answer was grounded in.

        Returns:
            A score in ``[0.0, 1.0]`` (higher is better) with a short
            explanation.

        Raises:
            ProviderError: If the call fails after retries are exhausted.
        """

    @abstractmethod
    def generate_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors.

        Unlike generation, embedding does not follow ``provider_mode`` —
        ``get_provider("embedding")`` always resolves to OpenRouter,
        regardless of mode (see ``get_provider()``'s own docstring). There
        is only one embedding model in play at any time
        (``settings.config.llm.cloud_embedding_model``), user-editable —
        so a mode switch can never mismatch vectors the way switching
        between two separate models could, but changing the embedding
        model itself still can; the frontend confirms with the user and
        triggers a reset + re-ingest when it changes. Requires
        ``OPENROUTER_API_KEY`` even under ``fully_local``.

        Args:
            texts: The texts to embed, in the order results are returned.

        Returns:
            One embedding vector per input text, same order, same length.

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
        "Before judging, first identify any sentence, header, or phrase "
        "that appears verbatim (or near-verbatim) in both texts — shared "
        "boilerplate like a title block, a footer, or a templated line "
        "(e.g. both texts happening to contain the same 'Companion "
        "document to: <X>' line) is NOT evidence of a relationship between "
        "Text 1 and Text 2 themselves, even if that shared line names a "
        "companion or reference — it only counts if the line specifically "
        "names Text 1 or Text 2 as the OTHER text's companion/reference, "
        "not some third document, and the line is not identical, "
        "boilerplate-style text repeated across otherwise-unrelated items. "
        "Base your judgment only on what is unique to each text once any "
        "such shared boilerplate is set aside.\n\n"
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


_FLAT_JSON_OBJECT = re.compile(r"\{[^{}]*\}")


def _extract_json_object(raw: str, required_key: str) -> str:
    """Pull a flat JSON object out of a response that may reason before it.

    The relationship prompt asks the model to first name any shared
    boilerplate before judging (see DECISIONS.md, 2026-08-29), and models
    reliably comply by explaining their reasoning ahead of the JSON rather
    than emitting ONLY JSON as instructed — so the raw response is not
    reliably pure JSON on its own. The eval-judgment prompts (see
    ``eval/evaluators.py``) invite similar reasoning-before-scoring. Scans
    for every brace-balanced `{...}` substring and returns the last one
    that's valid JSON containing ``required_key``, since the judgment is
    always the final thing emitted. Falls back to returning ``raw``
    unchanged if nothing matches, so the caller's own ``json.loads`` still
    raises a clear, familiar error.

    Args:
        raw: The model's raw response text.
        required_key: The key the real judgment object must contain (e.g.
            ``"related"``, ``"score"``) — distinguishes it from any other
            brace-balanced substring that happens to appear in the
            reasoning text.

    Returns:
        The extracted JSON object substring, or ``raw`` if none was found.
    """
    for candidate in reversed(_FLAT_JSON_OBJECT.findall(raw)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and required_key in parsed:
            return candidate
    return raw


def _parse_relationship_response(raw: str) -> RelationshipJudgment | None:
    parsed = json.loads(_extract_json_object(raw, required_key="related"))
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


def _build_answer_prompt(
    question: str,
    context: Sequence[ContextChunk],
    history: Sequence[ConversationTurn] = (),
) -> str:
    numbered = "\n\n".join(
        f"[{i + 1}] ({chunk.source_type}) {chunk.text}"
        for i, chunk in enumerate(context)
    )
    history_section = ""
    if history:
        transcript = "\n".join(
            f"{turn.role.capitalize()}: {turn.text}" for turn in history
        )
        history_section = (
            "Prior conversation, for resolving references like 'the second "
            "one' or 'that' in the current question — not itself part of "
            f"the citable context:\n{transcript}\n\n"
        )
    return (
        f"{history_section}"
        "Answer the question using only the numbered context below. Cite "
        "the context you use inline with its number in brackets, e.g. [1]. "
        "If the context doesn't answer the question, say so.\n\n"
        f"Context:\n{numbered}\n\nQuestion: {question}"
    )


def _build_condense_prompt(question: str, history: Sequence[ConversationTurn]) -> str:
    transcript = "\n".join(f"{turn.role.capitalize()}: {turn.text}" for turn in history)
    return (
        "Rewrite the follow-up question below as a standalone search query, "
        "using the prior conversation to resolve references like 'it', "
        "'that', or 'the second one' into the specific thing(s) they refer "
        "to. Keep it a single question or phrase capturing what the "
        "follow-up is actually asking about. Respond with ONLY the "
        "rewritten query text — no quotes, no explanation, no other "
        "text.\n\n"
        f"Prior conversation:\n{transcript}\n\nFollow-up question: {question}"
    )


_EVAL_CRITERION_INSTRUCTIONS: dict[EvalCriterion, str] = {
    "faithfulness": (
        "Score how well the ANSWER is supported by the CONTEXT alone. "
        "Every specific claim, fact, or detail in the answer must be "
        "traceable to something actually stated in the context — not "
        "something plausible-sounding, not general knowledge, not an "
        "inference the context doesn't support. 1.0 means every claim is "
        "directly grounded in the context; 0.0 means the answer states "
        "significant details the context never mentions (hallucination). "
        "The QUESTION is given only for background; do not score whether "
        "the answer addresses it — that's a separate criterion."
    ),
    "relevance": (
        "Score how directly the ANSWER addresses the QUESTION — on-topic, "
        "and covering what was actually asked, not a tangent or a partial "
        "answer to a different question. 1.0 means it fully addresses the "
        "question; 0.0 means it doesn't address the question at all. The "
        "CONTEXT is given only for background; do not score factual "
        "accuracy against it — that's a separate criterion."
    ),
}


def _build_eval_prompt(
    criterion: EvalCriterion, question: str, answer: str, context: Sequence[str]
) -> str:
    context_text = "\n\n".join(context) if context else "(no context retrieved)"
    return (
        f"{_EVAL_CRITERION_INSTRUCTIONS[criterion]}\n\n"
        "Respond with ONLY JSON: "
        '{"score": number between 0 and 1, "reasoning": short explanation}. '
        "No other text.\n\n"
        f"Question: {question}\n\nContext:\n{context_text}\n\nAnswer: {answer}"
    )


def _parse_eval_response(raw: str) -> EvalJudgment:
    parsed = json.loads(_extract_json_object(raw, required_key="score"))
    if not isinstance(parsed, dict) or "score" not in parsed:
        raise ValueError(f"Expected a JSON object with 'score', got: {raw!r}")
    score = float(parsed["score"])
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"Eval score out of the [0, 1] range: {raw!r}")
    return EvalJudgment(score=score, reasoning=str(parsed.get("reasoning", "")))


class LangChainProvider(ProviderInterface):
    """A provider backed by any LangChain chat model.

    Both concrete providers construct one of these rather than reimplementing
    the interface — see the module docstring.
    """

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        provider_name: str,
        embed_fn: Callable[[Sequence[str]], list[list[float]]] | None = None,
    ) -> None:
        """Initialize the provider.

        Args:
            chat_model: The LangChain chat model to route calls through.
            provider_name: Identifies this provider/model in error messages
                and retry logs (e.g. ``"ollama:llama3:8b"``).
            embed_fn: Embeds a batch of texts, backing
                :meth:`generate_embeddings`. Not a LangChain ``Embeddings``
                object directly — ``create_local_provider()`` wraps
                ``sentence-transformers``, ``create_openrouter_provider()``
                wraps a LangChain ``OpenAIEmbeddings`` — so this stays a
                plain callable rather than committing to one shape.
                ``None`` for a provider that never embeds (e.g. tests
                exercising only the chat-model methods).
        """
        self._chat_model = chat_model
        self._provider_name = provider_name
        self._embed_fn = embed_fn

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

    def generate_answer(
        self,
        question: str,
        context: Sequence[ContextChunk],
        history: Sequence[ConversationTurn] = (),
    ) -> str:
        """See :meth:`ProviderInterface.generate_answer`."""
        prompt = _build_answer_prompt(question, context, history)

        def call() -> str:
            response = self._chat_model.invoke(prompt)
            return str(response.content)

        return _retry_with_backoff(call, provider_name=self._provider_name)

    def generate_search_query(
        self, question: str, history: Sequence[ConversationTurn]
    ) -> str:
        """See :meth:`ProviderInterface.generate_search_query`."""
        prompt = _build_condense_prompt(question, history)

        def call() -> str:
            response = self._chat_model.invoke(prompt)
            rewritten = str(response.content).strip()
            if not rewritten:
                raise ValueError("Condensed query response was empty")
            return rewritten

        return _retry_with_backoff(call, provider_name=self._provider_name)

    def generate_eval_judgment(
        self,
        criterion: EvalCriterion,
        question: str,
        answer: str,
        context: Sequence[str],
    ) -> EvalJudgment:
        """See :meth:`ProviderInterface.generate_eval_judgment`."""
        prompt = _build_eval_prompt(criterion, question, answer, context)

        def call() -> EvalJudgment:
            response = self._chat_model.invoke(prompt)
            return _parse_eval_response(str(response.content))

        return _retry_with_backoff(call, provider_name=self._provider_name)

    def generate_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        """See :meth:`ProviderInterface.generate_embeddings`."""
        if not texts:
            return []
        if self._embed_fn is None:
            raise ProviderError(
                f"{self._provider_name} was constructed without an embedding function"
            )

        def call() -> list[list[float]]:
            return self._embed_fn(texts)

        return _retry_with_backoff(call, provider_name=self._provider_name)


def get_provider(task: Task) -> ProviderInterface:
    """Select the configured provider for a given task.

    ``"embedding"`` always resolves to OpenRouter, regardless of
    ``provider_mode`` — there is no local embedding path any more (removed
    — see DECISIONS.md). This means embedding calls need
    ``OPENROUTER_API_KEY`` configured even under ``fully_local``, which is
    otherwise meant to have no network dependency; ingestion (which always
    embeds) will fail for every item under `fully_local` without that key
    set. This is a deliberate trade-off, not an oversight — a single,
    cloud-only embedding model can never have a mode-driven dimension
    mismatch to worry about at all, which was worth more than preserving
    `fully_local`'s previous "zero network calls" property. See
    DECISIONS.md.

    Every other task reads ``settings.config.llm.provider_mode`` —
    ``fully_local``: generation runs through Ollama. ``fully_cloud``:
    generation runs through OpenRouter. There is no ``mixed`` mode
    (removed — see DECISIONS.md).

    Args:
        task: Which kind of call the returned provider will be used for.

    Returns:
        A configured provider instance.

    Raises:
        ProviderError: If the resolved provider is misconfigured (e.g. no
            OpenRouter API key set while cloud access is required — always
            required for ``"embedding"``, only required for other tasks
            under ``fully_cloud``).
    """
    from providers.local_provider import create_local_provider
    from providers.openrouter_provider import create_openrouter_provider

    if task == "embedding":
        return create_openrouter_provider()
    mode = get_settings().config.llm.provider_mode
    if mode == "fully_cloud":
        return create_openrouter_provider()
    return create_local_provider()
