"""Tests for the Chroma storage layer."""

from datetime import UTC, datetime

import pytest

from storage.chroma_store import (
    VectorChunk,
    VectorStoreError,
    delete_by_item,
    get_collection,
    query,
    upsert_chunks,
)


@pytest.fixture
def collection(tmp_path):
    return get_collection(tmp_path / "chroma")


def make_chunk(**overrides) -> VectorChunk:
    defaults = dict(
        id="emb-1",
        embedding=[0.1, 0.2, 0.3],
        document="Extractors never import from storage directly.",
        item_id="item-1",
        source_type="notion",
        project_name="pkg-agent",
        topic="architecture",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return VectorChunk(**defaults)


class TestGetCollection:
    def test_creates_a_persistent_collection(self, tmp_path):
        collection = get_collection(tmp_path / "chroma")

        assert collection.name == "chunks"

    def test_reopening_the_same_dir_reuses_the_collection(self, tmp_path):
        persist_dir = tmp_path / "chroma"
        first = get_collection(persist_dir)
        upsert_chunks(first, [make_chunk()])

        second = get_collection(persist_dir)

        assert second.count() == 1


class TestUpsertChunks:
    def test_stores_embedding_document_and_metadata(self, collection):
        upsert_chunks(collection, [make_chunk()])

        assert collection.count() == 1
        results = query(collection, [0.1, 0.2, 0.3], top_k=1)
        assert results[0].id == "emb-1"
        assert results[0].item_id == "item-1"
        assert results[0].source_type == "notion"
        assert results[0].project_name == "pkg-agent"
        assert results[0].topic == "architecture"
        assert results[0].created_at == datetime(2026, 8, 1, tzinfo=UTC)

    def test_omits_unset_optional_metadata_fields(self, collection):
        upsert_chunks(
            collection,
            [make_chunk(project_name=None, topic=None, created_at=None)],
        )

        results = query(collection, [0.1, 0.2, 0.3], top_k=1)
        assert results[0].project_name is None
        assert results[0].topic is None
        assert results[0].created_at is None

    def test_upserting_the_same_id_overwrites_it(self, collection):
        upsert_chunks(collection, [make_chunk(document="original")])
        upsert_chunks(collection, [make_chunk(document="revised")])

        assert collection.count() == 1
        results = query(collection, [0.1, 0.2, 0.3], top_k=1)
        assert results[0].document == "revised"

    def test_empty_iterable_is_a_no_op(self, collection):
        upsert_chunks(collection, [])

        assert collection.count() == 0


class TestQuery:
    def test_orders_results_by_similarity(self, collection):
        upsert_chunks(
            collection,
            [
                make_chunk(id="close", embedding=[1.0, 0.0, 0.0]),
                make_chunk(id="far", embedding=[-1.0, 0.0, 0.0]),
            ],
        )

        results = query(collection, [1.0, 0.0, 0.0], top_k=2)

        assert [r.id for r in results] == ["close", "far"]

    def test_where_filter_narrows_results(self, collection):
        upsert_chunks(
            collection,
            [
                make_chunk(id="a", source_type="notion"),
                make_chunk(id="b", source_type="gmail"),
            ],
        )

        results = query(
            collection, [0.1, 0.2, 0.3], top_k=8, where={"source_type": "gmail"}
        )

        assert [r.id for r in results] == ["b"]


class TestErrors:
    def test_query_with_mismatched_dimensionality_raises_vector_store_error(
        self, collection
    ):
        upsert_chunks(collection, [make_chunk(embedding=[0.1, 0.2, 0.3])])

        with pytest.raises(VectorStoreError):
            query(collection, [0.1, 0.2], top_k=1)


class TestDeleteByItem:
    def test_removes_only_that_items_chunks(self, collection):
        upsert_chunks(
            collection,
            [
                make_chunk(id="a", item_id="item-1", embedding=[1.0, 0.0, 0.0]),
                make_chunk(id="b", item_id="item-2", embedding=[0.0, 1.0, 0.0]),
            ],
        )

        delete_by_item(collection, "item-1")

        assert collection.count() == 1
        results = query(collection, [0.0, 1.0, 0.0], top_k=8)
        assert [r.id for r in results] == ["b"]

    def test_deleting_a_nonexistent_item_is_harmless(self, collection):
        delete_by_item(collection, "does-not-exist")

        assert collection.count() == 0
