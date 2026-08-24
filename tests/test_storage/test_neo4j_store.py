"""Tests for the Neo4j storage layer.

These are real integration tests against a live Neo4j instance — Neo4j
Community Edition has no embedded/in-process mode, unlike SQLite and Chroma.
They connect using NEO4J_TEST_URI/NEO4J_TEST_USER/NEO4J_TEST_PASSWORD (falling
back to the standard local defaults) and are skipped automatically if no
server is reachable.
"""

import os
from datetime import UTC, datetime

import neo4j
import pytest

from storage.neo4j_store import (
    GraphStoreError,
    ItemNode,
    Relationship,
    delete_item,
    ensure_constraints,
    get_driver,
    get_item,
    get_related_items,
    write_relationship,
)

TEST_URI = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
TEST_USER = os.environ.get("NEO4J_TEST_USER", "neo4j")
TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "testpassword123")


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
        project_name="pkg-agent",
        topic="architecture",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        url="https://notion.so/page-abc",
    )
    defaults.update(overrides)
    return ItemNode(**defaults)


class TestGetDriver:
    def test_connects_with_valid_credentials(self):
        d = get_driver(uri=TEST_URI, user=TEST_USER, password=TEST_PASSWORD)
        d.verify_connectivity()
        d.close()

    def test_invalid_credentials_raise_graph_store_error(self):
        with pytest.raises(GraphStoreError):
            get_driver(uri=TEST_URI, user=TEST_USER, password="wrong-password")


class TestWriteRelationship:
    def test_creates_both_endpoint_nodes(self, driver):
        source = make_item(id="item-1", title="Design notes")
        target = make_item(id="item-2", title="Follow-up email", source_type="gmail")

        write_relationship(driver, source, target, Relationship(label="discussed_in"))

        assert get_item(driver, "item-1").title == "Design notes"
        assert get_item(driver, "item-2").title == "Follow-up email"

    def test_omits_unset_optional_properties(self, driver):
        source = make_item(id="item-1", title=None, project_name=None, topic=None)
        target = make_item(id="item-2")

        write_relationship(driver, source, target, Relationship(label="relates"))

        stored = get_item(driver, "item-1")
        assert stored.title is None
        assert stored.project_name is None

    def test_rewriting_the_same_edge_updates_confidence_not_duplicates(self, driver):
        source = make_item(id="item-1")
        target = make_item(id="item-2")

        write_relationship(
            driver, source, target, Relationship(label="implements", confidence=0.5)
        )
        write_relationship(
            driver, source, target, Relationship(label="implements", confidence=0.9)
        )

        related = get_related_items(driver, "item-1")
        assert len(related) == 1
        assert related[0].relationship.confidence == 0.9

    def test_different_labels_between_same_items_create_separate_edges(self, driver):
        source = make_item(id="item-1")
        target = make_item(id="item-2")

        write_relationship(driver, source, target, Relationship(label="implements"))
        write_relationship(driver, source, target, Relationship(label="discussed_in"))

        related = get_related_items(driver, "item-1")
        assert {r.relationship.label for r in related} == {"implements", "discussed_in"}


class TestGetItem:
    def test_returns_none_for_an_item_with_no_relationship(self, driver):
        assert get_item(driver, "never-written") is None


class TestGetRelatedItems:
    def test_returns_outgoing_and_incoming_neighbors(self, driver):
        a, b, c = make_item(id="a"), make_item(id="b"), make_item(id="c")
        write_relationship(driver, a, b, Relationship(label="implements"))
        write_relationship(driver, c, a, Relationship(label="planned_in"))

        related = get_related_items(driver, "a")

        by_id = {r.item.id: r for r in related}
        assert by_id["b"].direction == "outgoing"
        assert by_id["c"].direction == "incoming"

    def test_item_with_no_relationships_has_no_neighbors(self, driver):
        assert get_related_items(driver, "never-written") == []


class TestDeleteItem:
    def test_removes_node_and_its_relationships(self, driver):
        source = make_item(id="item-1")
        target = make_item(id="item-2")
        write_relationship(driver, source, target, Relationship(label="implements"))

        delete_item(driver, "item-1")

        assert get_item(driver, "item-1") is None
        assert get_related_items(driver, "item-2") == []

    def test_deleting_a_nonexistent_item_is_harmless(self, driver):
        delete_item(driver, "does-not-exist")
