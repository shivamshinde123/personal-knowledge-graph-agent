"""Tests for agent/graph_view.py.

A thin pass-through to storage.neo4j_store.get_full_graph() — real Neo4j
integration test, same pattern as the rest of the agent-layer wrappers.
"""

import os
from urllib.parse import urlsplit

import neo4j
import pytest

from agent.graph_view import get_graph_snapshot
from storage.neo4j_store import (
    ItemNode,
    Relationship,
    ensure_constraints,
    get_driver,
    write_relationship,
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


def make_item(**overrides) -> ItemNode:
    defaults = dict(id="item-1", source_type="notion", title="A page")
    defaults.update(overrides)
    return ItemNode(**defaults)


class TestGetGraphSnapshot:
    def test_returns_the_real_graph(self, driver):
        a, b = make_item(id="a"), make_item(id="b")
        write_relationship(driver, a, b, Relationship(label="implements"))

        snapshot = get_graph_snapshot(driver)

        assert {n.id for n in snapshot.nodes} == {"a", "b"}
        assert len(snapshot.edges) == 1
        assert snapshot.edges[0].relationship.label == "implements"

    def test_empty_graph_returns_no_nodes_or_edges(self, driver):
        snapshot = get_graph_snapshot(driver)

        assert snapshot.nodes == []
        assert snapshot.edges == []
