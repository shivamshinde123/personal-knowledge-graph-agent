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
    """One source's outcome in the most recent run."""

    source_type: str
    items_processed: int
    status: ServiceStatus


class SourcesStatusResponse(BaseModel):
    """``GET /api/sources/status`` response body."""

    last_run: LastRunResponse | None
    sources: list[SourceStatusResponse]


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
