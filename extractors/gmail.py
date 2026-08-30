"""Gmail extractor: inbox threads → normalized items, grouped by conversation.

Per ``docs/Data_Extraction_Specification.docx`` section 5's field-level
table: subject (title), body text (embedded), sender/recipient and date
(metadata), thread ID (groups related messages), labels (lightweight
categorization, also used for noise filtering), and attachments
(selectively — text extracted from relevant document types only, same
``pypdf``/``python-docx`` approach as ``extractors/local_files.py``).

One :class:`~extractors.base.ExtractedItem` per **thread**, not per
message — every message in the thread is concatenated into one item's
``raw_text`` (sender, date, and body per message, oldest first), so a
reply is never embedded in isolation, disconnected from the conversation
it's actually part of. ``source_ref_id`` is the Gmail thread id, so a new
reply to an existing thread re-extracts and re-embeds the *whole*,
now-longer conversation (``insert_item()`` upserts on
``(source_type, source_ref_id)`` — see ``storage/sqlite_store.py``), not a
disconnected second item.

Authentication is OAuth2 user consent (a personal Gmail inbox has no
service-account/domain-wide-delegation option), which needs one
interactive, browser-based approval — this cannot happen during an
unattended scheduled run, so it's a one-time setup step
(:func:`setup_auth`, run via
``uv run python -m extractors.gmail --setup-auth``), not part of
``extract_new_items()`` itself. The resulting refresh token is cached to
disk and silently renewed on every later run — see ``DECISIONS.md``.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from io import BytesIO

import docx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from pypdf import PdfReader

from config.settings import PROJECT_ROOT, get_settings
from extractors.base import ExtractedItem, ExtractorError

logger = logging.getLogger(__name__)

SOURCE_TYPE = "gmail"

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Document types worth extracting text from when attached (resumes, offer
# letters, job descriptions) — per the spec's "Selectively" note. Anything
# else (images, archives, spreadsheets, ...) is skipped; see
# _extract_attachment_text().
_ATTACHABLE_DOC_SUFFIXES = ("pdf", "docx")


def _token_path():
    return PROJECT_ROOT / "data" / "gmail_token.json"


def extract_new_items(since: datetime | None = None) -> list[ExtractedItem]:
    """Extract Gmail threads with activity after ``since``.

    Args:
        since: Only include threads with a message dated after this time.
            ``None`` (the first-ever run) includes every thread in the
            mailbox. A thread with any qualifying message is extracted in
            full — every message in it, not just the new ones — so the
            conversation is never embedded with earlier context missing.

    Returns:
        One item per thread with at least one message surviving label
        filtering (``config.yaml``'s ``filters.gmail.excluded_labels``,
        e.g. promotional/social tab mail). A single thread or message
        failing to fetch/parse is logged and skipped rather than aborting
        the rest of the run.

    Raises:
        ExtractorError: If ``GMAIL_CREDENTIALS_PATH`` isn't configured, or
            no cached, authorized token exists yet (:func:`setup_auth`
            hasn't been run) — both source-level failures the daily batch
            records and moves past.
    """
    settings = get_settings()
    if settings.env.gmail_credentials_path is None:
        raise ExtractorError("GMAIL_CREDENTIALS_PATH is not configured")

    service = _build_service(_get_credentials())
    excluded_labels = set(settings.config.filters.gmail.excluded_labels)

    query = f"after:{int(since.timestamp())}" if since is not None else ""
    message_ids = _list_message_ids(service, query)
    thread_ids = _distinct_thread_ids(service, message_ids)

    logger.info(
        "Gmail extraction starting (since=%s): %d message(s) matched, "
        "%d distinct thread(s)",
        since,
        len(message_ids),
        len(thread_ids),
    )

    items: list[ExtractedItem] = []
    for thread_id in thread_ids:
        try:
            item = _extract_thread(service, thread_id, excluded_labels)
        except Exception as exc:
            logger.warning("Could not extract Gmail thread %s: %s", thread_id, exc)
            continue
        if item is not None:
            items.append(item)

    logger.info(
        "Gmail extraction finished: %d thread(s) scanned, %d item(s) extracted",
        len(thread_ids),
        len(items),
    )
    return items


def _build_service(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _get_credentials():
    """Load the cached, authorized token, refreshing it if expired.

    Never triggers the interactive consent flow itself — see
    :func:`setup_auth` and the module docstring.
    """
    token_path = _token_path()
    if not token_path.is_file():
        raise ExtractorError(
            "Gmail is not authorized yet. Run "
            "`uv run python -m extractors.gmail --setup-auth` once to complete "
            "the one-time browser consent, then re-run ingestion."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise ExtractorError(
            "Cached Gmail credentials are invalid or revoked. Run "
            "`uv run python -m extractors.gmail --setup-auth` again."
        )
    return creds


def setup_auth() -> None:
    """One-time interactive OAuth consent flow — never called by the daily batch.

    Opens a browser for the user to approve read-only Gmail access, then
    caches the resulting (refreshable) credentials to
    ``data/gmail_token.json``. Run via
    ``uv run python -m extractors.gmail --setup-auth``.

    Raises:
        ExtractorError: If ``GMAIL_CREDENTIALS_PATH`` isn't configured or
            the consent flow fails.
    """
    credentials_path = get_settings().env.gmail_credentials_path
    if credentials_path is None:
        raise ExtractorError("GMAIL_CREDENTIALS_PATH is not configured")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), _SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        raise ExtractorError(f"Gmail authorization failed: {exc}") from exc

    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Gmail authorization saved to %s", token_path)


def _list_message_ids(service, query: str) -> list[str]:
    try:
        ids: list[str] = []
        page_token = None
        while True:
            response = (
                service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token, maxResults=500)
                .execute()
            )
            ids.extend(m["id"] for m in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return ids
    except Exception as exc:
        raise ExtractorError(f"Could not list Gmail messages: {exc}") from exc


def _distinct_thread_ids(service, message_ids: list[str]) -> list[str]:
    """Which threads have new activity, in first-seen order.

    Listing already returns each message's ``threadId`` for free
    (``messages.list``'s minimal response format includes it), so this
    doesn't cost any extra API calls.
    """
    seen: dict[str, None] = {}
    for message_id in message_ids:
        try:
            meta = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=[])
                .execute()
            )
        except Exception as exc:
            logger.warning(
                "Could not resolve thread for Gmail message %s: %s", message_id, exc
            )
            continue
        seen.setdefault(meta["threadId"], None)
    return list(seen)


def _extract_thread(
    service, thread_id: str, excluded_labels: set[str]
) -> ExtractedItem | None:
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    messages = thread.get("messages", [])
    if not messages:
        return None

    parts: list[str] = []
    subject = None
    senders: list[str] = []
    dates: list[datetime] = []
    for message in messages:
        label_ids = set(message.get("labelIds", []))
        if label_ids & excluded_labels:
            continue
        headers = _headers(message)
        if subject is None:
            subject = headers.get("subject")
        sender = headers.get("from")
        if sender:
            senders.append(sender)
        date = _parse_date(headers.get("date"))
        if date is not None:
            dates.append(date)

        body = _extract_body(message["payload"])
        attachments = _extract_attachment_texts(
            service, message["id"], message["payload"]
        )
        message_text = "\n".join(
            part
            for part in (
                f"From: {sender}" if sender else None,
                f"Date: {headers.get('date')}" if headers.get("date") else None,
                body,
                *attachments,
            )
            if part
        )
        if message_text.strip():
            parts.append(message_text)

    if not parts:
        # Every message in the thread was filtered out (e.g. an entirely
        # promotional thread) — nothing worth ingesting.
        return None

    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=thread_id,
        title=subject or "(no subject)",
        url_or_path=f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        raw_text="\n\n---\n\n".join(parts),
        author_or_sender=senders[-1] if senders else None,
        created_at=min(dates) if dates else None,
        last_edited_at=max(dates) if dates else None,
    )


def _headers(message: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _extract_body(payload: dict) -> str:
    plain = _find_part(payload, "text/plain")
    if plain is not None:
        return _decode_body(plain)
    html = _find_part(payload, "text/html")
    if html is not None:
        return _strip_html(_decode_body(html))
    return ""


def _find_part(payload: dict, mime_type: str) -> dict | None:
    if payload.get("mimeType") == mime_type and payload.get("body", {}).get("data"):
        return payload
    for part in payload.get("parts", []):
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def _decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", errors="replace"
    )


def _strip_html(html: str) -> str:
    """Crude tag-stripping fallback for HTML-only messages.

    Not a full HTML parser — good enough for "make it readable plain
    text," not for preserving structure, which chunking doesn't need.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _iter_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []):
        yield from _iter_parts(part)


def _extract_attachment_texts(service, message_id: str, payload: dict) -> list[str]:
    texts: list[str] = []
    for part in _iter_parts(payload):
        filename = part.get("filename")
        if not filename:
            continue
        suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if suffix not in _ATTACHABLE_DOC_SUFFIXES:
            continue
        attachment_id = part.get("body", {}).get("attachmentId")
        if not attachment_id:
            continue
        try:
            raw = _fetch_attachment(service, message_id, attachment_id)
            text = _extract_attachment_text(suffix, raw)
        except Exception as exc:
            logger.debug(
                "Could not extract attachment %r from message %s: %s",
                filename,
                message_id,
                exc,
            )
            continue
        if text and text.strip():
            texts.append(f"Attachment ({filename}):\n{text}")
    return texts


def _fetch_attachment(service, message_id: str, attachment_id: str) -> bytes:
    response = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = response.get("data", "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _extract_attachment_text(suffix: str, raw: bytes) -> str:
    if suffix == "pdf":
        return "\n\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(raw)).pages
        )
    if suffix == "docx":
        return "\n".join(p.text for p in docx.Document(BytesIO(raw)).paragraphs)
    return ""


if __name__ == "__main__":
    import sys

    if "--setup-auth" not in sys.argv:
        print("Usage: uv run python -m extractors.gmail --setup-auth")
        sys.exit(1)
    setup_auth()
