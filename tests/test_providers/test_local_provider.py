"""Tests for the Ollama provider constructor.

Constructing a ChatOllama doesn't touch the network — it just builds a
client object — so these run without a live Ollama instance.
"""

from types import SimpleNamespace

from langchain_ollama import ChatOllama

from providers.base import LangChainProvider
from providers.local_provider import EMBEDDING_DIMENSIONS, create_local_provider


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

    def test_embedding_model_is_not_configurable(self, monkeypatch):
        """The embedding model is a frozen constant — no way to override it."""
        monkeypatch.setattr(
            "providers.local_provider.get_settings", lambda: fake_settings()
        )

        provider = create_local_provider()

        assert provider._embed_fn is not None

    def test_embeds_real_text_at_the_frozen_dimensionality(self, monkeypatch):
        monkeypatch.setattr(
            "providers.local_provider.get_settings", lambda: fake_settings()
        )
        provider = create_local_provider()

        vectors = provider.generate_embeddings(["hello world"])

        assert len(vectors) == 1
        assert len(vectors[0]) == EMBEDDING_DIMENSIONS
