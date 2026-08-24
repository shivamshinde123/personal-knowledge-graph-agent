"""Notion extractor: workspace pages → normalized items.

Per ``docs/Data_Extraction_Specification.docx`` section 4: during the daily
batch, every page reachable by the configured integration is listed via the
Notion API, filtered to those whose ``last_edited_time`` is after ``since``,
and their content pulled as structured blocks (paragraphs, headings, bullet
lists, ...) and converted to plain text. Blocks are joined with blank lines
so headings and paragraph breaks remain natural chunking boundaries for
``pipeline/chunking.py``, per section 4.3.
"""

from __future__ import annotations

import logging
from datetime import datetime

from notion_client import Client

from config.settings import get_settings
from extractors.base import ExtractedItem, ExtractorError

logger = logging.getLogger(__name__)

SOURCE_TYPE = "notion"

# A full scan visits every page the integration can see, one API call per
# page's block tree, which can take a long time on a large workspace — log
# progress every N pages rather than staying silent until the run finishes.
_PROGRESS_LOG_INTERVAL = 25


def extract_new_items(since: datetime | None = None) -> list[ExtractedItem]:
    """Extract Notion pages edited after ``since``.

    Args:
        since: Only include pages last edited after this time. ``None``
            (the first-ever run) includes every page the integration can
            see.

    Returns:
        One item per successfully read page. A page whose content can't be
        fetched (a transient API error, an unexpected block shape) is
        logged and skipped rather than aborting the rest of the run.

    Raises:
        ExtractorError: If the integration isn't configured, or listing
            pages fails outright (bad token, unreachable API) — a
            source-level failure the daily batch records and moves past.
    """
    api_key = get_settings().env.notion_api_key
    if not api_key:
        raise ExtractorError("NOTION_API_KEY is not configured")
    client = Client(auth=api_key)

    logger.info("Notion extraction starting (since=%s)", since)
    items: list[ExtractedItem] = []
    scanned = 0
    for page in _iter_pages(client):
        scanned += 1
        if scanned % _PROGRESS_LOG_INTERVAL == 0:
            logger.info(
                "Notion extraction in progress: scanned %d page(s), kept %d",
                scanned,
                len(items),
            )
        item = _extract_item(client, page, since)
        if item is not None:
            items.append(item)
    logger.info(
        "Notion extraction finished: scanned %d page(s), extracted %d item(s)",
        scanned,
        len(items),
    )
    return items


def _iter_pages(client: Client):
    try:
        cursor = None
        page_number = 0
        while True:
            response = client.search(
                filter={"property": "object", "value": "page"},
                start_cursor=cursor,
            )
            page_number += 1
            logger.debug(
                "Notion search page %d returned %d result(s)",
                page_number,
                len(response["results"]),
            )
            yield from response["results"]
            if not response.get("has_more"):
                return
            cursor = response["next_cursor"]
    except Exception as exc:
        raise ExtractorError(f"Could not list Notion pages: {exc}") from exc


def _extract_item(
    client: Client, page: dict, since: datetime | None
) -> ExtractedItem | None:
    page_id = page["id"]
    last_edited_at = _parse_timestamp(page.get("last_edited_time"))
    if last_edited_at is None:
        logger.warning("Notion page %s has no last_edited_time, skipping", page_id)
        return None
    if since is not None and last_edited_at <= since:
        return None
    try:
        text = _page_text(client, page_id)
    except Exception as exc:
        logger.warning("Could not extract text for Notion page %s: %s", page_id, exc)
        return None
    if not text.strip():
        return None
    return ExtractedItem(
        source_type=SOURCE_TYPE,
        source_ref_id=page_id,
        title=_extract_title(page),
        url_or_path=page.get("url", ""),
        raw_text=text,
        author_or_sender=None,
        created_at=_parse_timestamp(page.get("created_time")),
        last_edited_at=last_edited_at,
    )


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title", [])) or "Untitled"
    return "Untitled"


def _rich_text_to_plain(rich_text: list[dict]) -> str:
    return "".join(segment.get("plain_text", "") for segment in rich_text)


def _page_text(client: Client, block_id: str) -> str:
    parts: list[str] = []
    _collect_block_text(client, block_id, parts)
    return "\n\n".join(part for part in parts if part)


def _collect_block_text(client: Client, block_id: str, parts: list[str]) -> None:
    cursor = None
    while True:
        response = client.blocks.children.list(block_id=block_id, start_cursor=cursor)
        for block in response["results"]:
            parts.append(_block_to_text(block))
            if block.get("has_children"):
                _collect_block_text(client, block["id"], parts)
        if not response.get("has_more"):
            return
        cursor = response["next_cursor"]


def _block_to_text(block: dict) -> str:
    content = block.get(block.get("type"), {})
    rich_text = content.get("rich_text")
    if rich_text is None:
        return ""
    return _rich_text_to_plain(rich_text)
