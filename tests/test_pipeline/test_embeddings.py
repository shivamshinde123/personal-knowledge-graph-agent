"""Tests for chunk embedding and Chroma persistence.

Runs against the real configured sentence-transformers model and a real
embedded Chroma collection — no mocking. The model loads once per test
session (cached by ``pipeline.embeddings._model``) and must already be
locally cached (or reachable) as `sentence-transformers/all-MiniLM-L6-v2`.
"""

from datetime import UTC, datetime

import pytest

from config.settings import get_settings
from pipeline.embeddings import _model, embed_chunks
from storage.chroma_store import get_collection, query


@pytest.fixture
def collection(tmp_path):
    return get_collection(tmp_path / "chroma")


class TestEmbedChunks:
    def test_returns_one_sqlite_chunk_per_input_text_in_order(self, collection):
        chunks = embed_chunks(
            collection, "item-1", "notion", ["first chunk", "second chunk"]
        )

        assert [c.text for c in chunks] == ["first chunk", "second chunk"]
        assert [c.chunk_index for c in chunks] == [0, 1]
        assert all(c.item_id == "item-1" for c in chunks)

    def test_empty_input_returns_no_chunks_and_writes_nothing(self, collection):
        chunks = embed_chunks(collection, "item-1", "notion", [])

        assert chunks == []
        assert collection.count() == 0

    def test_persists_a_vector_per_chunk_to_chroma(self, collection):
        chunks = embed_chunks(collection, "item-1", "gmail", ["hello world"])

        assert collection.count() == 1
        results = query(collection, [0.0] * 384, top_k=1)
        assert results[0].id == chunks[0].embedding_id
        assert results[0].document == "hello world"
        assert results[0].item_id == "item-1"
        assert results[0].source_type == "gmail"

    def test_chroma_metadata_carries_optional_fields_through(self, collection):
        created = datetime(2026, 8, 1, tzinfo=UTC)

        embed_chunks(
            collection,
            "item-1",
            "notion",
            ["chunk text"],
            project_name="pkg-agent",
            topic="architecture",
            created_at=created,
        )

        result = query(collection, [0.0] * 384, top_k=1)[0]
        assert result.project_name == "pkg-agent"
        assert result.topic == "architecture"
        assert result.created_at == created

    def test_similar_chunks_land_near_each_other_in_the_real_embedding_space(
        self, collection
    ):
        embed_chunks(
            collection,
            "item-1",
            "notion",
            ["The cat sat on the mat.", "Quarterly revenue grew by twelve percent."],
        )

        results = query(
            collection, _embed_query("A kitten rested on the rug."), top_k=2
        )

        assert results[0].document == "The cat sat on the mat."


def _embed_query(text: str) -> list[float]:
    model = _model(get_settings().config.embedding.model)
    return model.encode([text], convert_to_numpy=True)[0].tolist()
