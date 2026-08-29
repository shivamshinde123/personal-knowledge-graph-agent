"""Tests for the LangGraph wiring: agent/graph.py::run().

Real integration tests against the real embedding model, a real Chroma
collection, real SQLite, and a live Neo4j instance (skipped automatically
if none is reachable, matching the rest of the agent/pipeline test
suites). Only the LLM provider is faked, for deterministic answers.
"""

import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import neo4j
import pytest

from agent.graph import run
from pipeline.embeddings import embed_chunks
from storage.chroma_store import get_collection
from storage.neo4j_store import (
    ItemNode,
    Relationship,
    ensure_constraints,
    get_driver,
    write_relationship,
)
from storage.sqlite_store import Item, connect, insert_item, replace_chunks

TEST_URI = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
TEST_USER = os.environ.get("NEO4J_TEST_USER", "neo4j")
TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "testpassword123")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_local(uri: str) -> bool:
    return urlsplit(uri).hostname in _LOCAL_HOSTS


if not _is_local(TEST_URI) and not os.environ.get("NEO4J_TEST_ALLOW_NONLOCAL_WIPE"):
    pytest.skip(
        f"NEO4J_TEST_URI={TEST_URI!r} is not localhost, and this suite wipes "
        "the entire database on every run.",
        allow_module_level=True,
    )


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


pytestmark = pytest.mark.skipif(
    not _server_available(), reason="No Neo4j server reachable at NEO4J_TEST_URI"
)


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


class FakeProvider:
    """A fake ProviderInterface returning a fixed answer for every call."""

    def __init__(self, answer="Here is the synthesized answer [1]."):
        """Return ``answer`` for every generate_answer() call."""
        self._answer = answer
        self.calls: list[tuple[str, list, tuple]] = []

    def generate_answer(self, question, context, history=()):
        self.calls.append((question, list(context), tuple(history)))
        return self._answer


def ingest(conn, collection, *, item_id, title, text, source_type="notion"):
    insert_item(
        conn,
        Item(
            id=item_id,
            source_type=source_type,
            source_ref_id=item_id,
            title=title,
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )
    chunks = embed_chunks(collection, item_id, source_type, [text])
    replace_chunks(conn, item_id, chunks)


class TestRun:
    def test_answers_a_plain_question_using_vector_and_keyword_search(
        self, conn, collection, driver, monkeypatch
    ):
        ingest(
            conn,
            collection,
            item_id="item-storage",
            title="Storage design",
            text="The storage layer uses SQLite, Chroma, and Neo4j.",
        )
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = run(
            conn, collection, driver, "What database technologies does the system use?"
        )

        assert result.answer == "Here is the synthesized answer [1]."
        assert any(s.item_id == "item-storage" for s in result.sources)
        assert "vector_search" in result.retrieval_methods_used
        assert "keyword_search" in result.retrieval_methods_used
        assert "graph_traversal" not in result.retrieval_methods_used

    def test_a_relationship_question_pulls_in_a_graph_connected_item(
        self, conn, collection, driver, monkeypatch
    ):
        ingest(
            conn,
            collection,
            item_id="item-storage",
            title="Storage design",
            text="The storage layer uses SQLite, Chroma, and Neo4j.",
        )
        ingest(
            conn,
            collection,
            item_id="item-batch",
            title="Daily batch",
            text="Something entirely unrelated in wording.",
        )
        write_relationship(
            driver,
            ItemNode(id="item-storage", source_type="notion", title="Storage design"),
            ItemNode(id="item-batch", source_type="notion", title="Daily batch"),
            Relationship(label="discussed_in"),
        )
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = run(
            conn,
            collection,
            driver,
            "How does the storage layer relate to other parts of the system?",
        )

        assert "graph_traversal" in result.retrieval_methods_used
        assert any(s.item_id == "item-batch" for s in result.sources)

    def test_a_specific_lookup_skips_vector_search(
        self, conn, collection, driver, monkeypatch
    ):
        ingest(
            conn,
            collection,
            item_id="item-recruiter",
            title="Recruiter email",
            text="Interview scheduled for next week.",
            source_type="gmail",
        )
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = run(conn, collection, driver, "Show me emails from this recruiter")

        assert "vector_search" not in result.retrieval_methods_used
        assert "keyword_search" in result.retrieval_methods_used

    def test_no_matching_content_returns_a_fixed_answer_without_calling_the_provider(
        self, conn, collection, driver, monkeypatch
    ):
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = run(conn, collection, driver, "What did I work on yesterday?")

        assert result.sources == []
        assert provider.calls == []
        assert result.answer

    def test_generates_a_session_id_when_none_given(
        self, conn, collection, driver, monkeypatch
    ):
        monkeypatch.setattr(
            "agent.synthesizer.get_provider", lambda task: FakeProvider()
        )

        result = run(conn, collection, driver, "What did I work on yesterday?")

        assert result.session_id

    def test_reuses_a_given_session_id(self, conn, collection, driver, monkeypatch):
        monkeypatch.setattr(
            "agent.synthesizer.get_provider", lambda task: FakeProvider()
        )

        result = run(
            conn, collection, driver, "What did I work on yesterday?", "sess-123"
        )

        assert result.session_id == "sess-123"
