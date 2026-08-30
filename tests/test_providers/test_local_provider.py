"""Tests for the Ollama provider constructor.

Constructing a ChatOllama doesn't touch the network — it just builds a
client object — so these run without a live Ollama instance.
"""

from types import SimpleNamespace

import pytest
from langchain_ollama import ChatOllama

from providers.base import LangChainProvider, ProviderError
from providers.local_provider import create_local_provider


def fake_settings(
    *, local_generation_model="llama3:8b", ollama_host="http://localhost:11434"
):
    return SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(local_generation_model=local_generation_model)
        ),
        env=SimpleNamespace(ollama_host=ollama_host),
    )


class TestCreateLocalProvider:
    def test_uses_the_configured_model_and_host_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "providers.local_provider.get_settings",
            lambda: fake_settings(
                local_generation_model="llama3:8b",
                ollama_host="http://example:11434",
            ),
        )

        provider = create_local_provider()

        assert isinstance(provider, LangChainProvider)
        chat_model = provider._chat_model
        assert isinstance(chat_model, ChatOllama)
        assert chat_model.model == "llama3:8b"
        assert chat_model.base_url == "http://example:11434"

    def test_explicit_model_overrides_the_configured_default(self, monkeypatch):
        monkeypatch.setattr(
            "providers.local_provider.get_settings",
            lambda: fake_settings(local_generation_model="llama3:8b"),
        )

        provider = create_local_provider(model="mistral:7b")

        assert provider._chat_model.model == "mistral:7b"

    def test_provider_name_includes_the_model(self, monkeypatch):
        monkeypatch.setattr(
            "providers.local_provider.get_settings", lambda: fake_settings()
        )

        provider = create_local_provider(model="mistral:7b")

        assert provider._provider_name == "ollama:mistral:7b"

    def test_has_no_embedding_function(self, monkeypatch):
        """There is no local embedding path any more.

        Embedding always goes through OpenRouter
        (providers/openrouter_provider.py), regardless of provider_mode —
        a local provider is generation-only.
        """
        monkeypatch.setattr(
            "providers.local_provider.get_settings", lambda: fake_settings()
        )
        provider = create_local_provider()

        with pytest.raises(ProviderError):
            provider.generate_embeddings(["hello"])
