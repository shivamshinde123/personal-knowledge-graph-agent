"""OpenRouter-backed provider: the cloud half of the dual provider setup.

Used for generation when ``provider_mode: fully_cloud``, and *always* for
embedding regardless of ``provider_mode`` — there is no local embedding
path any more (removed — see ``providers/base.py::get_provider()``,
``DECISIONS.md``). This means an ingestion run under ``fully_local``
still needs ``OPENROUTER_API_KEY`` configured, purely for embeddings; a
missing key fails every item's embedding call, not just cloud-generation
ones. See ``providers/base.py`` for the shared contract and
implementation.

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

# Frozen, not user-configurable — the sole embedding model, used
# regardless of provider_mode (no local embedding path any more, so there
# is no second model to match dimensions against). Left truncated to 384
# dimensions via the `dimensions` parameter anyway (Matryoshka
# representation learning — the model is trained so its leading
# dimensions alone remain a valid, if slightly lower-quality, embedding —
# text-embedding-3-small's native output is 1536-wide) rather than left at
# its native size, mainly to keep vectors small/cheap to store; verified
# directly against the real OpenRouter API that dimensions=384 actually
# returns an exactly-384-wide vector. See DECISIONS.md.
EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 384


def _make_embed_fn(api_key: str):
    # OpenAIEmbeddings, not a hand-rolled httpx call — OpenRouter exposes
    # an OpenAI-compatible /embeddings endpoint (verified directly), the
    # same "go through LangChain" pattern ChatOpenAI already uses for
    # generation. See DECISIONS.md.
    embeddings = OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        base_url=_OPENROUTER_BASE_URL,
        api_key=api_key,
    )

    def embed(texts: Sequence[str]) -> list[list[float]]:
        return embeddings.embed_documents(list(texts))

    return embed


def create_openrouter_provider(model: str | None = None) -> ProviderInterface:
    """Build a provider backed by a model served through OpenRouter.

    Args:
        model: The OpenRouter generation model id (e.g.
            ``"anthropic/claude-sonnet-4"``). Defaults to
            ``settings.config.llm.cloud_generation_model``.

    Returns:
        A provider that routes every generation call through OpenRouter
        and every embedding call through the frozen embedding model
        (:data:`EMBEDDING_MODEL`, truncated to :data:`EMBEDDING_DIMENSIONS`).

    Raises:
        ProviderError: If ``OPENROUTER_API_KEY`` is not configured — always
            required for embedding, regardless of ``provider_mode``, and
            also required for generation under ``fully_cloud``.
    """
    settings = get_settings()
    if not settings.env.openrouter_api_key:
        raise ProviderError(
            "OPENROUTER_API_KEY is not configured; set it in config/.env — "
            "required for embedding regardless of provider_mode, and for "
            "generation under fully_cloud."
        )
    resolved_model = model or settings.config.llm.cloud_generation_model
    chat_model = ChatOpenAI(
        model=resolved_model,
        base_url=_OPENROUTER_BASE_URL,
        api_key=settings.env.openrouter_api_key,
        max_tokens=settings.config.llm.cloud_max_tokens,
    )
    return LangChainProvider(
        chat_model,
        provider_name=f"openrouter:{resolved_model}",
        embed_fn=_make_embed_fn(settings.env.openrouter_api_key),
    )
