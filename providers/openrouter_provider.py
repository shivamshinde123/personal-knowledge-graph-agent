"""OpenRouter-backed provider: the cloud half of the dual provider setup.

Used directly (``provider_mode: fully_cloud``) or for the low-frequency,
quality-sensitive answer synthesis task under ``provider_mode: mixed``. See
``providers/base.py`` for the shared contract and implementation.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from config.settings import get_settings
from providers.base import LangChainProvider, ProviderError, ProviderInterface

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def create_openrouter_provider(model: str | None = None) -> ProviderInterface:
    """Build a provider backed by a model served through OpenRouter.

    Args:
        model: The OpenRouter model id (e.g. ``"anthropic/claude-sonnet-4"``).
            Defaults to ``settings.config.llm.cloud_model``.

    Returns:
        A provider that routes every call through OpenRouter.

    Raises:
        ProviderError: If ``OPENROUTER_API_KEY`` is not configured.
    """
    settings = get_settings()
    if not settings.env.openrouter_api_key:
        raise ProviderError(
            "OPENROUTER_API_KEY is not configured; set it in config/.env to "
            "use a fully_cloud or mixed provider mode."
        )
    resolved_model = model or settings.config.llm.cloud_model
    chat_model = ChatOpenAI(
        model=resolved_model,
        base_url=_OPENROUTER_BASE_URL,
        api_key=settings.env.openrouter_api_key,
    )
    return LangChainProvider(chat_model, provider_name=f"openrouter:{resolved_model}")
