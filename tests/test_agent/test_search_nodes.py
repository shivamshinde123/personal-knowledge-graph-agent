"""Tests for the vector and keyword search nodes.

Real integration tests: the real configured embedding model, a real
embedded Chroma collection, and real SQLite/FTS5 — no mocking, matching the
storage/pipeline test suites' convention. Only ``config.yaml``'s retrieval
settings (``top_k_vector``/``top_k_keyword``) are patched, to keep tests
fast and deterministic.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.search_nodes import keyword_search_node, vector_search
from pipeline.embeddings import embed_chunks
from storage.chroma_store import get_collection
from storage.sqlite_store import Chunk, Item, connect, insert_chunk, insert_item


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def collection(tmp_path):
    return get_collection(tmp_path / "chroma")


@pytest.fixture(autouse=True)
def set_top_k(monkeypatch):
    monkeypatch.setattr(
        "agent.search_nodes.get_settings",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                retrieval=SimpleNamespace(top_k_vector=5, top_k_keyword=5)
            )
        ),
    )


def make_item(**overrides) -> Item:
    defaults = dict(
        id="item-placeholder",
        source_type="notion",
        source_ref_id="page-placeholder",
        title="A page",
        url_or_path="https://notion.so/page-placeholder",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Item(**defaults)


class TestVectorSearch:
    def test_finds_semantically_similar_item(self, collection):
        embed_chunks(
            collection,
            "item-storage",
            "notion",
            ["The storage layer uses SQLite, Chroma, and Neo4j."],
        )
        embed_chunks(
            collection,
            "item-recipe",
            "notion",
            ["A traditional Italian pasta recipe needs tomatoes and basil."],
        )

        hits = vector_search(
            collection, "What database technologies does the system use?"
        )

        assert hits[0].item_id == "item-storage"

    def test_deduplicates_multiple_matching_chunks_of_the_same_item(self, collection):
        embed_chunks(
            collection,
            "item-storage",
            "notion",
            [
                "The storage layer uses SQLite for raw text.",
                "The storage layer uses Chroma for embeddings.",
            ],
        )

        hits = vector_search(collection, "storage layer")

        assert [h.item_id for h in hits] == ["item-storage"]

    def test_hits_are_ranked_starting_at_one(self, collection):
        embed_chunks(collection, "item-a", "notion", ["storage layer design"])

        hits = vector_search(collection, "storage layer design")

        assert hits[0].rank == 1


class TestKeywordSearchNode:
    def test_finds_matching_item_by_significant_term(self, conn):
        insert_item(conn, make_item(id="item-router", source_ref_id="a"))
        insert_chunk(
            conn,
            Chunk(
                id="c0",
                item_id="item-router",
                chunk_index=0,
                text="The router decides between vector and keyword search.",
                embedding_id="e0",
            ),
        )
        insert_item(conn, make_item(id="item-other", source_ref_id="b"))
        insert_chunk(
            conn,
            Chunk(
                id="c1",
                item_id="item-other",
                chunk_index=0,
                text="Browser history is the lightest-weight source.",
                embedding_id="e1",
            ),
        )

        hits = keyword_search_node(conn, "What does the router decide?")

        assert [h.item_id for h in hits] == ["item-router"]

    def test_deduplicates_multiple_matching_chunks_of_the_same_item(self, conn):
        insert_item(conn, make_item(id="item-a", source_ref_id="a"))
        insert_chunk(
            conn,
            Chunk(
                id="c0",
                item_id="item-a",
                chunk_index=0,
                text="storage layer uses SQLite",
                embedding_id="e0",
            ),
        )
        insert_chunk(
            conn,
            Chunk(
                id="c1",
                item_id="item-a",
                chunk_index=1,
                text="storage layer uses Chroma",
                embedding_id="e1",
            ),
        )

        hits = keyword_search_node(conn, "storage layer")

        assert [h.item_id for h in hits] == ["item-a"]

    def test_question_with_only_stopwords_returns_no_hits_without_erroring(self, conn):
        insert_item(conn, make_item(id="item-a", source_ref_id="a"))
        insert_chunk(
            conn,
            Chunk(
                id="c0",
                item_id="item-a",
                chunk_index=0,
                text="storage layer design",
                embedding_id="e0",
            ),
        )

        assert keyword_search_node(conn, "What is this?") == []

    def test_apostrophes_and_punctuation_do_not_break_the_query(self, conn):
        insert_item(conn, make_item(id="item-a", source_ref_id="a"))
        insert_chunk(
            conn,
            Chunk(
                id="c0",
                item_id="item-a",
                chunk_index=0,
                text="daily batch scheduling details",
                embedding_id="e0",
            ),
        )

        hits = keyword_search_node(conn, "What's the daily batch's scheduling?")

        assert [h.item_id for h in hits] == ["item-a"]
