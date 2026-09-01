"""OpenRouter-backed provider: the cloud half of the dual provider setup.

Used for both generation and embedding when ``provider_mode: fully_cloud``.
See ``providers/base.py`` for the shared contract and implementation.

The embedding model (``llm.cloud_embedding_model``) is user-editable, like
the generation model — changing it is a bigger deal than changing the
generation model, since it changes which embedding space every future
vector lands in. Nothing here enforces the "reset before changing"
requirement — that's the frontend's job (a confirm prompt on save, then
an automatic reset + re-ingest — see ``frontend/src/components/
SettingsPanel.jsx``, ``DECISIONS.md``); this module just always uses
whatever's currently configured.

``max_tokens`` is always explicitly capped (``config.yaml``'s
``llm.cloud_max_tokens``, default 4096) rather than left unset — an unset
``max_tokens`` defaults to the routed model's own maximum (64000 for
``anthropic/claude-sonnet-4``), which can exceed the account's remaining
OpenRouter credit balance and fail the call outright with a 402, verified
directly. See ``DECISIONS.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from config.settings import get_settings
from providers.base import LangChainProvider, ProviderError, ProviderInterface

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _make_embed_fn(model_name: str, api_key: str):
    # OpenAIEmbeddings, not a hand-rolled httpx call — OpenRouter exposes
    # an OpenAI-compatible /embeddings endpoint (verified directly), the
    # same "go through LangChain" pattern ChatOpenAI already uses for
    # generation. See DECISIONS.md.
    embeddings = OpenAIEmbeddings(
        model=model_name, base_url=_OPENROUTER_BASE_URL, api_key=api_key
    )

    def embed(texts: Sequence[str]) -> list[list[float]]:
        return embeddings.embed_documents(list(texts))

    return embed


def create_openrouter_provider(
    model: str | None = None, *, embedding_model: str | None = None
) -> ProviderInterface:
    """Build a provider backed by a model served through OpenRouter.

    Args:
        model: The OpenRouter generation model id (e.g.
            ``"anthropic/claude-sonnet-4"``). Defaults to
            ``settings.config.llm.cloud_generation_model``.
        embedding_model: The OpenRouter embedding model id (e.g.
            ``"openai/text-embedding-3-small"``). Defaults to
            ``settings.config.llm.cloud_embedding_model``.

    Returns:
        A provider that routes every generation call through OpenRouter
        and every embedding call through the configured embedding model.

    Raises:
        ProviderError: If ``OPENROUTER_API_KEY`` is not configured — this
            provider is only ever constructed under ``fully_cloud``, where
            the key is required for both generation and embedding.
    """
    settings = get_settings()
    if not settings.env.openrouter_api_key:
        raise ProviderError(
            "OPENROUTER_API_KEY is not configured; set it in config/.env — "
            "required for generation and embedding under fully_cloud."
        )
    resolved_model = model or settings.config.llm.cloud_generation_model
    resolved_embedding_model = (
        embedding_model or settings.config.llm.cloud_embedding_model
    )
    chat_model = ChatOpenAI(
        model=resolved_model,
        base_url=_OPENROUTER_BASE_URL,
        api_key=settings.env.openrouter_api_key,
        max_tokens=settings.config.llm.cloud_max_tokens,
    )
    return LangChainProvider(
        chat_model,
        provider_name=f"openrouter:{resolved_model}",
        embed_fn=_make_embed_fn(
            resolved_embedding_model, settings.env.openrouter_api_key
        ),
    )
