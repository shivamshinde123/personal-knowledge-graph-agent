"""``GET /api/sources/status``: the most recent daily batch run, per source.

Per ``docs/API_Specification.docx`` section 3.3, for display on the
Settings screen. Also ``GET``/``POST /api/sources/connections`` — live
per-source connection checks, an extension beyond the original spec (see
``agent/connection_check.py``, ``DECISIONS.md``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from agent.connection_check import get_connection_status
from agent.sources_status import get_sources_status
from api.schemas import (
    ConnectionsResponse,
    ConnectionStatusResponse,
    LastRunResponse,
    SourcesStatusResponse,
    SourceStatusResponse,
)

router = APIRouter()


@router.get("/sources/status", response_model=SourcesStatusResponse)
def get_sources_status_route(request: Request) -> SourcesStatusResponse:
    """Report the most recent batch run's outcome via ``agent/sources_status.py``."""
    result = get_sources_status(request.app.state.conn)
    last_run = (
        LastRunResponse(
            run_id=result.last_run.id,
            started_at=result.last_run.run_started_at,
            completed_at=result.last_run.run_completed_at,
            status=result.last_run.status,
            items_processed=result.last_run.items_processed,
            current_item=result.last_run.current_item,
        )
        if result.last_run is not None
        else None
    )
    return SourcesStatusResponse(
        last_run=last_run,
        sources=[
            SourceStatusResponse(
                source_type=source.source_type,
                items_processed=source.items_processed,
                total_items=source.total_items,
                status=source.status,
            )
            for source in result.sources
        ],
    )


@router.get("/sources/connections", response_model=ConnectionsResponse)
def get_connections_route() -> ConnectionsResponse:
    """Report each source's live connection status, using the cache if fresh."""
    return _to_connections_response(get_connection_status())


@router.post("/sources/connections/verify", response_model=ConnectionsResponse)
def verify_connections_route() -> ConnectionsResponse:
    """Force a fresh check of every source's connection ("Reverify" button)."""
    return _to_connections_response(get_connection_status(force_refresh=True))


def _to_connections_response(connections) -> ConnectionsResponse:
    return ConnectionsResponse(
        connections=[
            ConnectionStatusResponse(
                source_type=c.source_type,
                status=c.status,
                detail=c.detail,
                checked_at=c.checked_at,
            )
            for c in connections
        ]
    )
