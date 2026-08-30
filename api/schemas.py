"""Pydantic request/response models for every API endpoint.

Per ``docs/API_Specification.docx``. One module, per
``docs/File_Folder_Structure.docx``'s documented layout — route modules
import the shapes they need from here rather than defining their own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from config.settings import ProviderMode

ServiceStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    """``GET /api/health`` response body."""

    status: Literal["ok", "degraded"]
    services: dict[str, ServiceStatus]


class QueryRequest(BaseModel):
    """``POST /api/query`` request body."""

    question: str
    session_id: str | None = None


class SourceResponse(BaseModel):
    """One cited source, within a ``QueryResponse``."""

    item_id: str
    source_type: str
    title: str | None = None
    url: str | None = None


class QueryResponse(BaseModel):
    """``POST /api/query`` response body."""

    session_id: str
    answer: str
    sources: list[SourceResponse]
    retrieval_methods_used: list[str]
    latency_ms: int


class ErrorResponse(BaseModel):
    """Body shape for every error response, per API_Specification.docx section 2."""

    error: str
    detail: str


class LastRunResponse(BaseModel):
    """The most recent daily batch run, within a ``SourcesStatusResponse``."""

    run_id: str
    started_at: datetime
    completed_at: datetime | None
    status: str


class SourceStatusResponse(BaseModel):
    """One source's outcome in the most recent run, plus its running total."""

    source_type: str
    items_processed: int
    total_items: int
    status: ServiceStatus


class SourcesStatusResponse(BaseModel):
    """``GET /api/sources/status`` response body."""

    last_run: LastRunResponse | None
    sources: list[SourceStatusResponse]


class ConnectionStatusResponse(BaseModel):
    """One source's live connection check, within a ``ConnectionsResponse``."""

    source_type: str
    status: Literal["ok", "error", "not_configured"]
    detail: str | None
    checked_at: datetime


class ConnectionsResponse(BaseModel):
    """``GET``/``POST /api/sources/connections`` response body."""

    connections: list[ConnectionStatusResponse]


class SettingsResponse(BaseModel):
    """``GET /api/settings`` response body."""

    provider_mode: ProviderMode
    local_model: str
    cloud_model: str


class SettingsUpdateRequest(BaseModel):
    """``PUT /api/settings`` request body — every field is optional (partial update)."""

    provider_mode: ProviderMode | None = None
    local_model: str | None = None
    cloud_model: str | None = None


class SettingsUpdateResponse(BaseModel):
    """``PUT /api/settings`` response body."""

    status: Literal["updated"]
    provider_mode: ProviderMode
    local_model: str
    cloud_model: str


class SourceConfigResponse(BaseModel):
    """``GET``/``PUT /api/settings/sources`` response body.

    Extension beyond ``docs/API_Specification.docx`` — configurable
    ingestion scope, not just credentials. See ``DECISIONS.md``.
    """

    local_files_watch_dirs: list[str]
    notion_page_ids: list[str]


class SourceConfigUpdateRequest(BaseModel):
    """``PUT /api/settings/sources`` request body — both fields optional."""

    local_files_watch_dirs: list[str] | None = None
    notion_page_ids: list[str] | None = None


class SessionSummary(BaseModel):
    """One session, within a ``SessionsListResponse``."""

    session_id: str
    title: str | None
    updated_at: datetime


class SessionsListResponse(BaseModel):
    """``GET /api/sessions`` response body."""

    sessions: list[SessionSummary]


class MessageResponse(BaseModel):
    """One message, within a ``SessionHistoryResponse``."""

    role: Literal["user", "agent"]
    text: str
    timestamp: datetime
    sources: list[SourceResponse] | None = None


class SessionHistoryResponse(BaseModel):
    """``GET /api/sessions/{session_id}`` response body."""

    session_id: str
    messages: list[MessageResponse]


class IngestTriggerResponse(BaseModel):
    """``POST /api/ingest/trigger`` response body (202 Accepted)."""

    status: Literal["started"]
    run_id: str


class AdminResetRequest(BaseModel):
    """``POST /api/admin/reset`` request body.

    ``confirm`` must be explicitly ``true`` — a cheap guard against an
    accidental call to a destructive, hard-to-reverse endpoint. Extension
    beyond ``docs/API_Specification.docx`` — see ``DECISIONS.md``.
    """

    confirm: bool


class AdminResetResponse(BaseModel):
    """``POST /api/admin/reset`` response body."""

    status: Literal["reset"]


class GraphNodeResponse(BaseModel):
    """One item, within a ``GraphResponse``."""

    id: str
    title: str | None
    source_type: str
    url: str | None


class GraphEdgeResponse(BaseModel):
    """One relationship edge, within a ``GraphResponse``."""

    source_id: str
    target_id: str
    label: str
    confidence: float | None


class GraphResponse(BaseModel):
    """``GET /api/graph`` response body.

    Extension beyond ``docs/API_Specification.docx`` — a graph view wasn't
    in the original spec. See ``DECISIONS.md``.
    """

    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
