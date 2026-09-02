"""Guided setup wizard endpoints — issue #92.

Currently just the Google OAuth (Gmail + Calendar) flow, the one part of
the wizard needing new backend capability at all — every other step
(provider mode, cloud credentials, Notion, GitHub, local files, browser
history) is a thin wizard shell around functionality
``api/routes/settings.py`` and ``api/routes/sources.py`` already expose.
See ``agent/google_oauth.py``, ``extractors/google_oauth.py``,
``DECISIONS.md``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from agent.google_oauth import GoogleOAuthError, complete_authorization, is_connected
from agent.google_oauth import start_authorization as start_google_authorization
from api.schemas import (
    GoogleOAuthStartRequest,
    GoogleOAuthStartResponse,
    GoogleOAuthStatusResponse,
)

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
