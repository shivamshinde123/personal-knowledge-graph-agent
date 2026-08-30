"""OpenRouter-backed provider: the cloud half of the dual provider setup.

Used when ``provider_mode: fully_cloud``. See ``providers/base.py`` for
the shared contract and implementation.

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
    # an OpenAI-compatible /embeddings endpoint (verified directly: a real
    # POST to https://openrouter.ai/api/v1/embeddings returns a normal
    # OpenAI-shaped 401 rather than a 404, confirming the endpoint exists),
    # the same "go through LangChain" pattern ChatOpenAI already uses for
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
        A provider that routes every generation and embedding call
        through OpenRouter.

    Raises:
        ProviderError: If ``OPENROUTER_API_KEY`` is not configured.
    """
    settings = get_settings()
    if not settings.env.openrouter_api_key:
        raise ProviderError(
            "OPENROUTER_API_KEY is not configured; set it in config/.env to "
            "use fully_cloud provider mode."
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
