"""``POST /api/ingest/trigger``: manually trigger an ingestion batch.

Per ``docs/API_Specification.docx`` section 3.8 — a manual override
outside the daily schedule, primarily for development and testing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from agent.ingest_trigger import cancel_ingestion, trigger_ingestion
from api.schemas import IngestCancelRequest, IngestCancelResponse, IngestTriggerResponse

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


@router.post("/ingest/cancel", response_model=IngestCancelResponse)
def cancel_ingestion_route(
    payload: IngestCancelRequest, request: Request
) -> IngestCancelResponse:
    """Force-stop a running batch immediately.

    ``run_id`` is the ``ingestion_runs.id`` from
    ``GET /api/sources/status``'s ``last_run.run_id`` — not the display
    label ``POST /api/ingest/trigger`` returns (see ``agent/ingest_trigger.py``).
    Kills the process outright (see ``agent/ingest_trigger.py::cancel_ingestion()``
    for why a purely cooperative flag isn't enough on its own) — whatever
    was already processed and committed is kept, not rolled back.
    """
    cancel_ingestion(request.app.state.conn, payload.run_id)
    return IngestCancelResponse(status="cancel_requested")
