"""Tests for the Chroma storage layer."""

from datetime import UTC, datetime

import pytest

from storage.chroma_store import (
    VectorChunk,
    VectorStoreError,
    delete_by_item,
    get_collection,
    get_item_chunk_vectors,
    get_item_embeddings,
    query,
    reset_all,
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

    def test_a_dimension_mismatch_raises_a_clear_actionable_error(self, collection):
        """Real-world regression: the embedding model's output width changed.

        (llm.cloud_embedding_model config change, or a dropped forced-
        truncation), but the collection was already locked to the old
        dimension — every subsequent write failed with Chroma's own
        generic message. Verified directly against a real occurrence.
        """
        upsert_chunks(collection, [make_chunk(embedding=[0.1, 0.2, 0.3])])

        with pytest.raises(VectorStoreError, match="Reset all data"):
            upsert_chunks(collection, [make_chunk(id="emb-2", embedding=[0.1] * 10)])


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


class TestGetItemEmbeddings:
    def test_returns_every_chunk_embedding_for_the_item(self, collection):
        upsert_chunks(
            collection,
            [
                make_chunk(id="a", item_id="item-1", embedding=[1.0, 0.0, 0.0]),
                make_chunk(id="b", item_id="item-1", embedding=[0.0, 1.0, 0.0]),
                make_chunk(id="c", item_id="item-2", embedding=[0.0, 0.0, 1.0]),
            ],
        )

        vectors = get_item_embeddings(collection, "item-1")

        assert sorted(vectors) == [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]]

    def test_returns_empty_list_for_an_item_with_no_chunks(self, collection):
        assert get_item_embeddings(collection, "does-not-exist") == []


class TestGetItemChunkVectors:
    def test_returns_each_chunks_text_paired_with_its_embedding(self, collection):
        upsert_chunks(
            collection,
            [
                make_chunk(
                    id="a",
                    item_id="item-1",
                    document="First chunk text.",
                    embedding=[1.0, 0.0, 0.0],
                ),
                make_chunk(
                    id="b",
                    item_id="item-1",
                    document="Second chunk text.",
                    embedding=[0.0, 1.0, 0.0],
                ),
                make_chunk(id="c", item_id="item-2", document="Other item."),
            ],
        )

        vectors = get_item_chunk_vectors(collection, "item-1")

        by_text = {v.document: v.embedding for v in vectors}
        assert by_text == {
            "First chunk text.": [1.0, 0.0, 0.0],
            "Second chunk text.": [0.0, 1.0, 0.0],
        }

    def test_returns_empty_list_for_an_item_with_no_chunks(self, collection):
        assert get_item_chunk_vectors(collection, "does-not-exist") == []


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


class TestResetAll:
    def test_removes_every_chunk(self, tmp_path):
        persist_dir = tmp_path / "chroma"
        collection = get_collection(persist_dir)
        upsert_chunks(
            collection,
            [
                make_chunk(id="a", item_id="item-1"),
                make_chunk(id="b", item_id="item-2"),
            ],
        )

        new_collection = reset_all(persist_dir)

        assert new_collection.count() == 0

    def test_no_op_on_an_already_empty_collection(self, tmp_path):
        reset_all(tmp_path / "chroma")  # doesn't raise -- nothing to delete yet

    def test_clears_a_dimension_lock_left_by_the_previous_reset_approach(
        self, tmp_path
    ):
        """Regression: deleting documents alone didn't clear the dimension.

        An earlier version of reset_all() only deleted every document,
        leaving the collection's HNSW index -- and the dimension it locked
        in on first write -- untouched, so a write of a different-sized
        embedding kept failing right after a "reset". Verified directly
        against a real occurrence. See DECISIONS.md.
        """
        persist_dir = tmp_path / "chroma"
        collection = get_collection(persist_dir)
        upsert_chunks(collection, [make_chunk(embedding=[0.1, 0.2, 0.3])])

        new_collection = reset_all(persist_dir)

        # A write at a different dimension must now succeed.
        upsert_chunks(new_collection, [make_chunk(id="emb-2", embedding=[0.1] * 10)])
        assert new_collection.count() == 1
