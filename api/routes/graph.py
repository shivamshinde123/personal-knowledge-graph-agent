"""``GET /api/graph``: the whole relationship graph, for a frontend graph view.

Extension beyond ``docs/API_Specification.docx`` — see
``agent/graph_view.py``, ``DECISIONS.md``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from agent.graph_view import get_graph_snapshot
from api.schemas import GraphEdgeResponse, GraphNodeResponse, GraphResponse

router = APIRouter()


@router.get("/graph", response_model=GraphResponse)
def get_graph_route(request: Request) -> GraphResponse:
    """Return every item node and relationship edge currently in the graph."""
    snapshot = get_graph_snapshot(request.app.state.driver)
    return GraphResponse(
        nodes=[
            GraphNodeResponse(
                id=node.id,
                title=node.title,
                source_type=node.source_type,
                url=node.url,
            )
            for node in snapshot.nodes
        ],
        edges=[
            GraphEdgeResponse(
                source_id=edge.source_id,
                target_id=edge.target_id,
                label=edge.relationship.label,
                confidence=edge.relationship.confidence,
            )
            for edge in snapshot.edges
        ],
    )
