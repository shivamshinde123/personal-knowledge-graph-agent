"""``GET /api/sessions`` and ``GET /api/sessions/{session_id}``.

Per ``docs/API_Specification.docx`` sections 3.4/3.5. Reads
``storage/sqlite_store.py`` directly rather than through an ``agent/``
module — same reasoning as ``api/routes/settings.py``: this is a plain
read of session/message records, not agent reasoning, so there's no
meaningful "agent" step to route it through.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.schemas import (
    MessageResponse,
    SessionHistoryResponse,
    SessionsListResponse,
    SessionSummary,
)
from storage.sqlite_store import get_messages_for_session, get_session, list_sessions

router = APIRouter()


@router.get("/sessions", response_model=SessionsListResponse)
def get_sessions(request: Request) -> SessionsListResponse:
    """List every session, most recently active first."""
    sessions = list_sessions(request.app.state.conn)
    return SessionsListResponse(
        sessions=[
            SessionSummary(
                session_id=session.id,
                title=session.title,
                updated_at=session.updated_at,
            )
            for session in sessions
        ]
    )


@router.get("/sessions/{session_id}")
def get_session_history(session_id: str, request: Request):
    """Return one session's full message history, oldest first.

    Returns a 404 (shaped like every other error, per
    ``docs/API_Specification.docx`` section 2) if the session doesn't
    exist.
    """
    conn = request.app.state.conn
    if get_session(conn, session_id) is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "detail": f"No session with id {session_id!r}",
            },
        )

    messages = get_messages_for_session(conn, session_id)
    return SessionHistoryResponse(
        session_id=session_id,
        messages=[
            MessageResponse(
                role=message.role,
                text=message.text,
                timestamp=message.created_at,
                sources=message.sources,
            )
            for message in messages
        ],
    )
