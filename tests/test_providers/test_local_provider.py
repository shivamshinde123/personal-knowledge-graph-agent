"""Tests for the Ollama provider constructor.

Constructing a ChatOllama/OllamaEmbeddings doesn't touch the network — it
just builds a client object — so these run without a live Ollama instance.
"""

from types import SimpleNamespace

from langchain_ollama import ChatOllama, OllamaEmbeddings

from providers.base import LangChainProvider
from providers.local_provider import create_local_provider


def fake_settings(
    *,
    local_generation_model="llama3:8b",
    local_embedding_model="nomic-embed-text",
    ollama_host="http://localhost:11434",
):
    return SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(
                local_generation_model=local_generation_model,
                local_embedding_model=local_embedding_model,
            )
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

    def test_embed_fn_uses_the_configured_embedding_model_and_host(self, monkeypatch):
        monkeypatch.setattr(
            "providers.local_provider.get_settings",
            lambda: fake_settings(
                local_embedding_model="nomic-embed-text",
                ollama_host="http://example:11434",
            ),
        )

        provider = create_local_provider()

        assert provider._embed_fn is not None
        embeddings = provider._embed_fn.__closure__[0].cell_contents
        assert isinstance(embeddings, OllamaEmbeddings)
        assert embeddings.model == "nomic-embed-text"
        assert embeddings.base_url == "http://example:11434"

    def test_explicit_embedding_model_overrides_the_configured_default(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "providers.local_provider.get_settings",
            lambda: fake_settings(local_embedding_model="nomic-embed-text"),
        )

        provider = create_local_provider(embedding_model="mxbai-embed-large")

        embeddings = provider._embed_fn.__closure__[0].cell_contents
        assert embeddings.model == "mxbai-embed-large"

    def test_generate_embeddings_delegates_to_the_embed_fn(self, monkeypatch):
        monkeypatch.setattr(
            "providers.local_provider.get_settings", lambda: fake_settings()
        )
        calls = []

        def fake_embed_documents(self, texts):
            calls.append(list(texts))
            return [[0.1, 0.2] for _ in texts]

        # OllamaEmbeddings is a pydantic model — instances reject arbitrary
        # attribute assignment, so patch the method on the class instead.
        monkeypatch.setattr(OllamaEmbeddings, "embed_documents", fake_embed_documents)
        provider = create_local_provider()

        result = provider.generate_embeddings(["hello", "world"])

        assert result == [[0.1, 0.2], [0.1, 0.2]]
        assert calls == [["hello", "world"]]
