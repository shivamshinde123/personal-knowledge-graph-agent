"""Tests for the Neo4j storage layer.

These are real integration tests against a live Neo4j instance — Neo4j
Community Edition has no embedded/in-process mode, unlike SQLite and Chroma.
They connect using NEO4J_TEST_URI/NEO4J_TEST_USER/NEO4J_TEST_PASSWORD (falling
back to the standard local defaults) and are skipped automatically if no
server is reachable.
"""

import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import neo4j
import pytest

from storage.neo4j_store import (
    GraphStoreError,
    ItemNode,
    Relationship,
    delete_item,
    delete_relationships_for_item,
    ensure_constraints,
    get_driver,
    get_full_graph,
    get_item,
    get_related_items,
    has_any_relationship,
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
        "the entire database on every run. Set "
        "NEO4J_TEST_ALLOW_NONLOCAL_WIPE=1 to opt in if that's intentional.",
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

    def test_created_at_round_trips_as_a_stdlib_datetime(self, driver):
        source = make_item(id="item-1", created_at=datetime(2026, 8, 1, tzinfo=UTC))
        target = make_item(id="item-2")

        write_relationship(
            driver,
            source,
            target,
            Relationship(
                label="implements", created_at=datetime(2026, 8, 3, tzinfo=UTC)
            ),
        )

        stored = get_item(driver, "item-1")
        assert type(stored.created_at) is datetime
        assert stored.created_at == datetime(2026, 8, 1, tzinfo=UTC)

        related = get_related_items(driver, "item-1")
        assert type(related[0].relationship.created_at) is datetime
        assert related[0].relationship.created_at == datetime(2026, 8, 3, tzinfo=UTC)

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


class TestGetFullGraph:
    def test_empty_graph_returns_no_nodes_or_edges(self, driver):
        snapshot = get_full_graph(driver)

        assert snapshot.nodes == []
        assert snapshot.edges == []

    def test_returns_every_node_and_edge(self, driver):
        a, b, c = make_item(id="a"), make_item(id="b"), make_item(id="c")
        write_relationship(driver, a, b, Relationship(label="implements"))
        write_relationship(driver, b, c, Relationship(label="discussed_in"))

        snapshot = get_full_graph(driver)

        assert {n.id for n in snapshot.nodes} == {"a", "b", "c"}
        assert len(snapshot.edges) == 2
        by_pair = {
            (e.source_id, e.target_id): e.relationship.label for e in snapshot.edges
        }
        assert by_pair[("a", "b")] == "implements"
        assert by_pair[("b", "c")] == "discussed_in"

    def test_edge_carries_its_confidence_and_created_at(self, driver):
        a, b = make_item(id="a"), make_item(id="b")
        write_relationship(
            driver,
            a,
            b,
            Relationship(
                label="implements",
                confidence=0.85,
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
        )

        snapshot = get_full_graph(driver)

        edge = snapshot.edges[0]
        assert edge.relationship.confidence == 0.85
        assert edge.relationship.created_at == datetime(2026, 8, 1, tzinfo=UTC)


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


class TestDeleteRelationshipsForItem:
    def test_removes_outgoing_and_incoming_edges_but_keeps_the_node(self, driver):
        a, b, c = make_item(id="a"), make_item(id="b"), make_item(id="c")
        write_relationship(driver, a, b, Relationship(label="implements"))
        write_relationship(driver, c, a, Relationship(label="planned_in"))

        delete_relationships_for_item(driver, "a")

        assert get_related_items(driver, "a") == []
        assert get_item(driver, "a") is not None
        # Unrelated to "a"'s edges specifically — "b" and "c" nodes untouched.
        assert get_item(driver, "b") is not None
        assert get_item(driver, "c") is not None

    def test_does_not_affect_other_items_relationships(self, driver):
        a, b, c = make_item(id="a"), make_item(id="b"), make_item(id="c")
        write_relationship(driver, a, b, Relationship(label="implements"))
        write_relationship(driver, b, c, Relationship(label="discussed_in"))

        delete_relationships_for_item(driver, "a")

        related_to_b = {r.item.id for r in get_related_items(driver, "b")}
        assert related_to_b == {"c"}

    def test_an_item_with_no_relationships_or_no_node_is_harmless(self, driver):
        delete_relationships_for_item(driver, "does-not-exist")


class TestHasAnyRelationship:
    def test_false_when_no_edge_exists(self, driver):
        assert has_any_relationship(driver, "a", "b") is False

    def test_true_for_the_written_direction(self, driver):
        a, b = make_item(id="a"), make_item(id="b")
        write_relationship(driver, a, b, Relationship(label="implements"))

        assert has_any_relationship(driver, "a", "b") is True

    def test_true_regardless_of_which_id_is_passed_first(self, driver):
        a, b = make_item(id="a"), make_item(id="b")
        write_relationship(driver, a, b, Relationship(label="implements"))

        assert has_any_relationship(driver, "b", "a") is True

    def test_false_for_unrelated_pair_even_if_both_have_other_edges(self, driver):
        a, b = make_item(id="a"), make_item(id="b")
        write_relationship(driver, a, b, Relationship(label="implements"))

        assert has_any_relationship(driver, "a", "c") is False
        assert has_any_relationship(driver, "b", "c") is False
