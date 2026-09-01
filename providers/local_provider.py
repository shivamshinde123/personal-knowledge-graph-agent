"""Ollama-backed provider: the local half of the dual provider setup.

Used when ``provider_mode: fully_local``, for *both* generation and
embedding — Ollama only, never any other local backend (e.g. not
``sentence-transformers``, which was the original local-embedding path
historically but has since been dropped from the project entirely — see
DECISIONS.md). This is a deliberate re-reversal of an earlier decision to
make embedding always go through OpenRouter regardless of
``provider_mode``; see
``providers/base.py::get_provider()`` and ``DECISIONS.md`` for the full
history and the cost-driven reasoning behind bringing local embedding back.
See ``providers/base.py`` for the shared contract and implementation.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_ollama import ChatOllama, OllamaEmbeddings

from config.settings import get_settings
from providers.base import LangChainProvider, ProviderInterface


def _make_embed_fn(model_name: str, base_url: str):
    # OllamaEmbeddings, not a hand-rolled httpx call — mirrors the
    # "go through LangChain" pattern create_openrouter_provider() already
    # uses for its own embeddings. See DECISIONS.md.
    embeddings = OllamaEmbeddings(model=model_name, base_url=base_url)

    def embed(texts: Sequence[str]) -> list[list[float]]:
        return embeddings.embed_documents(list(texts))

    return embed


def create_local_provider(
    model: str | None = None, *, embedding_model: str | None = None
) -> ProviderInterface:
    """Build a provider backed by locally running Ollama models.

    Args:
        model: The generation model tag to use (e.g. ``"llama3:8b"``).
            Defaults to ``settings.config.llm.local_generation_model``.
        embedding_model: The embedding model tag to use (e.g.
            ``"nomic-embed-text"``). Defaults to
            ``settings.config.llm.local_embedding_model``.

    Returns:
        A provider that routes every generation and embedding call through
        Ollama.
    """
    settings = get_settings()
    resolved_model = model or settings.config.llm.local_generation_model
    resolved_embedding_model = (
        embedding_model or settings.config.llm.local_embedding_model
    )
    chat_model = ChatOllama(model=resolved_model, base_url=settings.env.ollama_host)
    return LangChainProvider(
        chat_model,
        provider_name=f"ollama:{resolved_model}",
        embed_fn=_make_embed_fn(resolved_embedding_model, settings.env.ollama_host),
    )
