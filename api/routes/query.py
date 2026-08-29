"""``POST /api/query``: submits a question to the LangGraph agent.

Per ``docs/API_Specification.docx`` section 3.1. Per ``Component_Map.docx``,
this is the API layer's one real piece of business logic wiring — it calls
``agent/graph.py::run()``, the agent's public entrypoint, and nothing in
``storage``/``providers`` directly.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from agent.graph import run
from api.schemas import QueryRequest, QueryResponse, SourceResponse

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
def post_query(payload: QueryRequest, request: Request) -> QueryResponse:
    """Answer a question via ``agent/graph.py::run()`` and time the call."""
    started = time.monotonic()
    result = run(
        request.app.state.conn,
        request.app.state.collection,
        request.app.state.driver,
        payload.question,
        payload.session_id,
    )
    latency_ms = round((time.monotonic() - started) * 1000)
    return QueryResponse(
        session_id=result.session_id,
        answer=result.answer,
        sources=[
            SourceResponse(
                item_id=source.item_id,
                source_type=source.source_type,
                title=source.title,
                url=source.url,
            )
            for source in result.sources
        ],
        retrieval_methods_used=result.retrieval_methods_used,
        latency_ms=latency_ms,
    )
