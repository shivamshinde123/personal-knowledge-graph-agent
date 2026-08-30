"""Neo4j storage: the relationship graph between items.

This module owns the graph store defined in ``docs/Database_Schema.docx``.
It belongs to the storage layer: it imports only ``config.settings`` and
holds the only live Neo4j driver in the system, per
``docs/Component_Map.docx`` and ``docs/Coding_Conventions.docx``.

Per ``docs/Database_Schema.docx`` section 5, an item is only written to
Neo4j once it has at least one confirmed relationship — items with no
detected relationships live in SQLite and Chroma only. Accordingly, this
module has no standalone "create an item node" function; ``write_relationship``
creates (or updates) both endpoint nodes as part of writing the edge between
them.

Typical use::

    from storage.neo4j_store import get_driver, ensure_constraints, write_relationship

    driver = get_driver()
    ensure_constraints(driver)
    write_relationship(driver, source_item, target_item, relationship)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import neo4j

from config.settings import get_settings


class GraphStoreError(Exception):
    """Raised when a Neo4j storage operation fails."""


# Neo4jError covers server-side failures (auth, constraint violations, bad
# Cypher); DriverError covers client-side connectivity failures (e.g. the
# server being unreachable). Both can surface from any driver call below.
_NEO4J_ERRORS = (neo4j.exceptions.Neo4jError, neo4j.exceptions.DriverError)


@dataclass(slots=True)
class ItemNode:
    """A node in the relationship graph, mirroring ``items`` in SQLite.

    ``id`` matches ``items.id`` in SQLite, per the cross-store consistency
    rules in ``docs/Database_Schema.docx`` section 5.
    """

    id: str
    source_type: str
    title: str | None = None
    project_name: str | None = None
    topic: str | None = None
    created_at: datetime | None = None
    url: str | None = None


@dataclass(slots=True)
class Relationship:
    """A ``RELATES_TO`` edge between two items."""

    label: str
    confidence: float | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class RelatedItem:
    """A neighboring item reached by one ``RELATES_TO`` hop."""

    item: ItemNode
    relationship: Relationship
    direction: Literal["outgoing", "incoming"]


@dataclass(slots=True)
class GraphEdge:
    """One directed ``RELATES_TO`` edge, for a whole-graph snapshot."""

    source_id: str
    target_id: str
    relationship: Relationship


@dataclass(slots=True)
class GraphSnapshot:
    """Every item node and relationship edge currently in the graph."""

    nodes: list[ItemNode]
    edges: list[GraphEdge]


def _item_properties(item: ItemNode) -> dict[str, object]:
    return {
        "id": item.id,
        "source_type": item.source_type,
        "title": item.title,
        "project_name": item.project_name,
        "topic": item.topic,
        "created_at": item.created_at,
        "url": item.url,
    }


def _to_datetime(value: object) -> datetime | None:
    """Convert a value read back from Neo4j into a standard-library datetime.

    Temporal properties come back as ``neo4j.time.DateTime``, not
    ``datetime.datetime`` — the driver's own type, not the stdlib one — so
    it's converted via ``to_native()`` to satisfy the ``datetime | None``
    type on :class:`ItemNode` and :class:`Relationship`.
    """
    if value is None or isinstance(value, datetime):
        return value
    return value.to_native()


def _item_from_node(node: neo4j.graph.Node) -> ItemNode:
    return ItemNode(
        id=node["id"],
        source_type=node["source_type"],
        title=node.get("title"),
        project_name=node.get("project_name"),
        topic=node.get("topic"),
        created_at=_to_datetime(node.get("created_at")),
        url=node.get("url"),
    )


def get_driver(
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> neo4j.Driver:
    """Open a Neo4j driver using the configured (or given) connection details.

    Args:
        uri: Bolt URI. Defaults to ``settings.env.neo4j_uri``.
        user: Username. Defaults to ``settings.env.neo4j_user``.
        password: Password. Defaults to ``settings.env.neo4j_password``.

    Returns:
        An open driver. Callers are responsible for closing it (or using it
        as a context manager) when done.

    Raises:
        GraphStoreError: If the driver cannot connect.
    """
    env = get_settings().env
    resolved_uri = env.neo4j_uri if uri is None else uri
    resolved_user = env.neo4j_user if user is None else user
    resolved_password = env.neo4j_password if password is None else password
    try:
        driver = neo4j.GraphDatabase.driver(
            resolved_uri, auth=(resolved_user, resolved_password)
        )
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(
            f"Could not connect to Neo4j at {resolved_uri!r}: {exc}"
        ) from exc
    try:
        driver.verify_connectivity()
    except _NEO4J_ERRORS as exc:
        driver.close()
        raise GraphStoreError(
            f"Could not connect to Neo4j at {resolved_uri!r}: {exc}"
        ) from exc
    return driver


def ensure_constraints(driver: neo4j.Driver) -> None:
    """Create the schema's constraint and index if they don't already exist.

    Idempotent — safe to call on every process start.

    Args:
        driver: An open driver from :func:`get_driver`.

    Raises:
        GraphStoreError: If the constraint/index creation fails.
    """
    try:
        with driver.session() as session:
            session.run(
                "CREATE CONSTRAINT item_id_unique IF NOT EXISTS "
                "FOR (i:Item) REQUIRE i.id IS UNIQUE"
            )
            session.run(
                "CREATE INDEX item_project_name IF NOT EXISTS "
                "FOR (i:Item) ON (i.project_name)"
            )
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(f"Could not create schema constraints: {exc}") from exc


def write_relationship(
    driver: neo4j.Driver,
    source: ItemNode,
    target: ItemNode,
    relationship: Relationship,
) -> None:
    """Create or update a ``RELATES_TO`` edge, creating both endpoint nodes.

    Both items are upserted (created if missing, refreshed if present) since
    a relationship is the trigger for an item's first appearance in the
    graph. Calling this again for the same ``(source.id, target.id, label)``
    updates the existing edge's ``confidence`` rather than creating a
    duplicate.

    Args:
        driver: An open driver from :func:`get_driver`.
        source: The relationship's source item.
        target: The relationship's target item.
        relationship: The edge to write.

    Raises:
        GraphStoreError: If the write fails.
    """
    query = """
    MERGE (a:Item {id: $source.id})
    SET a += $source
    MERGE (b:Item {id: $target.id})
    SET b += $target
    MERGE (a)-[r:RELATES_TO {label: $label}]->(b)
    ON CREATE SET r.created_at = $created_at
    SET r.confidence = $confidence
    """
    try:
        with driver.session() as session:
            session.run(
                query,
                source=_item_properties(source),
                target=_item_properties(target),
                label=relationship.label,
                confidence=relationship.confidence,
                created_at=relationship.created_at,
            )
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(
            f"Could not write relationship {source.id!r} -{relationship.label}-> "
            f"{target.id!r}: {exc}"
        ) from exc


def has_any_relationship(driver: neo4j.Driver, item_a: str, item_b: str) -> bool:
    """Whether any ``RELATES_TO`` edge already connects two items, either direction.

    Used by ``pipeline/relationships.py`` to avoid re-judging (and
    re-writing) a pair that's already related — relationship detection runs
    per newly-processed item, so without this check, item A being processed
    can write ``A -[label]-> B``, and later item B being independently
    processed can write a second, separate ``B -[label]-> A`` edge for what
    is really the same discovered relationship. See ``DECISIONS.md``.

    Args:
        driver: An open driver from :func:`get_driver`.
        item_a: One item's id.
        item_b: The other item's id.

    Returns:
        ``True`` if a ``RELATES_TO`` edge exists between them in either
        direction, regardless of label.

    Raises:
        GraphStoreError: If the query fails.
    """
    query = """
    MATCH (a:Item {id: $item_a})-[:RELATES_TO]-(b:Item {id: $item_b})
    RETURN count(*) > 0 AS connected
    """
    try:
        with driver.session() as session:
            record = session.run(query, item_a=item_a, item_b=item_b).single()
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(
            f"Could not check for an existing relationship between "
            f"{item_a!r} and {item_b!r}: {exc}"
        ) from exc
    return bool(record is not None and record["connected"])


def get_item(driver: neo4j.Driver, item_id: str) -> ItemNode | None:
    """Fetch a single item node by id.

    Args:
        driver: An open driver from :func:`get_driver`.
        item_id: The item's id.

    Returns:
        The item, or ``None`` if it has no node in the graph (it may still
        exist in SQLite and Chroma without any confirmed relationship).
    """
    try:
        with driver.session() as session:
            record = session.run(
                "MATCH (i:Item {id: $id}) RETURN i", id=item_id
            ).single()
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(f"Could not fetch item {item_id!r}: {exc}") from exc
    return None if record is None else _item_from_node(record["i"])


def get_related_items(driver: neo4j.Driver, item_id: str) -> list[RelatedItem]:
    """Fetch an item's direct (one-hop) neighbors in either direction.

    Args:
        driver: An open driver from :func:`get_driver`.
        item_id: The item whose neighbors to fetch.

    Returns:
        Related items with the connecting relationship and its direction
        relative to ``item_id``.

    Raises:
        GraphStoreError: If the query fails.
    """
    query = """
    MATCH (i:Item {id: $id})-[r:RELATES_TO]->(other:Item)
    RETURN other, r, 'outgoing' AS direction
    UNION
    MATCH (i:Item {id: $id})<-[r:RELATES_TO]-(other:Item)
    RETURN other, r, 'incoming' AS direction
    """
    try:
        with driver.session() as session:
            records = list(session.run(query, id=item_id))
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(
            f"Could not fetch neighbors of {item_id!r}: {exc}"
        ) from exc
    return [
        RelatedItem(
            item=_item_from_node(record["other"]),
            relationship=Relationship(
                label=record["r"]["label"],
                confidence=record["r"].get("confidence"),
                created_at=_to_datetime(record["r"].get("created_at")),
            ),
            direction=record["direction"],
        )
        for record in records
    ]


def get_full_graph(driver: neo4j.Driver) -> GraphSnapshot:
    """Fetch every item node and relationship edge currently in the graph.

    Per ``docs/Database_Schema.docx`` section 5, only items with at least
    one confirmed relationship have a node here at all — an item that's
    never been related to anything simply doesn't appear, which is exactly
    the useful subset for a relationship *graph* view (an unconnected item
    wouldn't add anything to one). Used by ``agent/graph_view.py`` for the
    frontend's graph visualization.

    No pagination or size limit — appropriate for this project's scale
    (a single user's personal data), not a general-purpose graph API. If
    the graph grows large enough for this to matter, this is the function
    to revisit.

    Args:
        driver: An open driver from :func:`get_driver`.

    Returns:
        Every node and edge, unfiltered. A node with no edges cannot exist
        (per the invariant above), but a fetched graph could still be
        empty if nothing has been related yet.

    Raises:
        GraphStoreError: If the query fails.
    """
    try:
        with driver.session() as session:
            node_records = list(session.run("MATCH (i:Item) RETURN i"))
            edge_records = list(
                session.run(
                    "MATCH (a:Item)-[r:RELATES_TO]->(b:Item) "
                    "RETURN a.id AS source_id, b.id AS target_id, r"
                )
            )
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(f"Could not fetch the full graph: {exc}") from exc
    nodes = [_item_from_node(record["i"]) for record in node_records]
    edges = [
        GraphEdge(
            source_id=record["source_id"],
            target_id=record["target_id"],
            relationship=Relationship(
                label=record["r"]["label"],
                confidence=record["r"].get("confidence"),
                created_at=_to_datetime(record["r"].get("created_at")),
            ),
        )
        for record in edge_records
    ]
    return GraphSnapshot(nodes=nodes, edges=edges)


def delete_relationships_for_item(driver: neo4j.Driver, item_id: str) -> None:
    """Delete every ``RELATES_TO`` edge touching an item, keeping its node.

    Used when an already-related item is edited and re-ingested: its
    existing relationships were judged against the *old* content, and
    ``has_any_relationship()`` would otherwise skip re-judging any
    candidate it was already connected to, silently freezing a possibly
    stale edge forever. Called before relationship detection re-runs for
    an updated item, so re-detection starts clean. See ``DECISIONS.md``.

    Unlike :func:`delete_item`, the node itself (and its
    ``title``/``project_name``/etc. properties) is left in place — only
    its edges are removed. A no-op if the item has no edges (or no node).

    Args:
        driver: An open driver from :func:`get_driver`.
        item_id: The item whose relationships should be cleared.

    Raises:
        GraphStoreError: If the delete fails.
    """
    try:
        with driver.session() as session:
            session.run(
                "MATCH (i:Item {id: $id})-[r:RELATES_TO]-() DELETE r", id=item_id
            )
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(
            f"Could not delete relationships for item {item_id!r}: {exc}"
        ) from exc


def delete_item(driver: neo4j.Driver, item_id: str) -> None:
    """Delete an item node and all of its relationships.

    SQLite's ``delete_item()`` cascades to ``chunks`` on its own; per
    ``docs/Database_Schema.docx`` section 5, callers deleting an item must
    call this too, since Neo4j doesn't enforce foreign keys against SQLite.
    A no-op if the item has no graph node.

    Args:
        driver: An open driver from :func:`get_driver`.
        item_id: The item's id.

    Raises:
        GraphStoreError: If the delete fails.
    """
    try:
        with driver.session() as session:
            session.run("MATCH (i:Item {id: $id}) DETACH DELETE i", id=item_id)
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(f"Could not delete item {item_id!r}: {exc}") from exc


def reset_all(driver: neo4j.Driver) -> None:
    """Delete every node and relationship in the graph.

    A no-op on an already-empty graph. See ``agent/admin.py``,
    ``DECISIONS.md``.

    Args:
        driver: An open driver from :func:`get_driver`.

    Raises:
        GraphStoreError: If the delete fails.
    """
    try:
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
    except _NEO4J_ERRORS as exc:
        raise GraphStoreError(f"Could not reset the graph: {exc}") from exc
