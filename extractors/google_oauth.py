"""Shared Gmail+Calendar OAuth: the guided setup wizard's token flow.

A peer to ``extractors/gmail.py``/``extractors/calendar.py`` within the
extractors layer, not the agent layer — despite being driven from the API
(via ``agent/google_oauth.py``, a thin wrapper matching ``agent/browse.py``'s
own role), the actual OAuth mechanics belong here: this is fundamentally
the same kind of concern ``extractors/gmail.py``'s/``extractors/calendar.py``'s
own ``_get_credentials()``/``setup_auth()`` already own — loading,
refreshing, and persisting a third-party API credential — just shared
across both instead of duplicated per service. Per
``docs/Component_Map.docx``'s dependency rules, ``agent/`` may never depend
on ``extractors/`` for real business logic (only vice versa is allowed
here), so this cannot live in ``agent/`` itself even though it's called
from there.

**Why this exists alongside ``extractors/gmail.py``'s/``extractors/calendar.py``'s
own ``setup_auth()``**: those use ``InstalledAppFlow.run_local_server()`` —
built for a synchronous CLI script that can open a browser and block on its
own temporary local server. That doesn't fit a wizard driven entirely
through this app's own already-running FastAPI backend: there's no
terminal to run a script from, and (per issue #92) no "download a
client_secret.json and place it on disk" step either. This module
implements the same OAuth2 authorization-code flow directly, using the
backend's own real, already-published HTTP endpoint
(``GET /api/setup/google/oauth/callback``, see ``api/routes/setup.py``) as
the redirect target, with the user pasting a Client ID/Secret straight
into the wizard instead of a JSON file.

**One combined consent screen for both Gmail and Calendar** (see
DECISIONS.md) — a single authorization requests both scopes at once, and
the resulting token is shared by both extractors (:func:`load_credentials`,
called from ``extractors/gmail.py``'s/``extractors/calendar.py``'s own
``_get_credentials()`` as their first choice, falling back to each
extractor's own older, separate per-service token file so an existing
manual setup keeps working unchanged).

**Client type**: the wizard instructs the user to create a **Desktop app**
OAuth client in Google Cloud Console (not "Web application") — Google
accepts any ``localhost``/``127.0.0.1`` redirect URI dynamically for a
Desktop-type client, without needing it pre-registered in Console, which
is what makes a variable, per-install backend port (a different one in
Docker vs. a manual dev setup) workable with zero extra Console
configuration. This is a server-side property of the Client ID itself,
not something this module's code can enforce — only document.

**Pending-flow state**: kept in an in-memory, module-level dict, keyed by
the OAuth ``state`` parameter (Google's CSRF-protection value) — sufficient
because the whole authorize-then-callback round trip happens within one
continuously-running backend process (never persisted across a restart,
same as not needing to survive one).
"""

from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from config.settings import PROJECT_ROOT, update_google_oauth_config

# oauthlib refuses to exchange a code against a non-https redirect/callback
# URL (`insecure_transport`) unless this is set — and this app's callback
# (`GET /api/setup/google/oauth/callback`, api/routes/setup.py) is always
# plain http, on 127.0.0.1/localhost for a manual dev setup or on whatever
# host:port docker-compose.yml publishes for Docker, neither of which get
# TLS. Safe here specifically because this whole system is a single-user,
# local-only tool with no authentication layer by design (see CLAUDE.md) —
# the redirect never crosses a network boundary this app itself doesn't
# already trust. Set at import time (not just before this module's own
# Flow calls) so it also covers anything else in-process that touches
# oauthlib, though nothing else in this codebase currently does. See
# DECISIONS.md.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Keyed by the OAuth `state` value Google echoes back on the callback —
# see the module docstring's "Pending-flow state" note.
_pending_flows: dict[str, Flow] = {}


class GoogleOAuthError(Exception):
    """Raised when starting or completing the Google OAuth flow fails."""


def token_path():
    """Where the shared Gmail+Calendar token is cached.

    Returns:
        The path this module reads/writes — a sibling of
        ``extractors/gmail.py``'s/``extractors/calendar.py``'s own
        per-service token files, all under ``data/`` (a Docker-volume-
        backed, restart-surviving directory — see DECISIONS.md, issue #52).
    """
    return PROJECT_ROOT / "data" / "google_oauth_token.json"


def is_connected() -> bool:
    """Has the guided flow already produced a usable shared token?

    A cheap on-disk check only — doesn't call Google at all. The real
    "is it still valid" check is ``agent/connection_check.py``'s live API
    ping, same as the older per-service token files.

    Returns:
        ``True`` if the shared token file exists.
    """
    return token_path().is_file()


def _client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
        }
    }


def start_authorization(
    *, client_id: str, client_secret: str, redirect_uri: str
) -> str:
    """Save the client credentials and build the Google consent screen URL.

    Args:
        client_id: The user-pasted Desktop-app OAuth Client ID.
        client_secret: The user-pasted OAuth Client Secret.
        redirect_uri: This backend's own callback URL — built by the
            caller from the *inbound request's* own host/port
            (``request.base_url`` — see ``api/routes/setup.py``), so it's
            correct whether this is a manual dev setup or a Docker
            deployment on a different published port, with no separate
            "what's my public URL" setting needed.

    Returns:
        The URL to open (in a new tab/popup) for the user to approve
        access — requesting both Gmail and Calendar read scopes at once.

    Raises:
        GoogleOAuthError: If the credentials are empty, or Google rejects
            building the request (e.g. a malformed Client ID).
    """
    if not client_id or not client_secret:
        raise GoogleOAuthError("Client ID and Client Secret are both required.")

    update_google_oauth_config(client_id=client_id, client_secret=client_secret)

    try:
        flow = Flow.from_client_config(
            _client_config(client_id, client_secret),
            scopes=_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
    except Exception as exc:
        raise GoogleOAuthError(f"Could not start Google authorization: {exc}") from exc

    _pending_flows[state] = flow
    return auth_url


def complete_authorization(*, state: str, authorization_response: str) -> None:
    """Exchange the callback's ``code`` for tokens and cache them to disk.

    Args:
        state: The ``state`` query parameter Google echoed back on the
            callback — must match a pending :func:`start_authorization`
            call.
        authorization_response: The full callback URL, query string and
            all (``str(request.url)`` in ``api/routes/setup.py``) — what
            ``Flow.fetch_token()`` expects.

    Raises:
        GoogleOAuthError: If ``state`` is unknown/expired (a stale or
            forged callback), or the token exchange itself fails (e.g. the
            user denied consent, or the code was already used).
    """
    flow = _pending_flows.pop(state, None)
    if flow is None:
        raise GoogleOAuthError(
            "This authorization link has expired or was already used — "
            "restart the Google connection step and try again."
        )

    try:
        flow.fetch_token(authorization_response=authorization_response)
    except Exception as exc:
        raise GoogleOAuthError(f"Google authorization failed: {exc}") from exc

    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(flow.credentials.to_json(), encoding="utf-8")


def load_credentials() -> Credentials | None:
    """Load and, if needed, refresh the shared Gmail+Calendar credentials.

    Called by ``extractors/gmail.py``'s/``extractors/calendar.py``'s own
    ``_get_credentials()`` as their first choice, before falling back to
    their own older, separate per-service token file.

    Returns:
        Valid, ready-to-use credentials, or ``None`` if the guided flow
        was never completed — callers fall back to the older path in that
        case, they don't treat it as an error.

    Raises:
        GoogleOAuthError: If a cached token exists but is invalid/expired
            with no refresh token, and refreshing fails.
    """
    path = token_path()
    if not path.is_file():
        return None

    creds = Credentials.from_authorized_user_file(str(path), _SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GoogleOAuthError(
                f"Could not refresh the shared Google token: {exc}"
            ) from exc
        path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise GoogleOAuthError(
            "The shared Google token is invalid or was revoked — "
            "reconnect Google from Settings."
        )
    return creds
