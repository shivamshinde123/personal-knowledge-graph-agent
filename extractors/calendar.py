"""Google Calendar extractor: primary calendar events → normalized items.

Per ``docs/Data_Extraction_Specification.docx`` section 7's field-level
table: title (stored as ``ExtractedItem.title``), description (the main
embedded text — titles alone are usually too short to embed meaningfully
per ``docs/Technical_Design_Document.docx`` section 3.4, so it's not the
*only* content, but it's the dominant part), start/end time and attendees
(folded into ``raw_text`` — timeline correlation and "who a meeting was
with" are both genuinely useful retrieval/relationship signals, unlike
Gmail's thread id there's no natural place to demote them to). Location
and color/category tag are dropped entirely — the spec marks both
"metadata only, not embedded" or "lightweight," and (same reasoning as
Gmail's thread id) ``ExtractedItem`` has no metadata field to put them in,
so they're not worth the complexity for their low signal — see
``DECISIONS.md``.

Recurring event instances are never expanded: ``events().list()`` is
called with ``singleEvents=False``, so a recurring series returns as one
event (with a ``recurrence`` rule) rather than one item per occurrence,
directly satisfying the spec's "recurrence pattern stored once, not per
instance." A recurring event with no description — a standing reminder,
not a real meeting — is dropped by
``config.yaml``'s ``filters.calendar.skip_recurring_without_description``
(default ``true``), per section 3.4's "a rule filters out pure recurring
noise (e.g. daily reminders)".

Authentication follows the same one-time-OAuth-then-cached-token pattern
as ``extractors/gmail.py`` (a personal calendar has no service-account
option either) — see that module's docstring for the full reasoning. The
two sources use separate credentials/token files
(``GOOGLE_CALENDAR_CREDENTIALS_PATH`` / ``data/calendar_token.json``),
not shared ones, matching their already-separate ``.env`` variables.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config.settings import PROJECT_ROOT, get_settings
from extractors.base import ExtractedItem, ExtractorError

logger = logging.getLogger(__name__)

SOURCE_TYPE = "calendar"

_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
_CALENDAR_ID = "primary"
_PAGE_SIZE = 250


def _token_path():
    return PROJECT_ROOT / "data" / "calendar_token.json"


def extract_new_items(since: datetime | None = None) -> list[ExtractedItem]:
    """Extract primary-calendar events updated after ``since``.

    Args:
        since: Only include events created/updated after this time.
            ``None`` (the first-ever run) includes every event on the
            calendar surviving noise filtering.

    Returns:
        One item per non-cancelled event surviving
        ``filters.calendar.skip_recurring_without_description``. A single
        event failing to parse is logged and skipped rather than
        aborting the rest of the run.

    Raises:
        ExtractorError: If ``GOOGLE_CALENDAR_CREDENTIALS_PATH`` isn't
            configured, or no cached, authorized token exists yet
            (:func:`setup_auth` hasn't been run) — both source-level
            failures the daily batch records and moves past.
    """
    settings = get_settings()
    if settings.env.google_calendar_credentials_path is None:
        raise ExtractorError("GOOGLE_CALENDAR_CREDENTIALS_PATH is not configured")

    service = _build_service(_get_credentials())
    filters = settings.config.filters.calendar
    events = _list_events(service, since)

    logger.info(
        "Calendar extraction starting (since=%s): %d event(s) fetched",
        since,
        len(events),
    )

    items: list[ExtractedItem] = []
    for event in events:
        try:
            item = _event_to_item(event, filters)
        except Exception as exc:
            logger.warning(
                "Could not extract calendar event %r: %s", event.get("id"), exc
            )
            continue
        if item is not None:
            items.append(item)

    logger.info(
        "Calendar extraction finished: %d event(s) scanned, %d item(s) extracted",
        len(events),
        len(items),
    )
    return items


def _build_service(creds):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _get_credentials():
    """Load the cached, authorized token, refreshing it if expired.

    Never triggers the interactive consent flow itself — see
    :func:`setup_auth` and the module docstring.
    """
    token_path = _token_path()
    if not token_path.is_file():
        raise ExtractorError(
            "Google Calendar is not authorized yet. Run "
            "`uv run python -m extractors.calendar --setup-auth` once to "
            "complete the one-time browser consent, then re-run ingestion."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise ExtractorError(
            "Cached Google Calendar credentials are invalid or revoked. Run "
            "`uv run python -m extractors.calendar --setup-auth` again."
        )
    return creds


def setup_auth() -> None:
    """One-time interactive OAuth consent flow — never called by the daily batch.

    Opens a browser for the user to approve read-only Calendar access,
    then caches the resulting (refreshable) credentials to
    ``data/calendar_token.json``. Run via
    ``uv run python -m extractors.calendar --setup-auth``.

    Raises:
        ExtractorError: If ``GOOGLE_CALENDAR_CREDENTIALS_PATH`` isn't
            configured or the consent flow fails.
    """
    credentials_path = get_settings().env.google_calendar_credentials_path
    if credentials_path is None:
        raise ExtractorError("GOOGLE_CALENDAR_CREDENTIALS_PATH is not configured")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), _SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        raise ExtractorError(f"Google Calendar authorization failed: {exc}") from exc

    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Google Calendar authorization saved to %s", token_path)


def _list_events(service, since: datetime | None) -> list[dict]:
    try:
        params: dict = {
            "calendarId": _CALENDAR_ID,
            "maxResults": _PAGE_SIZE,
            # Never expand a recurring series into one entry per
            # occurrence — see the module docstring.
            "singleEvents": False,
            "showDeleted": False,
        }
        if since is not None:
            params["updatedMin"] = _to_rfc3339(since)

        events: list[dict] = []
        page_token = None
        while True:
            params["pageToken"] = page_token
            response = service.events().list(**params).execute()
            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return events
    except Exception as exc:
        raise ExtractorError(f"Could not list Google Calendar events: {exc}") from exc


def _event_to_item(event: dict, filters) -> ExtractedItem | None:
    if event.get("status") == "cancelled":
        return None

    title = event.get("summary") or "(no title)"
    description = (event.get("description") or "").strip()
    is_recurring = bool(event.get("recurrence"))
    if is_recurring and not description and filters.skip_recurring_without_description:
        return None

    start = _parse_event_time(event.get("start"))
    end = _parse_event_time(event.get("end"))
    attendees = [a["email"] for a in event.get("attendees", []) if a.get("email")]
    organizer = (event.get("organizer") or {}).get("email")

    parts = [title]
    if start is not None:
        when = start.isoformat()
        if end is not None:
            when += f" to {end.isoformat()}"
        parts.append(f"Time: {when}")
    if attendees:
        parts.append("Attendees: " + ", ".join(attendees))
    if description:
        parts.append(description)

    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=event["id"],
        title=title,
        url_or_path=event.get("htmlLink", ""),
        raw_text="\n\n".join(parts),
        author_or_sender=organizer,
        created_at=_parse_timestamp(event.get("created")),
        last_edited_at=_parse_timestamp(event.get("updated")),
    )


def _parse_event_time(value: dict | None) -> datetime | None:
    """Parse a Calendar event's ``start``/``end`` — timed or all-day.

    A timed event has ``dateTime`` (already includes a UTC offset); an
    all-day event has only ``date`` (no time component at all), which is
    anchored to midnight UTC rather than left naive, so it can still be
    compared against ``since``/other sources' timestamps.
    """
    if not value:
        return None
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"])
    if "date" in value:
        return datetime.fromisoformat(value["date"]).replace(tzinfo=UTC)
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


if __name__ == "__main__":
    import sys

    if "--setup-auth" not in sys.argv:
        print("Usage: uv run python -m extractors.calendar --setup-auth")
        sys.exit(1)
    setup_auth()
