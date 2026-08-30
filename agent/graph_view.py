"""Graph view: the whole relationship graph, for frontend visualization.

Lives in the agent layer, not ``api/``, for the same reason
``agent/health.py``/``agent/sources_status.py``/``agent/connection_check.py``
do — the API layer depends only on the agent's public entrypoints. See
``DECISIONS.md``.

Distinct from ``agent/graph_traversal.py``, which is the LangGraph node
used internally during query answering (a one-hop lookup seeded from
search results) — this is a whole-graph snapshot for a user-facing graph
view, not part of the query path.
"""

from __future__ import annotations

import neo4j

from storage.neo4j_store import GraphSnapshot, get_full_graph


def get_graph_snapshot(driver: neo4j.Driver) -> GraphSnapshot:
    """Return every item node and relationship edge currently in the graph.

    Args:
        driver: An open Neo4j driver.

    Returns:
        The full graph snapshot — see
        :func:`storage.neo4j_store.get_full_graph`.

    Raises:
        GraphStoreError: If the query fails.
    """
    return get_full_graph(driver)
