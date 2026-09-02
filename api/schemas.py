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
    """The most recent daily batch run, within a ``SourcesStatusResponse``.

    ``items_processed`` updates live while ``status`` is still
    ``"running"`` — ``scheduler/daily_batch.py`` persists it after every
    item, not just once at completion — so polling this gives a real,
    live progress count, not just a start/stop signal. See DECISIONS.md.

    ``current_item`` is similarly live: set to a short label (e.g.
    ``"Extracting github…"`` while a source's extraction is in flight, then
    ``"github: my-repo: fix the bug"`` once per-item processing starts) —
    a completed run leaves it at whatever it last was, which the frontend
    only displays while ``status == "running"``.
    """

    run_id: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    items_processed: int
    current_item: str | None = None


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
    """``GET /api/settings`` response body.

    Two generation/embedding pairs — local (Ollama) and cloud
    (OpenRouter) — all four fields settable regardless of which
    ``provider_mode`` is currently active; the frontend only *displays*
    the pair matching the active mode. Changing ``provider_mode`` itself
    is the bigger deal: it changes which embedding space every future
    vector lands in, so the frontend treats it as destructive (a double
    confirm, then an automatic full reset). Changing ``cloud_embedding_model``
    in place, without switching modes, still needs a reset + re-ingest for
    the same reason. Nothing here enforces either reset; that's the
    frontend's job. See ``DECISIONS.md``.
    """

    provider_mode: ProviderMode
    local_generation_model: str
    local_embedding_model: str
    cloud_generation_model: str
    cloud_embedding_model: str


class SettingsUpdateRequest(BaseModel):
    """``PUT /api/settings`` request body — every field is optional (partial update)."""

    provider_mode: ProviderMode | None = None
    local_generation_model: str | None = None
    local_embedding_model: str | None = None
    cloud_generation_model: str | None = None
    cloud_embedding_model: str | None = None


class SettingsUpdateResponse(BaseModel):
    """``PUT /api/settings`` response body."""

    status: Literal["updated"]
    provider_mode: ProviderMode
    local_generation_model: str
    local_embedding_model: str
    cloud_generation_model: str
    cloud_embedding_model: str


class SourceConfigResponse(BaseModel):
    """``GET``/``PUT /api/settings/sources`` response body.

    Extension beyond ``docs/API_Specification.docx`` — configurable
    ingestion scope, not just credentials. See ``DECISIONS.md``.

    The six date-range fields are plain ``YYYY-MM-DD`` strings (or
    ``None``), not a ``date`` type — this matches an HTML
    ``<input type="date">``'s value directly, so the frontend needs no
    parsing/formatting boundary. See ``DECISIONS.md``.

    ``gmail_date_range_start``/``_end`` and ``calendar_date_range_start``/
    ``_end`` are never actually ``None`` in practice — they carry
    ``EnvSettings.effective_gmail_date_range_start``/``_end`` and
    ``effective_calendar_date_range_start``/``_end``, which default to a
    rolling window (last 15 days / next 30 days) rather than "unset."
    ``github_date_range_start``/``_end`` are the one pair that can
    genuinely be ``None`` — GitHub's own range was never asked to default.
    See ``DECISIONS.md``.

    ``running_in_docker`` mirrors ``EnvSettings.running_in_docker`` — when
    true, ``local_files_watch_dirs`` is a fixed, Docker-volume-backed path
    the frontend must render read-only (no "Browse…" button, no editable
    textarea): the running app cannot mount a new host folder into itself,
    only ``docker-compose.yml`` can, at container-start. See
    ``DECISIONS.md``.
    """

    local_files_watch_dirs: list[str]
    notion_page_ids: list[str]
    github_repos: list[str]
    gmail_date_range_start: str | None
    gmail_date_range_end: str | None
    github_date_range_start: str | None
    github_date_range_end: str | None
    calendar_date_range_start: str | None
    calendar_date_range_end: str | None
    running_in_docker: bool


class SourceConfigUpdateRequest(BaseModel):
    """``PUT /api/settings/sources`` request body — every field optional."""

    local_files_watch_dirs: list[str] | None = None
    notion_page_ids: list[str] | None = None
    github_repos: list[str] | None = None
    gmail_date_range_start: str | None = None
    gmail_date_range_end: str | None = None
    github_date_range_start: str | None = None
    github_date_range_end: str | None = None
    calendar_date_range_start: str | None = None
    calendar_date_range_end: str | None = None


class BrowseFolderResponse(BaseModel):
    """``POST /api/settings/browse-folder`` response body.

    Extension beyond ``docs/API_Specification.docx`` — see
    ``agent/browse.py``, ``DECISIONS.md``.
    """

    path: str | None


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


class IngestCancelRequest(BaseModel):
    """``POST /api/ingest/cancel`` request body."""

    run_id: str


class IngestCancelResponse(BaseModel):
    """``POST /api/ingest/cancel`` response body.

    Acknowledges the request only — the run stops at its next check point,
    not immediately, so the actual outcome (``status: "cancelled"`` on the
    run) is observed afterward via ``GET /api/sources/status``, same as
    every other ingestion outcome.
    """

    status: Literal["cancel_requested"]


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


class GoogleOAuthStartRequest(BaseModel):
    """``POST /api/setup/google/oauth/start`` request body.

    Extension beyond ``docs/API_Specification.docx`` — the guided setup
    wizard (issue #92). Both fields are saved to ``config/.env`` before
    the authorization URL is built, so a completed connection persists the
    same way any other credential does.
    """

    client_id: str
    client_secret: str


class GoogleOAuthStartResponse(BaseModel):
    """``POST /api/setup/google/oauth/start`` response body."""

    authorization_url: str


class GoogleOAuthStatusResponse(BaseModel):
    """``GET /api/setup/google/oauth/status`` response body.

    Polled by the wizard while its "Connect Google" popup is open, since
    the actual callback lands in that separate popup/tab, not the
    wizard's own — see ``frontend/src/components/SetupWizard.jsx``.
    """

    connected: bool
