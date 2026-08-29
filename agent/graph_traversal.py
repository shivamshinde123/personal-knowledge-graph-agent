"""Graph Traversal Node: pulls relationship-connected items from Neo4j.

Per ``docs/Component_Map.docx``: ``GraphTraversalNode`` calls only
``Neo4jStore``. Per ``docs/Technical_Design_Document.docx`` section 8.2
step 3 ("connected nodes are pulled from Neo4j via graph traversal"),
traversal expands outward from items already found by vector/keyword
search — a natural-language question has no entry point of its own into
the graph, so this node only ever runs alongside (never instead of) the
search nodes, and only when ``agent/router.py::route()`` decides
relationships are relevant to the question.
"""

from __future__ import annotations

import logging

import neo4j

from agent.search_nodes import SearchHit
from storage.neo4j_store import GraphStoreError, get_related_items

logger = logging.getLogger(__name__)


def graph_traversal(
    driver: neo4j.Driver, seed_hits: list[SearchHit]
) -> list[SearchHit]:
    """Expand a set of search hits with their one-hop graph neighbors.

    Args:
        driver: An open Neo4j driver.
        seed_hits: Hits already found by vector/keyword search, to expand
            from.

    Returns:
        Newly-discovered neighboring items — excluding anything already
        present in ``seed_hits`` — ranked by how many distinct seed items
        they're connected to (an item connected to several of the seeds is
        more likely relevant than one connected to only one), with ties
        broken by the best (lowest) rank among the seeds that reached it.
        A seed whose neighbor lookup fails is logged and skipped, rather
        than aborting traversal for the rest — one bad lookup shouldn't
        cost every other seed's neighbors.
    """
    seed_ids = {hit.item_id for hit in seed_hits}
    connection_counts: dict[str, int] = {}
    best_seed_rank: dict[str, int] = {}

    for seed in seed_hits:
        try:
            related = get_related_items(driver, seed.item_id)
        except GraphStoreError as exc:
            logger.warning("Graph traversal failed for %r: %s", seed.item_id, exc)
            continue
        for related_item in related:
            neighbor_id = related_item.item.id
            if neighbor_id in seed_ids:
                continue
            connection_counts[neighbor_id] = connection_counts.get(neighbor_id, 0) + 1
            best_seed_rank[neighbor_id] = min(
                best_seed_rank.get(neighbor_id, seed.rank), seed.rank
            )

    ordered_ids = sorted(
        connection_counts,
        key=lambda item_id: (-connection_counts[item_id], best_seed_rank[item_id]),
    )
    return [
        SearchHit(item_id=item_id, rank=rank)
        for rank, item_id in enumerate(ordered_ids, start=1)
    ]
