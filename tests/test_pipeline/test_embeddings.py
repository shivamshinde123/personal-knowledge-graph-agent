"""Tests for chunk embedding and Chroma persistence.

Runs against the real configured cloud embedding model (OpenRouter — see
DECISIONS.md, 2026-08-30: embedding is always cloud, regardless of
provider_mode) and a real embedded Chroma collection — no mocking.
Requires a real OPENROUTER_API_KEY configured.
"""

from datetime import UTC, datetime

import numpy as np
import pytest

from pipeline.embeddings import embed_chunks, embed_query
from storage.chroma_store import get_collection, query


@pytest.fixture
def collection(tmp_path):
    return get_collection(tmp_path / "chroma")


@pytest.fixture(scope="module")
def embedding_dimensions():
    """The real configured embedding model's actual output width.

    Computed once via a real call rather than hardcoded, so these tests
    don't need updating every time the configured cloud embedding model
    changes (see config/config.yaml's llm.cloud_embedding_model).
    """
    return len(embed_query("dimension probe"))


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

    def test_persists_a_vector_per_chunk_to_chroma(
        self, collection, embedding_dimensions
    ):
        chunks = embed_chunks(collection, "item-1", "gmail", ["hello world"])

        assert collection.count() == 1
        results = query(collection, [0.0] * embedding_dimensions, top_k=1)
        assert results[0].id == chunks[0].embedding_id
        assert results[0].document == "hello world"
        assert results[0].item_id == "item-1"
        assert results[0].source_type == "gmail"

    def test_chroma_metadata_carries_optional_fields_through(
        self, collection, embedding_dimensions
    ):
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

        result = query(collection, [0.0] * embedding_dimensions, top_k=1)[0]
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

        results = query(collection, embed_query("A kitten rested on the rug."), top_k=2)

        assert results[0].document == "The cat sat on the mat."


class TestEmbedQuery:
    def test_returns_a_non_empty_vector(self):
        vector = embed_query("some query text")

        assert len(vector) > 0

    def test_similar_text_yields_a_closer_vector_by_cosine_distance(self):
        cat = np.array(embed_query("The cat sat on the mat."))
        kitten = np.array(embed_query("A kitten rested on the rug."))
        revenue = np.array(embed_query("Quarterly revenue grew by twelve percent."))

        def cosine_distance(a, b):
            return 1 - (a @ b) / (np.linalg.norm(a) * np.linalg.norm(b))

        assert cosine_distance(cat, kitten) < cosine_distance(cat, revenue)
