"""Ollama-backed provider: the local half of the dual provider setup.

Used directly (``provider_mode: fully_local``) or for the cheap, high-
frequency ingestion tasks under ``provider_mode: mixed``. See
``providers/base.py`` for the shared contract and implementation.
"""

from __future__ import annotations

from langchain_ollama import ChatOllama

from config.settings import get_settings
from providers.base import LangChainProvider, ProviderInterface


def create_local_provider(model: str | None = None) -> ProviderInterface:
    """Build a provider backed by a locally running Ollama model.

    Args:
        model: The model tag to use (e.g. ``"llama3:8b"``). Defaults to
            ``settings.config.llm.local_model``.

    Returns:
        A provider that routes every call through Ollama.
    """
    settings = get_settings()
    resolved_model = model or settings.config.llm.local_model
    chat_model = ChatOllama(model=resolved_model, base_url=settings.env.ollama_host)
    return LangChainProvider(chat_model, provider_name=f"ollama:{resolved_model}")
