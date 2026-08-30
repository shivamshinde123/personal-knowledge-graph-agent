"""Ollama-backed provider: the local half of the dual provider setup.

Used when ``provider_mode: fully_local``. See ``providers/base.py`` for
the shared contract and implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache

from langchain_ollama import ChatOllama
from sentence_transformers import SentenceTransformer

from config.settings import get_settings
from providers.base import LangChainProvider, ProviderInterface


@cache
def _sentence_transformer(name: str) -> SentenceTransformer:
    """Load (and cache) a sentence-transformers model by name.

    Cached here (moved from ``pipeline/embeddings.py``, which now routes
    through this provider instead of loading the model itself — see
    ``DECISIONS.md``) rather than in ``pipeline/``, since the model choice
    is a provider-level concern (``local_embedding_model``), not a
    pipeline one.
    """
    return SentenceTransformer(name)


def _make_embed_fn(model_name: str):
    def embed(texts: Sequence[str]) -> list[list[float]]:
        model = _sentence_transformer(model_name)
        return model.encode(list(texts), convert_to_numpy=True).tolist()

    return embed


def create_local_provider(
    model: str | None = None, *, embedding_model: str | None = None
) -> ProviderInterface:
    """Build a provider backed by a locally running Ollama model.

    Args:
        model: The generation model tag to use (e.g. ``"llama3:8b"``).
            Defaults to ``settings.config.llm.local_generation_model``.
        embedding_model: The ``sentence-transformers`` model name to
            embed with. Defaults to
            ``settings.config.llm.local_embedding_model``.

    Returns:
        A provider that routes every generation call through Ollama and
        every embedding call through a local ``sentence-transformers``
        model.
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
        embed_fn=_make_embed_fn(resolved_embedding_model),
    )
