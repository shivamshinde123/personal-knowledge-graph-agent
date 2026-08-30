"""Ollama-backed provider: the local half of the dual provider setup.

Used when ``provider_mode: fully_local``, for generation only — embedding
always goes through OpenRouter regardless of ``provider_mode`` (see
``providers/base.py::get_provider()``, ``providers/openrouter_provider.py``,
``DECISIONS.md``), so this provider is constructed with no embedding
function at all; a call to ``generate_embeddings()`` on it raises
``ProviderError`` (``LangChainProvider``'s own safety net for an
``embed_fn``-less provider). See ``providers/base.py`` for the shared
contract and implementation.
"""

from __future__ import annotations

from langchain_ollama import ChatOllama

from config.settings import get_settings
from providers.base import LangChainProvider, ProviderInterface


def create_local_provider(model: str | None = None) -> ProviderInterface:
    """Build a provider backed by a locally running Ollama model.

    Args:
        model: The generation model tag to use (e.g. ``"llama3:8b"``).
            Defaults to ``settings.config.llm.local_generation_model``.

    Returns:
        A provider that routes every generation call through Ollama.
        Never call :meth:`~providers.base.ProviderInterface.generate_embeddings`
        on it — use ``get_provider("embedding")`` instead, which always
        resolves to OpenRouter.
    """
    settings = get_settings()
    resolved_model = model or settings.config.llm.local_generation_model
    chat_model = ChatOllama(model=resolved_model, base_url=settings.env.ollama_host)
    return LangChainProvider(chat_model, provider_name=f"ollama:{resolved_model}")
