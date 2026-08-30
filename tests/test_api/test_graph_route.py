"""Tests for GET /api/graph.

Unlike tests/test_storage/test_neo4j_store.py's own ``driver`` fixture,
``tests/test_api/conftest.py``'s ``driver`` deliberately doesn't wipe the
graph — most API tests don't touch it. This file writes real graph data,
so it wipes around itself explicitly instead.
"""

import pytest

from storage.neo4j_store import ItemNode, Relationship, write_relationship


def make_item(**overrides) -> ItemNode:
    defaults = dict(id="item-1", source_type="notion", title="A page")
    defaults.update(overrides)
    return ItemNode(**defaults)


@pytest.fixture(autouse=True)
def clean_graph(driver):
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    yield
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


class TestGetGraph:
    def test_returns_the_real_graph(self, driver, client):
        a = make_item(id="a", title="Storage design", source_type="notion")
        b = make_item(id="b", title="Storage implementation", source_type="github")
        write_relationship(
            driver, a, b, Relationship(label="implements", confidence=0.9)
        )

        response = client.get("/api/graph")

        assert response.status_code == 200
        body = response.json()
        assert {n["id"] for n in body["nodes"]} == {"a", "b"}
        node_a = next(n for n in body["nodes"] if n["id"] == "a")
        assert node_a["title"] == "Storage design"
        assert node_a["source_type"] == "notion"
        assert len(body["edges"]) == 1
        edge = body["edges"][0]
        assert edge["source_id"] == "a"
        assert edge["target_id"] == "b"
        assert edge["label"] == "implements"
        assert edge["confidence"] == 0.9

    def test_empty_graph_returns_no_nodes_or_edges(self, client):
        response = client.get("/api/graph")

        assert response.status_code == 200
        assert response.json() == {"nodes": [], "edges": []}
