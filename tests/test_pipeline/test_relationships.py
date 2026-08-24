"""Tests for candidate-narrowed relationship detection.

Real integration tests: SQLite (``:memory:``), an embedded Chroma
collection, the real configured embedding model, and a live Neo4j instance
(skipped automatically if none is reachable, same as
``tests/test_storage/test_neo4j_store.py``). Only the LLM provider is
faked, since relationship judgments need to be deterministic per test.
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import neo4j
import pytest

from pipeline.embeddings import embed_chunks
from pipeline.relationships import detect_relationships
from providers.base import ProviderError, RelationshipJudgment
from storage.chroma_store import get_collection
from storage.neo4j_store import ensure_constraints, get_driver, get_related_items
from storage.sqlite_store import Item, connect, insert_item, replace_chunks

TEST_URI = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
TEST_USER = os.environ.get("NEO4J_TEST_USER", "neo4j")
TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "testpassword123")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_local(uri: str) -> bool:
    return urlsplit(uri).hostname in _LOCAL_HOSTS


def _server_available() -> bool:
    try:
        driver = neo4j.GraphDatabase.driver(TEST_URI, auth=(TEST_USER, TEST_PASSWORD))
        try:
            driver.verify_connectivity()
        finally:
            driver.close()
    except Exception:
        return False
    return True


if not _is_local(TEST_URI) and not os.environ.get("NEO4J_TEST_ALLOW_NONLOCAL_WIPE"):
    pytest.skip(
        f"NEO4J_TEST_URI={TEST_URI!r} is not localhost, and this suite wipes "
        "the entire database on every run.",
        allow_module_level=True,
    )

pytestmark = pytest.mark.skipif(
    not _server_available(), reason="No Neo4j server reachable at NEO4J_TEST_URI"
)


class FakeProvider:
    """A fake ProviderInterface returning canned relationship judgments."""

    def __init__(self, judgments_by_pair=None, default=None):
        """Map candidate text to a judgment, or fall back to ``default``."""
        self.calls: list[tuple[str, str]] = []
        self._judgments_by_pair = judgments_by_pair or {}
        self._default = default

    def generate_relationship(self, source_text, candidate_text):
        self.calls.append((source_text, candidate_text))
        return self._judgments_by_pair.get(candidate_text, self._default)


@pytest.fixture
def driver():
    d = get_driver(uri=TEST_URI, user=TEST_USER, password=TEST_PASSWORD)
    ensure_constraints(d)
    with d.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    yield d
    with d.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    d.close()


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def collection(tmp_path):
    return get_collection(tmp_path / "chroma")


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


def ingest(conn, collection, *, ref: str, text: str, project_name=None) -> str:
    """Insert an item with one embedded, SQLite-persisted chunk; return its id."""
    item_id = insert_item(
        conn,
        make_item(
            id=f"raw-{ref}",
            source_ref_id=ref,
            title=f"Item {ref}",
            project_name=project_name,
        ),
    )
    chunks = embed_chunks(
        collection, item_id, "notion", [text], project_name=project_name
    )
    replace_chunks(conn, item_id, chunks)
    return item_id


@pytest.fixture(autouse=True)
def set_candidate_count(monkeypatch):
    monkeypatch.setattr(
        "pipeline.relationships.get_settings",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                retrieval=SimpleNamespace(relationship_candidate_count=5)
            )
        ),
    )


class TestDetectRelationships:
    def test_confirmed_candidate_is_written_to_neo4j(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        candidate_id = ingest(
            conn, collection, ref="b", text="Storage layer design notes"
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == [(candidate_id, provider._default)]
        related = get_related_items(driver, source_id)
        assert [r.item.id for r in related] == [candidate_id]
        assert related[0].relationship.label == "discussed_in"

    def test_rejected_candidate_writes_nothing(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        ingest(conn, collection, ref="b", text="Storage layer design notes")
        provider = FakeProvider(default=None)
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert get_related_items(driver, source_id) == []

    def test_the_source_item_is_never_its_own_candidate(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Only item in the store")
        provider = FakeProvider(
            default=RelationshipJudgment(label="implements", confidence=0.5)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert provider.calls == []

    def test_project_name_narrows_candidates_to_the_same_project(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(
            conn,
            collection,
            ref="a",
            text="pkg-agent chunking design",
            project_name="pkg-agent",
        )
        ingest(
            conn,
            collection,
            ref="b",
            text="pkg-agent embeddings design",
            project_name="pkg-agent",
        )
        other_project_id = ingest(
            conn,
            collection,
            ref="c",
            text="pkg-agent unrelated other project notes",
            project_name="other-project",
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.7)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        matched_ids = {item_id for item_id, _ in result}
        assert other_project_id not in matched_ids

    def test_unknown_item_returns_empty_without_touching_storage(
        self, conn, driver, collection, monkeypatch
    ):
        provider = FakeProvider(default=RelationshipJudgment(label="x"))
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        assert detect_relationships(conn, driver, collection, "never-ingested") == []
        assert provider.calls == []

    def test_item_with_no_chunks_returns_empty(self, conn, driver, collection):
        item_id = insert_item(conn, make_item(id="raw-a", source_ref_id="a"))

        assert detect_relationships(conn, driver, collection, item_id) == []

    def test_a_failed_provider_call_is_skipped_not_raised(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        ingest(conn, collection, ref="b", text="Storage layer design notes")

        class ExplodingProvider:
            def generate_relationship(self, source_text, candidate_text):
                raise ProviderError("boom")

        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: ExplodingProvider()
        )

        assert detect_relationships(conn, driver, collection, source_id) == []
