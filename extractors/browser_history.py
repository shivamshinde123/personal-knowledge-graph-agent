"""Browser history extractor: the local Chromium ``History`` SQLite file → items.

Per ``docs/Data_Extraction_Specification.docx`` section 8, this is
deliberately the lightest-weight source: only page title, URL, visit
timestamp, and visit count are read — no page content is fetched — and only
the title becomes the item's embedded/searchable text
(``ExtractedItem.raw_text``). Domain-blocklist and visit-count noise
filtering happen here, inside the extractor, rather than in
``pipeline/filters.py``, since ``ExtractedItem`` has no ``visit_count`` or
URL-domain field for a generic cross-source rule to act on — see
``DECISIONS.md``.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from config.settings import BrowserHistoryFilters, get_settings
from extractors.base import ExtractedItem, ExtractorError

logger = logging.getLogger(__name__)

SOURCE_TYPE = "browser_history"

# Chrome/WebKit timestamps are microseconds since this epoch, not Unix epoch.
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


def extract_new_items(since: datetime | None = None) -> list[ExtractedItem]:
    """Extract browser history entries last visited after ``since``.

    Args:
        since: Only include entries last visited after this time. ``None``
            (the first-ever run) includes every entry surviving the noise
            filters below.

    Returns:
        One item per URL surviving the domain blocklist and minimum
        visit-count threshold (``config.yaml``'s
        ``filters.browser_history``).

    Raises:
        ExtractorError: If ``BROWSER_HISTORY_PATH`` isn't configured, the
            file doesn't exist, or it can't be read — a source-level
            failure the daily batch records and moves past.
    """
    settings = get_settings()
    history_path = settings.env.browser_history_path
    if history_path is None:
        raise ExtractorError("BROWSER_HISTORY_PATH is not configured")
    if not history_path.is_file():
        raise ExtractorError(f"Browser history file not found: {history_path}")

    rows = _read_urls_table(history_path)
    filters = settings.config.filters.browser_history

    items: list[ExtractedItem] = []
    for url, title, visit_count, last_visit_time in rows:
        item = _to_item(url, title, visit_count, last_visit_time, since, filters)
        if item is not None:
            items.append(item)
    return items


def _read_urls_table(history_path: Path) -> list[tuple]:
    """Read ``(url, title, visit_count, last_visit_time)`` from a copy of the file.

    The browser holds an exclusive lock on its live history file while
    running, so this reads a temporary copy rather than the original —
    otherwise every run would fail with "database is locked" whenever the
    browser is open, which is most of the time.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_copy = Path(tmp_dir) / "History"
            shutil.copy2(history_path, tmp_copy)
            conn = sqlite3.connect(tmp_copy)
            try:
                return conn.execute(
                    "SELECT url, title, visit_count, last_visit_time FROM urls"
                ).fetchall()
            finally:
                conn.close()
    except Exception as exc:
        raise ExtractorError(f"Could not read browser history: {exc}") from exc


def _to_item(
    url: str,
    title: str | None,
    visit_count: int,
    last_visit_time: int | None,
    since: datetime | None,
    filters: BrowserHistoryFilters,
) -> ExtractedItem | None:
    if not title or not title.strip():
        return None
    if visit_count < filters.min_visit_count:
        return None
    if _is_blocked(url, filters.domain_blocklist):
        return None
    visited_at = _webkit_to_datetime(last_visit_time)
    if visited_at is None:
        return None
    if since is not None and visited_at <= since:
        return None
    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=url,
        title=title,
        url_or_path=url,
        raw_text=title,
        author_or_sender=None,
        created_at=None,
        last_edited_at=visited_at,
    )


def _is_blocked(url: str, domain_blocklist: list[str]) -> bool:
    """Whether ``url`` matches any blocklist entry, by substring.

    Entries are either a bare domain (``"facebook.com"``) or a
    domain-plus-path prefix (``"google.com/search"``); substring
    containment against the full URL handles both without needing to parse
    the URL, and correctly leaves e.g. ``google.com/maps`` unblocked when
    only ``google.com/search`` is listed.
    """
    return any(entry in url for entry in domain_blocklist)


def _webkit_to_datetime(webkit_timestamp: int | None) -> datetime | None:
    """Convert a Chrome/WebKit timestamp to a UTC ``datetime``."""
    if not webkit_timestamp:
        return None
    return _WEBKIT_EPOCH + timedelta(microseconds=webkit_timestamp)
