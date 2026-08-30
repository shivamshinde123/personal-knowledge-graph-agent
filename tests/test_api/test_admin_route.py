"""Tests for POST /api/admin/reset.

Same reasoning as tests/test_api/test_graph_route.py: the shared ``driver``
fixture in conftest.py doesn't wipe Neo4j between tests, so this file
wipes around itself since it writes real graph data.
"""

from datetime import UTC, datetime

import pytest

from storage.chroma_store import VectorChunk, get_collection, upsert_chunks
from storage.neo4j_store import ItemNode, Relationship, write_relationship
from storage.sqlite_store import Item, get_item, insert_item


@pytest.fixture(autouse=True)
def clean_graph(driver):
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    yield
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def make_sqlite_item(**overrides) -> Item:
    defaults = dict(
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
    )
    defaults.update(overrides)
    return Item(**defaults)


class TestResetAllRoute:
    def test_requires_explicit_confirmation(self, client):
        response = client.post("/api/admin/reset", json={"confirm": False})

        assert response.status_code == 422
        assert response.json()["error"] == "confirmation_required"

    def test_wipes_all_three_stores_when_confirmed(
        self, client, conn, collection, chroma_persist_dir, driver
    ):
        insert_item(conn, make_sqlite_item())
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
        a = ItemNode(id="a", source_type="notion", title="A")
        b = ItemNode(id="b", source_type="github", title="B")
        write_relationship(driver, a, b, Relationship(label="implements"))

        response = client.post("/api/admin/reset", json={"confirm": True})

        assert response.status_code == 200
        assert response.json() == {"status": "reset"}
        assert get_item(conn, "item-1") is None
        # Resetting Chroma deletes and recreates the collection (see
        # storage/chroma_store.py::reset_all()) -- the `collection` fixture's
        # object no longer refers to a live collection, so re-open it fresh
        # from the same persist dir instead of reusing that reference.
        assert get_collection(chroma_persist_dir).count() == 0

        graph_response = client.get("/api/graph")
        assert graph_response.json() == {"nodes": [], "edges": []}
