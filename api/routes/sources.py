"""``GET /api/sources/status``: the most recent daily batch run, per source.

Per ``docs/API_Specification.docx`` section 3.3, for display on the
Settings screen.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from agent.sources_status import get_sources_status
from api.schemas import LastRunResponse, SourcesStatusResponse, SourceStatusResponse

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
                status=source.status,
            )
            for source in result.sources
        ],
    )
