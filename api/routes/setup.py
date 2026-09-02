"""Guided setup wizard endpoints — issue #92.

The Google OAuth (Gmail + Calendar) flow needed genuinely new backend
capability (see ``agent/google_oauth.py``, ``extractors/google_oauth.py``).
Provider mode, Notion, and GitHub are mostly a thin wizard shell around
functionality ``api/routes/settings.py``/``api/routes/sources.py`` already
expose — except that, before this module, there was no way to *save*
``NOTION_API_KEY``/``GITHUB_TOKEN``/``OPENROUTER_API_KEY`` at all (only
source-scope fields had an update endpoint), and no way to validate a
credential against the real API *before* saving it, which issue #92
explicitly asks for. ``POST /setup/validate`` and ``POST /setup/credentials``
below fill both gaps. ``GET /setup/host-data-files`` fills a similar gap
for browser history under Docker — a real pick list of what's actually
reachable, instead of typing an in-container path blind. Local files
watching itself needs no new endpoint at all — it's read-only under Docker
(see ``api/routes/settings.py``) and already fully editable via the
existing ``Browse…``/textarea flow otherwise.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from agent.connection_check import (
    github_token_works,
    notion_key_works,
    openrouter_key_works,
)
from agent.google_oauth import GoogleOAuthError, complete_authorization, is_connected
from agent.google_oauth import start_authorization as start_google_authorization
from agent.mounted_files import list_host_data_files
from api.schemas import (
    GoogleOAuthStartRequest,
    GoogleOAuthStartResponse,
    GoogleOAuthStatusResponse,
    MountedFilesResponse,
    SetupCredentialsRequest,
    SetupCredentialsResponse,
    SetupValidateRequest,
    SetupValidateResponse,
)
from config.settings import update_credentials_config

router = APIRouter()


def _callback_page(*, ok: bool, message: str) -> str:
    """A tiny, dependency-free HTML page for the OAuth popup to land on.

    Not JSON — this route is hit by a real browser navigation (Google's
    own redirect), not a ``fetch()`` call, so it needs to render
    *something* human-readable. The wizard's own tab never sees this page
    directly; it detects success/failure by polling
    ``GET /api/setup/google/oauth/status`` while this popup is open (see
    ``frontend/src/components/SetupWizard.jsx``).
    """
    heading = "Google connected" if ok else "Connection failed"
    return f"""<!doctype html>
<html><head><title>{heading}</title></head>
<body style="font-family: sans-serif; text-align: center; padding: 48px;">
<h2>{heading}</h2>
<p>{message}</p>
<p>You can close this tab and return to the setup wizard.</p>
</body></html>"""


@router.post("/setup/google/oauth/start", response_model=GoogleOAuthStartResponse)
def start_google_oauth_route(payload: GoogleOAuthStartRequest, request: Request):
    """Save the pasted Client ID/Secret and build the consent screen URL.

    The redirect URI is built from this *inbound request's* own host/port
    (``request.url_for()``) rather than any configured "public URL"
    setting — correct whether this is a manual dev setup or a Docker
    deployment on a different published port, with nothing new to
    configure. A ``GoogleOAuthError`` (empty credentials, or Google
    rejecting the request) maps to a plain error response via
    ``api/main.py``'s shared handler — this route has no special handling
    of its own.
    """
    redirect_uri = str(request.url_for("google_oauth_callback_route"))
    auth_url = start_google_authorization(
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        redirect_uri=redirect_uri,
    )
    return GoogleOAuthStartResponse(authorization_url=auth_url)


@router.get("/setup/google/oauth/callback", name="google_oauth_callback_route")
def google_oauth_callback_route(request: Request) -> HTMLResponse:
    """Google's own redirect target — exchanges the code, caches the token.

    Hit directly by the user's browser (a real navigation following
    Google's consent screen), never by the frontend's own ``fetch()``
    calls — so this always returns HTML, not JSON, regardless of outcome.
    A denied consent, an expired/reused ``state``, or a failed token
    exchange are all rendered as a plain failure page rather than an API
    error response, since there's no JSON-consuming caller here to receive
    one.
    """
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(
            _callback_page(ok=False, message=f"Google reported: {error}")
        )

    state = request.query_params.get("state")
    if not state:
        return HTMLResponse(
            _callback_page(ok=False, message="Missing state parameter.")
        )

    try:
        complete_authorization(state=state, authorization_response=str(request.url))
    except GoogleOAuthError as exc:
        return HTMLResponse(_callback_page(ok=False, message=str(exc)))

    return HTMLResponse(
        _callback_page(ok=True, message="Gmail and Google Calendar are now connected.")
    )


@router.get("/setup/google/oauth/status", response_model=GoogleOAuthStatusResponse)
def google_oauth_status_route() -> GoogleOAuthStatusResponse:
    """Polled by the wizard while its "Connect Google" popup is open.

    A cheap on-disk check (``agent/google_oauth.py::is_connected()``) —
    the popup itself completes the real token exchange via the callback
    route above; this route only tells the *wizard's own tab* that it
    happened, since the callback lands in a separate tab/window.
    """
    return GoogleOAuthStatusResponse(connected=is_connected())


@router.post("/setup/validate", response_model=SetupValidateResponse)
def validate_credential_route(payload: SetupValidateRequest) -> SetupValidateResponse:
    """Check a pasted credential against the real API, before saving it.

    Never touches ``config/.env`` — a raw value in, a real/fake result
    out. The wizard only calls ``POST /setup/credentials`` once this
    returns ``status: "ok"``, so a bad credential is never persisted (per
    issue #92's "validated... before letting the user continue" — not the
    same as ``PUT /api/settings/sources``, which saves unconditionally).
    """
    # Dispatches on the plain module-level names (not a dict built once at
    # import time, which would capture stale references tests couldn't
    # monkeypatch) — a plain function reference in a function body is
    # looked up fresh from this module's namespace on every call.
    if payload.source == "openrouter":
        ok, detail = openrouter_key_works(payload.value)
    elif payload.source == "notion":
        ok, detail = notion_key_works(payload.value)
    else:
        ok, detail = github_token_works(payload.value)
    return SetupValidateResponse(status="ok" if ok else "error", detail=detail)


@router.post("/setup/credentials", response_model=SetupCredentialsResponse)
def save_credentials_route(
    payload: SetupCredentialsRequest,
) -> SetupCredentialsResponse:
    """Save Notion/GitHub/OpenRouter credentials to ``config/.env``.

    Before this endpoint, none of these three had any way to be set
    except editing ``config/.env`` by hand — only source-*scope* fields
    (``PUT /api/settings/sources``) had one. Does no validation of its
    own; the wizard calls ``POST /setup/validate`` first and only reaches
    here on a confirmed-good value. A ``ConfigError`` (file I/O failure)
    maps to 500 via ``api/main.py``'s shared handler.
    """
    update_credentials_config(
        notion_api_key=payload.notion_api_key,
        github_token=payload.github_token,
        openrouter_api_key=payload.openrouter_api_key,
    )
    return SetupCredentialsResponse(status="updated")


@router.get("/setup/host-data-files", response_model=MountedFilesResponse)
def list_host_data_files_route() -> MountedFilesResponse:
    """List files under the Docker ``/host-data`` mount, for a real pick list.

    Used by the wizard's browser-history step (and, in principle, any
    future step needing a file under a mounted host directory) so the
    user picks from what's actually reachable instead of typing an
    in-container path blind. Always ``{"files": []}`` outside Docker,
    where ``/host-data`` doesn't exist — the wizard falls back to a plain
    text input for the real host path in that case.
    """
    return MountedFilesResponse(files=list_host_data_files())
