"""``GET /api/health``: reports the operational status of every backing service.

Used by the frontend to show a status indicator, and by the user for
troubleshooting — per ``docs/API_Specification.docx`` section 3.2.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from agent.health import check_health
from api.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    """Check every backing service via ``agent/health.py::check_health()``."""
    status = check_health(
        request.app.state.conn, request.app.state.collection, request.app.state.driver
    )
    return HealthResponse(status=status.status, services=status.services)
