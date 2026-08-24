"""Integration tests for the daily batch orchestrator.

Runs the real pipeline end to end: SQLite (``:memory:``), an embedded
Chroma collection, the real embedding model, a real local-files extraction
pass against temp files, and a live Neo4j instance (skipped automatically
if none is reachable, same as the storage/pipeline test suites). Only the
LLM provider is faked, for deterministic metadata and relationship
judgments.
"""

import os
from types import SimpleNamespace
from urllib.parse import urlsplit

import neo4j
import pytest

import scheduler.daily_batch as daily_batch
from extractors import local_files
from extractors.base import ExtractorError
from providers.base import ItemMetadata, RelationshipJudgment
from storage.chroma_store import get_collection
from storage.neo4j_store import ensure_constraints, get_driver, get_related_items
from storage.sqlite_store import (
    connect,
    get_chunks_for_item,
    get_item,
    get_last_run_timestamp,
    keyword_search,
)

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


class FakeMetadataProvider:
    """A fake ProviderInterface returning the same metadata for every item."""

    def __init__(self, metadata=None):
        """Use ``metadata`` for every item, or a default if omitted."""
        self._metadata = metadata or ItemMetadata(
            project_name="pkg-agent", topic="storage"
        )

    def generate_metadata(self, texts):
        return [self._metadata for _ in texts]


class FakeRelationshipProvider:
    """A fake ProviderInterface returning the same relationship judgment always."""

    def __init__(self, judgment=None):
        """Use ``judgment`` for every candidate, or a default if omitted."""
        self._judgment = judgment or RelationshipJudgment(
            label="discussed_in", confidence=0.9
        )

    def generate_relationship(self, source_text, candidate_text):
        return self._judgment


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


@pytest.fixture
def watch_dir(tmp_path, monkeypatch):
    directory = tmp_path / "watched"
    directory.mkdir()
    monkeypatch.setattr(
        "extractors.local_files.get_settings",
        lambda: SimpleNamespace(env=SimpleNamespace(watch_dirs=[directory])),
    )
    return directory


@pytest.fixture(autouse=True)
def fake_providers(monkeypatch):
    monkeypatch.setattr(
        "pipeline.metadata.get_provider", lambda task: FakeMetadataProvider()
    )
    monkeypatch.setattr(
        "pipeline.relationships.get_provider", lambda task: FakeRelationshipProvider()
    )
    monkeypatch.setattr(
        "pipeline.relationships.get_settings",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                retrieval=SimpleNamespace(relationship_candidate_count=5)
            )
        ),
    )


class TestFullRun:
    @pytest.fixture(autouse=True)
    def local_files_only(self, monkeypatch):
        """Restrict the real run to the local-files extractor.

        This class exercises the local-files ingestion path specifically
        (per the module docstring); without this, the real, unmocked
        ``notion.extract_new_items`` in ``_EXTRACTORS`` would hit the
        actual Notion API using whatever ``NOTION_API_KEY`` happens to be
        configured on the machine running the tests.
        """
        monkeypatch.setattr(
            daily_batch, "_EXTRACTORS", [("local_file", local_files.extract_new_items)]
        )

    def test_ingests_files_end_to_end(self, conn, driver, collection, watch_dir):
        (watch_dir / "a.txt").write_text("Building the storage layer", encoding="utf-8")
        (watch_dir / "b.txt").write_text(
            "Storage layer design notes and decisions", encoding="utf-8"
        )

        daily_batch._run(conn, collection, driver)

        rows = conn.execute("SELECT * FROM items").fetchall()
        assert len(rows) == 2
        for row in rows:
            item = get_item(conn, row["id"])
            assert item.project_name == "pkg-agent"
            assert item.topic == "storage"
            chunks = get_chunks_for_item(conn, item.id)
            assert len(chunks) == 1
            assert collection.get(ids=[chunks[0].embedding_id])["ids"]

    def test_keyword_search_finds_ingested_content(
        self, conn, driver, collection, watch_dir
    ):
        (watch_dir / "a.txt").write_text("A unique phrase xyzzy", encoding="utf-8")

        daily_batch._run(conn, collection, driver)

        results = keyword_search(conn, "xyzzy")
        assert len(results) == 1

    def test_writes_a_confirmed_relationship_between_ingested_items(
        self, conn, driver, collection, watch_dir
    ):
        (watch_dir / "a.txt").write_text("Building the storage layer", encoding="utf-8")
        (watch_dir / "b.txt").write_text(
            "Storage layer design notes and decisions", encoding="utf-8"
        )

        daily_batch._run(conn, collection, driver)

        rows = conn.execute("SELECT id FROM items").fetchall()
        related_somewhere = any(get_related_items(driver, row["id"]) for row in rows)
        assert related_somewhere

    def test_run_completes_as_success_and_advances_the_watermark(
        self, conn, driver, collection, watch_dir
    ):
        (watch_dir / "a.txt").write_text(
            "Some content here that is long enough", encoding="utf-8"
        )

        daily_batch._run(conn, collection, driver)

        run = conn.execute(
            "SELECT * FROM ingestion_runs ORDER BY run_started_at DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "success"
        assert run["items_processed"] == 1
        assert get_last_run_timestamp(conn) is not None

    def test_no_matching_files_is_still_a_successful_empty_run(
        self, conn, driver, collection, watch_dir
    ):
        daily_batch._run(conn, collection, driver)

        run = conn.execute(
            "SELECT * FROM ingestion_runs ORDER BY run_started_at DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "success"
        assert run["items_processed"] == 0


class TestFailureHandling:
    @pytest.fixture(autouse=True)
    def local_files_only(self, monkeypatch):
        """See ``TestFullRun.local_files_only`` — same reasoning applies here."""
        monkeypatch.setattr(
            daily_batch, "_EXTRACTORS", [("local_file", local_files.extract_new_items)]
        )

    def test_extractor_level_failure_leaves_watermark_unchanged(
        self, conn, driver, collection, monkeypatch
    ):
        def boom(since):
            raise ExtractorError("source is unreachable")

        monkeypatch.setattr(daily_batch, "_EXTRACTORS", [("local_file", boom)])

        daily_batch._run(conn, collection, driver)

        run = conn.execute(
            "SELECT * FROM ingestion_runs ORDER BY run_started_at DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "failed"
        assert run["items_processed"] == 0
        assert "unreachable" in run["error_log"]
        assert get_last_run_timestamp(conn) is None

    def test_one_bad_item_does_not_stop_the_rest(
        self, conn, driver, collection, watch_dir, monkeypatch
    ):
        (watch_dir / "good.txt").write_text(
            "This one works just fine, no issues here", encoding="utf-8"
        )
        (watch_dir / "bad.txt").write_text(
            "This one will explode during chunking", encoding="utf-8"
        )

        real_chunk_text = daily_batch.chunk_text

        def flaky_chunk_text(text):
            if "explode" in text:
                raise RuntimeError("simulated chunking failure")
            return real_chunk_text(text)

        monkeypatch.setattr(daily_batch, "chunk_text", flaky_chunk_text)

        daily_batch._run(conn, collection, driver)

        run = conn.execute(
            "SELECT * FROM ingestion_runs ORDER BY run_started_at DESC LIMIT 1"
        ).fetchone()
        assert run["status"] == "partial_failure"
        assert run["items_processed"] == 1
        assert "bad.txt" in run["error_log"]
        rows = conn.execute("SELECT title FROM items").fetchall()
        assert [r["title"] for r in rows] == ["good.txt"]
