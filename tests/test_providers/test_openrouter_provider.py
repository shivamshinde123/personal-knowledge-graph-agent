"""Tests for the OpenRouter provider constructor.

Constructing a ChatOpenAI/OpenAIEmbeddings doesn't touch the network, so
these run without hitting OpenRouter.
"""

from types import SimpleNamespace

import pytest
from langchain_openai import ChatOpenAI

from providers.base import LangChainProvider, ProviderError
from providers.openrouter_provider import create_openrouter_provider


def fake_settings(
    *,
    cloud_generation_model="anthropic/claude-sonnet-4",
    cloud_embedding_model="openai/text-embedding-3-small",
    cloud_max_tokens=4096,
    api_key="sk-test",
):
    return SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(
                cloud_generation_model=cloud_generation_model,
                cloud_embedding_model=cloud_embedding_model,
                cloud_max_tokens=cloud_max_tokens,
            )
        ),
        env=SimpleNamespace(openrouter_api_key=api_key),
    )


class TestCreateOpenrouterProvider:
    def test_uses_the_configured_model_and_openrouter_base_url(self, monkeypatch):
        monkeypatch.setattr(
            "providers.openrouter_provider.get_settings",
            lambda: fake_settings(cloud_generation_model="anthropic/claude-sonnet-4"),
        )

        provider = create_openrouter_provider()

        assert isinstance(provider, LangChainProvider)
        chat_model = provider._chat_model
        assert isinstance(chat_model, ChatOpenAI)
        assert chat_model.model_name == "anthropic/claude-sonnet-4"
        assert chat_model.openai_api_base == "https://openrouter.ai/api/v1"

    def test_explicit_model_overrides_the_configured_default(self, monkeypatch):
        monkeypatch.setattr(
            "providers.openrouter_provider.get_settings",
            lambda: fake_settings(),
        )

        provider = create_openrouter_provider(model="openai/gpt-4o")

        assert provider._chat_model.model_name == "openai/gpt-4o"

    def test_provider_name_includes_the_model(self, monkeypatch):
        monkeypatch.setattr(
            "providers.openrouter_provider.get_settings",
            lambda: fake_settings(),
        )

        provider = create_openrouter_provider(model="openai/gpt-4o")

        assert provider._provider_name == "openrouter:openai/gpt-4o"

    def test_caps_max_tokens_at_the_configured_value(self, monkeypatch):
        monkeypatch.setattr(
            "providers.openrouter_provider.get_settings",
            lambda: fake_settings(cloud_max_tokens=2000),
        )

        provider = create_openrouter_provider()

        assert provider._chat_model.max_tokens == 2000

    def test_missing_api_key_raises_provider_error(self, monkeypatch):
        monkeypatch.setattr(
            "providers.openrouter_provider.get_settings",
            lambda: fake_settings(api_key=None),
        )

        with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
            create_openrouter_provider()

    def test_embed_fn_uses_the_configured_embedding_model(self, monkeypatch):
        monkeypatch.setattr(
            "providers.openrouter_provider.get_settings",
            lambda: fake_settings(
                cloud_embedding_model="openai/text-embedding-3-small"
            ),
        )
        captured = {}

        def fake_make_embed_fn(model_name, api_key):
            captured["model_name"] = model_name
            captured["api_key"] = api_key
            return lambda texts: [[0.0]] * len(texts)

        monkeypatch.setattr(
            "providers.openrouter_provider._make_embed_fn", fake_make_embed_fn
        )

        provider = create_openrouter_provider()

        assert captured["model_name"] == "openai/text-embedding-3-small"
        assert captured["api_key"] == "sk-test"
        assert provider._embed_fn(["a"]) == [[0.0]]

    def test_explicit_embedding_model_overrides_the_configured_default(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "providers.openrouter_provider.get_settings",
            lambda: fake_settings(),
        )
        captured = {}
        monkeypatch.setattr(
            "providers.openrouter_provider._make_embed_fn",
            lambda model_name, api_key: captured.setdefault("model_name", model_name),
        )

        create_openrouter_provider(embedding_model="a-different-model")

        assert captured["model_name"] == "a-different-model"
