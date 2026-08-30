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

# Frozen, not user-configurable — see providers/openrouter_provider.py's
# EMBEDDING_MODEL/EMBEDDING_DIMENSIONS for why: this model's native output
# is exactly EMBEDDING_DIMENSIONS wide, matching what the cloud side is
# truncated to, so Chroma vectors from either provider are always the same
# dimensionality regardless of which one embedded them. Letting either side
# be freely changed would silently reintroduce a dimension mismatch — see
# DECISIONS.md.
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384


@cache
def _sentence_transformer(name: str) -> SentenceTransformer:
    """Load (and cache) a sentence-transformers model by name."""
    return SentenceTransformer(name)


def _embed(texts: Sequence[str]) -> list[list[float]]:
    model = _sentence_transformer(EMBEDDING_MODEL)
    return model.encode(list(texts), convert_to_numpy=True).tolist()


def create_local_provider(model: str | None = None) -> ProviderInterface:
    """Build a provider backed by a locally running Ollama model.

    Args:
        model: The generation model tag to use (e.g. ``"llama3:8b"``).
            Defaults to ``settings.config.llm.local_generation_model``.

    Returns:
        A provider that routes every generation call through Ollama and
        every embedding call through the frozen local
        ``sentence-transformers`` model (:data:`EMBEDDING_MODEL`).
    """
    settings = get_settings()
    resolved_model = model or settings.config.llm.local_generation_model
    chat_model = ChatOllama(model=resolved_model, base_url=settings.env.ollama_host)
    return LangChainProvider(
        chat_model,
        provider_name=f"ollama:{resolved_model}",
        embed_fn=_embed,
    )
