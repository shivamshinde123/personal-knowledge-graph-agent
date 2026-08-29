"""``POST /api/ingest/trigger``: manually trigger an ingestion batch.

Per ``docs/API_Specification.docx`` section 3.8 — a manual override
outside the daily schedule, primarily for development and testing.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from agent.ingest_trigger import trigger_ingestion
from api.schemas import IngestTriggerResponse

router = APIRouter()


@router.post(
    "/ingest/trigger",
    response_model=IngestTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_ingestion_route() -> IngestTriggerResponse:
    """Spawn a daily batch run via ``agent/ingest_trigger.py``.

    Returns immediately (202 Accepted) without waiting for the run to
    finish — outcome is checked afterward via ``GET /api/sources/status``.
    """
    run_id = trigger_ingestion()
    return IngestTriggerResponse(status="started", run_id=run_id)
