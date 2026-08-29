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
from pipeline.relationships import _mean_embedding, detect_relationships
from providers.base import ProviderError, RelationshipJudgment
from storage.chroma_store import get_collection
from storage.neo4j_store import (
    ItemNode,
    Relationship,
    ensure_constraints,
    get_driver,
    get_related_items,
    write_relationship,
)
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


def ingest_multi(
    conn, collection, *, ref: str, texts, project_name=None, source_type="notion"
) -> str:
    """Insert an item with multiple embedded, SQLite-persisted chunks; return its id."""
    item_id = insert_item(
        conn,
        make_item(
            id=f"raw-{ref}",
            source_type=source_type,
            source_ref_id=ref,
            title=f"Item {ref}",
            project_name=project_name,
        ),
    )
    chunks = embed_chunks(
        collection, item_id, source_type, list(texts), project_name=project_name
    )
    replace_chunks(conn, item_id, chunks)
    return item_id


def ingest(
    conn, collection, *, ref: str, text: str, project_name=None, source_type="notion"
) -> str:
    """Insert an item with one embedded, SQLite-persisted chunk; return its id."""
    return ingest_multi(
        conn,
        collection,
        ref=ref,
        texts=[text],
        project_name=project_name,
        source_type=source_type,
    )


@pytest.fixture(autouse=True)
def set_candidate_count(monkeypatch):
    monkeypatch.setattr(
        "pipeline.relationships.get_settings",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                retrieval=SimpleNamespace(
                    relationship_candidate_count=5,
                    relationship_confidence_threshold=0.6,
                    relationship_candidate_max_distance=None,
                )
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


class TestBrowserHistoryExcludedFromRelationships:
    """Browser history is intentionally excluded from relationship detection.

    Per CLAUDE.md's locked-in decisions: "Browser History is intentionally
    the lightest-weight source ... no relationship detection." Its only
    text is a short page title, which gives an LLM confirmation call almost
    nothing real to reason about.
    """

    def test_a_browser_history_item_never_initiates_detection(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(
            conn,
            collection,
            ref="a",
            text="Sign in to your account",
            source_type="browser_history",
        )
        ingest(conn, collection, ref="b", text="Sign in to your account details")
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert provider.calls == []

    def test_a_browser_history_item_is_never_a_candidate_for_another_item(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        ingest(
            conn,
            collection,
            ref="b",
            text="Building the storage layer",
            source_type="browser_history",
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert provider.calls == []


class TestConfidenceThreshold:
    def test_a_confirmed_but_low_confidence_judgment_is_discarded(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        ingest(conn, collection, ref="b", text="Storage layer design notes")
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.4)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert get_related_items(driver, source_id) == []

    def test_a_confirmed_judgment_at_or_above_the_threshold_is_kept(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        candidate_id = ingest(
            conn, collection, ref="b", text="Storage layer design notes"
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.6)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == [(candidate_id, provider._default)]

    def test_a_judgment_with_no_confidence_is_not_filtered(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        candidate_id = ingest(
            conn, collection, ref="b", text="Storage layer design notes"
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=None)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == [(candidate_id, provider._default)]


class TestWholeDocumentNarrowing:
    """Regression coverage for using every chunk, not just the first, to narrow.

    Real items often share near-identical boilerplate in their first chunk
    (a title block, a "prepared for" line) — narrowing on that chunk alone
    would treat every item sharing it as equally similar, regardless of
    what the rest of the document actually says.
    """

    def test_narrowing_prefers_similar_substance_over_a_shared_first_chunk(
        self, conn, driver, collection, monkeypatch
    ):
        # candidate_count=1: only the single nearest neighbor gets judged,
        # so this only passes if narrowing actually distinguished B from C.
        monkeypatch.setattr(
            "pipeline.relationships.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(
                    retrieval=SimpleNamespace(
                        relationship_candidate_count=1,
                        relationship_confidence_threshold=0.6,
                        relationship_candidate_max_distance=None,
                    )
                )
            ),
        )
        boilerplate = (
            "Personal Knowledge Graph Agent design document prepared by "
            "Shivam Shinde"
        )
        source_id = ingest_multi(
            conn,
            collection,
            ref="a",
            texts=[
                boilerplate,
                "The storage layer uses SQLite, Chroma, and Neo4j for local "
                "persistence of ingested items.",
            ],
        )
        storage_related_id = ingest_multi(
            conn,
            collection,
            ref="b",
            texts=[
                boilerplate,
                "SQLite, Chroma, and Neo4j together form the storage layer "
                "that persists all ingested items locally.",
            ],
        )
        ingest_multi(
            conn,
            collection,
            ref="c",
            texts=[
                boilerplate,
                "A traditional Italian pasta recipe needs fresh tomatoes, "
                "basil, garlic, and olive oil.",
            ],
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert [item_id for item_id, _ in result] == [storage_related_id]


class TestSkipsAlreadyRelatedCandidates:
    """Candidates already connected to the source, in either direction.

    Skipped before the LLM is even called — see DECISIONS.md.
    """

    def test_a_candidate_already_related_in_the_forward_direction_is_skipped(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        candidate_id = ingest(
            conn, collection, ref="b", text="Storage layer design notes"
        )
        write_relationship(
            driver,
            ItemNode(id=source_id, source_type="notion"),
            ItemNode(id=candidate_id, source_type="notion"),
            Relationship(label="implements", confidence=0.9),
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert provider.calls == []
        related = get_related_items(driver, source_id)
        assert len(related) == 1
        assert related[0].relationship.label == "implements"

    def test_a_candidate_already_related_in_the_reverse_direction_is_skipped(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest(conn, collection, ref="a", text="Building the storage layer")
        candidate_id = ingest(
            conn, collection, ref="b", text="Storage layer design notes"
        )
        # The candidate was processed first and wrote the edge the other way.
        write_relationship(
            driver,
            ItemNode(id=candidate_id, source_type="notion"),
            ItemNode(id=source_id, source_type="notion"),
            Relationship(label="implements", confidence=0.9),
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert provider.calls == []
        related = get_related_items(driver, source_id)
        assert len(related) == 1
        assert related[0].relationship.label == "implements"


class TestSourceChunkSelection:
    """The chunk of the *source* item shown to the LLM is per-candidate.

    Not always chunk 0 — picked by cosine similarity to each candidate's
    whole-document embedding. See DECISIONS.md, 2026-08-29.
    """

    def test_the_source_chunk_relevant_to_this_candidate_is_used_not_chunk_zero(
        self, conn, driver, collection, monkeypatch
    ):
        source_id = ingest_multi(
            conn,
            collection,
            ref="a",
            texts=[
                "Personal Knowledge Graph Agent — internal notes and misc "
                "reminders unrelated to any particular subsystem.",
                "The storage layer uses SQLite, Chroma, and Neo4j for local "
                "persistence of ingested items.",
            ],
        )
        ingest(
            conn,
            collection,
            ref="b",
            text="SQLite, Chroma, and Neo4j together form the storage layer "
            "that persists all ingested items locally.",
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        detect_relationships(conn, driver, collection, source_id)

        assert len(provider.calls) == 1
        source_text_shown_to_llm = provider.calls[0][0]
        assert "storage layer" in source_text_shown_to_llm
        assert "internal notes" not in source_text_shown_to_llm


class TestCandidateMaxDistance:
    """Candidates too far from the source, per the configured cutoff.

    Filtered out before ever reaching the LLM. See DECISIONS.md, 2026-08-29.
    """

    def test_a_candidate_farther_than_the_threshold_is_never_judged(
        self, conn, driver, collection, monkeypatch
    ):
        monkeypatch.setattr(
            "pipeline.relationships.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(
                    retrieval=SimpleNamespace(
                        relationship_candidate_count=5,
                        relationship_confidence_threshold=0.6,
                        relationship_candidate_max_distance=0.05,
                    )
                )
            ),
        )
        source_id = ingest(
            conn,
            collection,
            ref="a",
            text="The storage layer uses SQLite, Chroma, and Neo4j.",
        )
        ingest(
            conn,
            collection,
            ref="b",
            text="A traditional Italian pasta recipe needs fresh tomatoes, "
            "basil, garlic, and olive oil.",
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == []
        assert provider.calls == []

    def test_a_candidate_within_the_threshold_is_still_judged(
        self, conn, driver, collection, monkeypatch
    ):
        monkeypatch.setattr(
            "pipeline.relationships.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(
                    retrieval=SimpleNamespace(
                        relationship_candidate_count=5,
                        relationship_confidence_threshold=0.6,
                        relationship_candidate_max_distance=2.0,
                    )
                )
            ),
        )
        source_id = ingest(
            conn,
            collection,
            ref="a",
            text="The storage layer uses SQLite, Chroma, and Neo4j.",
        )
        candidate_id = ingest(
            conn,
            collection,
            ref="b",
            text="SQLite, Chroma, and Neo4j together form the storage layer.",
        )
        provider = FakeProvider(
            default=RelationshipJudgment(label="discussed_in", confidence=0.9)
        )
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        result = detect_relationships(conn, driver, collection, source_id)

        assert result == [(candidate_id, provider._default)]

    def test_a_null_threshold_disables_the_filter(
        self, conn, driver, collection, monkeypatch
    ):
        # The autouse set_candidate_count fixture already sets
        # max_distance=None; confirm a genuinely distant pair still gets
        # judged (not silently skipped) when the filter is off.
        source_id = ingest(
            conn,
            collection,
            ref="a",
            text="The storage layer uses SQLite, Chroma, and Neo4j.",
        )
        ingest(
            conn,
            collection,
            ref="b",
            text="A traditional Italian pasta recipe needs fresh tomatoes, "
            "basil, garlic, and olive oil.",
        )
        provider = FakeProvider(default=None)
        monkeypatch.setattr(
            "pipeline.relationships.get_provider", lambda task: provider
        )

        detect_relationships(conn, driver, collection, source_id)

        assert len(provider.calls) == 1


class TestMeanEmbedding:
    def test_averages_vectors_elementwise(self):
        assert _mean_embedding([[1.0, 0.0], [0.0, 2.0]]) == pytest.approx([0.5, 1.0])

    def test_empty_input_returns_none(self):
        assert _mean_embedding([]) is None
