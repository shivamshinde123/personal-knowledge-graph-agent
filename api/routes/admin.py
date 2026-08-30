"""``POST /api/admin/reset``: wipe SQLite, Chroma, and Neo4j back to empty.

Extension beyond ``docs/API_Specification.docx`` — a destructive,
hard-to-reverse operation, requested directly for local development/
testing (recovering from a bad ingestion run, or restarting a demo from a
clean slate). See ``agent/admin.py``, ``DECISIONS.md``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent.admin import AdminError, reset_all_data
from api.schemas import AdminResetRequest, AdminResetResponse

router = APIRouter()


@router.post("/admin/reset", response_model=AdminResetResponse)
def reset_all_route(payload: AdminResetRequest, request: Request):
    """Wipe every store to empty. Requires ``{"confirm": true}`` in the body.

    A rejected confirmation and a partial reset (some stores wiped, others
    not — see ``agent/admin.py::AdminError``) are both returned shaped like
    every other error, per ``docs/API_Specification.docx`` section 2,
    rather than via ``HTTPException`` (see ``api/routes/sessions.py`` for
    the same reasoning).
    """
    if not payload.confirm:
        return JSONResponse(
            status_code=422,
            content={
                "error": "confirmation_required",
                "detail": "Set confirm: true to reset all data.",
            },
        )
    state = request.app.state
    try:
        new_collection = reset_all_data(
            state.conn, state.chroma_persist_dir, state.driver
        )
    except AdminError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "admin_error", "detail": str(exc)},
        )
    # Resetting Chroma deletes and recreates the collection (see
    # storage/chroma_store.py::reset_all()) -- the old Collection object
    # this process opened at startup is no longer valid, so it must be
    # replaced with the fresh one or every later request in this process
    # (query, ingestion) would keep using a stale/deleted collection.
    state.collection = new_collection
    return AdminResetResponse(status="reset")
