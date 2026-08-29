"""Tests for the graph traversal node.

Real integration tests against a live Neo4j instance, same convention as
``tests/test_storage/test_neo4j_store.py`` — skipped automatically if no
server is reachable.
"""

import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import neo4j
import pytest

from agent.graph_traversal import graph_traversal
from agent.search_nodes import SearchHit
from storage.neo4j_store import (
    GraphStoreError,
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


def make_item(**overrides) -> ItemNode:
    defaults = dict(
        id="item-1",
        source_type="notion",
        title="Design notes",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return ItemNode(**defaults)


def link(driver, source_id, target_id, label="discussed_in"):
    write_relationship(
        driver,
        make_item(id=source_id),
        make_item(id=target_id),
        Relationship(label=label),
    )


class TestGraphTraversal:
    def test_finds_a_one_hop_neighbor(self, driver):
        link(driver, "item-a", "item-b")

        hits = graph_traversal(driver, [SearchHit(item_id="item-a", rank=1)])

        assert [h.item_id for h in hits] == ["item-b"]

    def test_finds_neighbors_reached_by_incoming_edges_too(self, driver):
        link(driver, "item-b", "item-a")

        hits = graph_traversal(driver, [SearchHit(item_id="item-a", rank=1)])

        assert [h.item_id for h in hits] == ["item-b"]

    def test_excludes_neighbors_already_in_the_seed_set(self, driver):
        link(driver, "item-a", "item-b")

        hits = graph_traversal(
            driver,
            [SearchHit(item_id="item-a", rank=1), SearchHit(item_id="item-b", rank=2)],
        )

        assert hits == []

    def test_an_item_with_no_relationships_yields_no_neighbors(self, driver):
        write_relationship(
            driver,
            make_item(id="item-a"),
            make_item(id="item-b"),
            Relationship(label="discussed_in"),
        )

        hits = graph_traversal(driver, [SearchHit(item_id="item-c", rank=1)])

        assert hits == []

    def test_ranks_by_number_of_distinct_seeds_connected_to(self, driver):
        # item-shared is connected to both seeds; item-solo only to one.
        link(driver, "item-seed-1", "item-shared")
        link(driver, "item-seed-2", "item-shared")
        link(driver, "item-seed-1", "item-solo")

        hits = graph_traversal(
            driver,
            [
                SearchHit(item_id="item-seed-1", rank=1),
                SearchHit(item_id="item-seed-2", rank=2),
            ],
        )

        assert [h.item_id for h in hits] == ["item-shared", "item-solo"]
        assert [h.rank for h in hits] == [1, 2]

    def test_a_failed_lookup_is_skipped_not_raised(self, driver, monkeypatch):
        link(driver, "item-a", "item-b")

        def boom(driver, item_id):
            raise GraphStoreError("boom")

        monkeypatch.setattr("agent.graph_traversal.get_related_items", boom)

        assert graph_traversal(driver, [SearchHit(item_id="item-a", rank=1)]) == []

    def test_empty_seed_hits_returns_no_neighbors(self, driver):
        assert graph_traversal(driver, []) == []
