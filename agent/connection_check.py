"""Connection status: live per-source connectivity/configuration checks.

Lives in the agent layer, not ``api/``, for the same reason
``agent/health.py`` and ``agent/sources_status.py`` do — the API layer
depends only on the agent's public entrypoints. See ``DECISIONS.md``.

Distinct from ``agent/sources_status.py``, which reports the *last daily
batch run's* outcome per source (an item never fails there simply because
it hasn't run recently, or at all) — before this module existed, that made
the Settings screen show every source, including ones with no extractor
built yet, as a green "OK" the moment nothing had ever run, which reads
as "verified working" when it really means "nothing has been checked."
This module actually checks: for a configured source, is its
credential/path valid right now (a real, cheap API call for Notion,
Gmail, GitHub, and Google Calendar; a filesystem check for local
files/browser history); for an unconfigured source, says so explicitly
rather than defaulting to "ok". See ``DECISIONS.md``.

Results are cached in-process (module-level, not persisted) since a live
check — especially Notion's real API call — is too expensive to repeat on
every Settings-page load; ``get_connection_status()`` reuses a cache younger
than ``max_age_seconds`` unless ``force_refresh`` is set, which is what the
"Reverify" button on the Settings screen sends.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from config.settings import EnvSettings, get_settings

logger = logging.getLogger(__name__)

ConnectionState = Literal["ok", "error", "not_configured"]

_DEFAULT_MAX_AGE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    """One source's live connectivity/configuration check result."""

    source_type: str
    status: ConnectionState
    detail: str | None
    checked_at: datetime


_cache: list[ConnectionStatus] | None = None
_cache_time: datetime | None = None


def get_connection_status(
    *, force_refresh: bool = False, max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS
) -> list[ConnectionStatus]:
    """Return each source's connection status, live-checking if needed.

    Args:
        force_refresh: Skip the cache and check every source again
            regardless of age — what the Settings screen's "Reverify"
            button requests.
        max_age_seconds: How old a cached result can be before it's
            considered stale and re-checked anyway.

    Returns:
        One :class:`ConnectionStatus` per of the six sources, in a fixed
        order (``local_file``, ``notion``, ``gmail``, ``github``,
        ``calendar``, ``browser_history``), matching
        ``agent/sources_status.py``'s ``_SOURCE_TYPES``.
    """
    global _cache, _cache_time
    if not force_refresh and _cache is not None and _cache_time is not None:
        age = (datetime.now(UTC) - _cache_time).total_seconds()
        if age < max_age_seconds:
            return _cache

    _cache = check_all_connections()
    _cache_time = datetime.now(UTC)
    return _cache


def check_all_connections() -> list[ConnectionStatus]:
    """Check every source's connection fresh, ignoring any cache.

    Returns:
        One :class:`ConnectionStatus` per source, same order as
        :func:`get_connection_status`.
    """
    env = get_settings().env
    now = datetime.now(UTC)
    return [
        _check_local_files(env, now),
        _check_notion(env, now),
        _check_gmail(env, now),
        _check_github(env, now),
        _check_calendar(env, now),
        _check_browser_history(env, now),
    ]


def _check_local_files(env: EnvSettings, now: datetime) -> ConnectionStatus:
    watch_dirs = env.watch_dirs
    if not watch_dirs:
        return ConnectionStatus(
            source_type="local_file",
            status="not_configured",
            detail="LOCAL_FILES_WATCH_DIRS is not set.",
            checked_at=now,
        )
    missing = [str(d) for d in watch_dirs if not d.is_dir()]
    if missing:
        return ConnectionStatus(
            source_type="local_file",
            status="error",
            detail=f"Not found or not a directory: {', '.join(missing)}",
            checked_at=now,
        )
    noun = "directory" if len(watch_dirs) == 1 else "directories"
    return ConnectionStatus(
        source_type="local_file",
        status="ok",
        detail=f"{len(watch_dirs)} watch {noun} reachable.",
        checked_at=now,
    )


def _check_notion(env: EnvSettings, now: datetime) -> ConnectionStatus:
    if not env.notion_api_key:
        return ConnectionStatus(
            source_type="notion",
            status="not_configured",
            detail="NOTION_API_KEY is not set.",
            checked_at=now,
        )
    try:
        from notion_client import Client

        Client(auth=env.notion_api_key).users.me()
    except Exception as exc:
        logger.warning("Notion connection check failed: %s", exc)
        return ConnectionStatus(
            source_type="notion",
            status="error",
            detail=str(exc),
            checked_at=now,
        )
    return ConnectionStatus(
        source_type="notion",
        status="ok",
        detail="Notion API token verified.",
        checked_at=now,
    )


def _check_gmail(env: EnvSettings, now: datetime) -> ConnectionStatus:
    if env.gmail_credentials_path is None:
        return ConnectionStatus(
            source_type="gmail",
            status="not_configured",
            detail="GMAIL_CREDENTIALS_PATH is not set.",
            checked_at=now,
        )
    try:
        from extractors.gmail import _build_service, _get_credentials

        # A one-time interactive consent (extractors/gmail.py::setup_auth())
        # not having been run yet is reported as "not configured", not
        # "error" — there's nothing wrong with the setup, it just isn't
        # finished. Any other failure (revoked/invalid token, unreachable
        # API) is a real error.
        service = _build_service(_get_credentials())
        service.users().getProfile(userId="me").execute()
    except Exception as exc:
        from extractors.base import ExtractorError

        if isinstance(exc, ExtractorError) and "not authorized yet" in str(exc):
            return ConnectionStatus(
                source_type="gmail",
                status="not_configured",
                detail=str(exc),
                checked_at=now,
            )
        logger.warning("Gmail connection check failed: %s", exc)
        return ConnectionStatus(
            source_type="gmail",
            status="error",
            detail=str(exc),
            checked_at=now,
        )
    return ConnectionStatus(
        source_type="gmail",
        status="ok",
        detail="Gmail authorization verified.",
        checked_at=now,
    )


def _check_github(env: EnvSettings, now: datetime) -> ConnectionStatus:
    if not env.github_token:
        return ConnectionStatus(
            source_type="github",
            status="not_configured",
            detail="GITHUB_TOKEN is not set.",
            checked_at=now,
        )
    try:
        import httpx

        response = httpx.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {env.github_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("GitHub connection check failed: %s", exc)
        return ConnectionStatus(
            source_type="github",
            status="error",
            detail=str(exc),
            checked_at=now,
        )
    return ConnectionStatus(
        source_type="github",
        status="ok",
        detail="GitHub token verified.",
        checked_at=now,
    )


def _check_calendar(env: EnvSettings, now: datetime) -> ConnectionStatus:
    if env.google_calendar_credentials_path is None:
        return ConnectionStatus(
            source_type="calendar",
            status="not_configured",
            detail="GOOGLE_CALENDAR_CREDENTIALS_PATH is not set.",
            checked_at=now,
        )
    try:
        from extractors.calendar import _build_service, _get_credentials

        # A one-time interactive consent (extractors/calendar.py::setup_auth())
        # not having been run yet is reported as "not configured", not
        # "error" — there's nothing wrong with the setup, it just isn't
        # finished. Any other failure (revoked/invalid token, unreachable
        # API) is a real error. Same pattern as _check_gmail().
        service = _build_service(_get_credentials())
        service.calendarList().get(calendarId="primary").execute()
    except Exception as exc:
        from extractors.base import ExtractorError

        if isinstance(exc, ExtractorError) and "not authorized yet" in str(exc):
            return ConnectionStatus(
                source_type="calendar",
                status="not_configured",
                detail=str(exc),
                checked_at=now,
            )
        logger.warning("Google Calendar connection check failed: %s", exc)
        return ConnectionStatus(
            source_type="calendar",
            status="error",
            detail=str(exc),
            checked_at=now,
        )
    return ConnectionStatus(
        source_type="calendar",
        status="ok",
        detail="Google Calendar authorization verified.",
        checked_at=now,
    )


def _check_browser_history(env: EnvSettings, now: datetime) -> ConnectionStatus:
    path = env.browser_history_path
    if path is None:
        return ConnectionStatus(
            source_type="browser_history",
            status="not_configured",
            detail="BROWSER_HISTORY_PATH is not set.",
            checked_at=now,
        )
    if not path.is_file():
        return ConnectionStatus(
            source_type="browser_history",
            status="error",
            detail=f"File not found: {path}",
            checked_at=now,
        )
    return ConnectionStatus(
        source_type="browser_history",
        status="ok",
        detail="History file reachable.",
        checked_at=now,
    )
