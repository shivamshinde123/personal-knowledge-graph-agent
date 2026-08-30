"""Tests for agent/admin.py.

Real SQLite/Chroma/Neo4j integration test, same pattern as the rest of
the agent-layer wrappers — see tests/test_agent/test_graph_view.py.
"""

import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import neo4j
import pytest

from agent.admin import AdminError, reset_all_data
from storage.chroma_store import VectorChunk, get_collection, upsert_chunks
from storage.neo4j_store import (
    ItemNode,
    Relationship,
    ensure_constraints,
    get_driver,
    write_relationship,
)
from storage.neo4j_store import get_item as get_graph_item
from storage.sqlite_store import Item, connect, insert_item
from storage.sqlite_store import get_item as get_sqlite_item

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
def chroma_persist_dir(tmp_path):
    return tmp_path / "chroma"


@pytest.fixture
def collection(chroma_persist_dir):
    return get_collection(chroma_persist_dir)


def make_item(**overrides) -> ItemNode:
    defaults = dict(id="item-1", source_type="notion", title="A page")
    defaults.update(overrides)
    return ItemNode(**defaults)


class TestResetAllData:
    def test_wipes_all_three_stores(self, conn, collection, chroma_persist_dir, driver):
        insert_item(
            conn,
            Item(
                id="item-1",
                source_type="notion",
                source_ref_id="page-1",
                title="A page",
                url_or_path=None,
                author_or_sender=None,
                created_at=None,
                last_edited_at=None,
                ingested_at=datetime.now(UTC),
                project_name=None,
                topic=None,
            ),
        )
        upsert_chunks(
            collection,
            [
                VectorChunk(
                    id="emb-1",
                    embedding=[0.1, 0.2, 0.3],
                    document="text",
                    item_id="item-1",
                    source_type="notion",
                    project_name=None,
                    topic=None,
                    created_at=datetime.now(UTC),
                )
            ],
        )
        a, b = make_item(id="a"), make_item(id="b")
        write_relationship(driver, a, b, Relationship(label="implements"))

        new_collection = reset_all_data(conn, chroma_persist_dir, driver)

        assert get_sqlite_item(conn, "item-1") is None
        assert new_collection.count() == 0
        assert get_graph_item(driver, "a") is None

    def test_a_broken_sqlite_store_raises_admin_error(
        self, conn, chroma_persist_dir, driver
    ):
        # A missing table is a stand-in for any real reset_all() failure —
        # simplest way to make the SQLite side fail without also breaking
        # conn.rollback() itself (a closed connection can't even roll back).
        conn.execute("DROP TABLE items")

        with pytest.raises(AdminError, match="sqlite"):
            reset_all_data(conn, chroma_persist_dir, driver)
