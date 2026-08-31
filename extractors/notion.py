"""Notion extractor: workspace pages → normalized items.

Per ``docs/Data_Extraction_Specification.docx`` section 4: every page
reachable by the configured integration is listed via the Notion API,
filtered to those whose ``last_edited_time`` is after ``since``, and their
content pulled as structured blocks (paragraphs, headings, bullet lists,
...) and converted to plain text. Blocks are joined with blank lines so
headings and paragraph breaks remain natural chunking boundaries for
``pipeline/chunking.py``, per section 4.3.

If ``settings.env.notion_page_ids_list`` is non-empty, ingestion is scoped
to just those pages (fetched directly by id) instead of every page the
integration can see — an extension beyond the original spec, configurable
via ``PUT /api/settings/sources``. An empty/unset list keeps the original,
unscoped "whole workspace" behavior. See ``DECISIONS.md``.

A subpage nested under a scoped page is its own ``ExtractedItem`` (own id,
title, ``last_edited_time``, url) — extracted recursively regardless of
whether the *parent* page itself changed, since a subpage's own edits
don't bump its parent's timestamp in Notion, and a subpage otherwise has
no other way to be discovered when ingestion is scoped to specific page
ids rather than the whole workspace. See ``DECISIONS.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from notion_client import Client

from config.settings import get_settings
from extractors.base import ExtractedItem, ExtractorError, OnProgress

logger = logging.getLogger(__name__)

SOURCE_TYPE = "notion"

# A full scan visits every page the integration can see, one API call per
# page's block tree, which can take a long time on a large workspace — log
# progress every N pages rather than staying silent until the run finishes.
_PROGRESS_LOG_INTERVAL = 25


def extract_new_items(
    since: datetime | None = None, on_progress: OnProgress | None = None
) -> list[ExtractedItem]:
    """Extract Notion pages edited after ``since``.

    Args:
        since: Only include pages last edited after this time. ``None``
            (the first-ever run) includes every page the integration can
            see.
        on_progress: Called once per *root* page scanned (not per
            subpage) — ``current``, ``total``, ``label=page title``.
            ``total`` is the configured page count when scoped to
            ``NOTION_PAGE_IDS``, else ``None`` (unscoped/workspace mode
            has no cheap upfront total). Returning ``False`` stops after
            the root page just reported — subpages already discovered
            from earlier root pages are still fully processed first. See
            ``extractors/base.py``, ``DECISIONS.md``.

    Returns:
        One item per successfully read page. A page whose content can't be
        fetched (a transient API error, an unexpected block shape) is
        logged and skipped rather than aborting the rest of the run.

    Raises:
        ExtractorError: If the integration isn't configured, or listing
            pages fails outright (bad token, unreachable API) — a
            source-level failure the daily batch records and moves past.
    """
    settings = get_settings().env
    api_key = settings.notion_api_key
    if not api_key:
        raise ExtractorError("NOTION_API_KEY is not configured")
    client = Client(auth=api_key)

    page_ids = settings.notion_page_ids_list
    pages_iter = (
        _iter_specific_pages(client, page_ids) if page_ids else _iter_pages(client)
    )
    total = len(page_ids) if page_ids else None

    logger.info(
        "Notion extraction starting (since=%s, scope=%s)",
        since,
        f"{len(page_ids)} configured page(s)" if page_ids else "whole workspace",
    )
    items: list[ExtractedItem] = []
    scanned = 0
    # Dedupes a page that's reachable more than one way — every page in
    # unscoped/workspace mode is visited both directly (via search()) and
    # again as a subpage discovered while walking another page's blocks;
    # a page could also be listed directly in a scoped NOTION_PAGE_IDS
    # *and* be a subpage of another configured page.
    seen_page_ids: set[str] = set()
    for page in pages_iter:
        scanned += 1
        if scanned % _PROGRESS_LOG_INTERVAL == 0:
            logger.info(
                "Notion extraction in progress: scanned %d page(s), kept %d",
                scanned,
                len(items),
            )
        items.extend(_extract_page_and_subpages(client, page, since, seen_page_ids))
        if on_progress is not None and not on_progress(
            scanned, total, _extract_title(page)
        ):
            logger.info("Notion extraction stopped early (cancelled)")
            break
    logger.info(
        "Notion extraction finished: scanned %d page(s), extracted %d item(s)",
        scanned,
        len(items),
    )
    return items


def _iter_specific_pages(client: Client, page_ids: list[str]):
    """Fetch each configured page directly by id, skipping the workspace search.

    A page fetched via ``client.pages.retrieve()`` is the same shape
    (``id``, ``properties``, ``url``, ``last_edited_time``,
    ``created_time``) as one returned by ``client.search()`` in
    :func:`_iter_pages`, so :func:`_extract_page_and_subpages` works
    identically either way. A page id that's been deleted, or that the
    integration no longer has access to, is logged and skipped rather
    than aborting the rest of the configured scope.
    """
    for page_id in page_ids:
        try:
            yield client.pages.retrieve(page_id=page_id)
        except Exception as exc:
            logger.warning(
                "Could not fetch configured Notion page %s: %s", page_id, exc
            )


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


def _extract_page_and_subpages(
    client: Client,
    page: dict,
    since: datetime | None,
    seen_page_ids: set[str],
) -> list[ExtractedItem]:
    """Extract ``page`` (if it qualifies) plus every subpage nested in it.

    Blocks are always walked, even when ``page`` itself doesn't pass the
    ``since`` filter — otherwise a subpage nested under an unchanged parent
    could never be discovered at all when ingestion is scoped to specific
    page ids (a subpage isn't independently listed anywhere; the only way
    to find it is by walking its parent's blocks). Each subpage found gets
    its own independent ``since`` check and recursion into its own
    subpages, exactly as if it had been configured directly. See
    ``DECISIONS.md``.
    """
    page_id = page["id"]
    if page_id in seen_page_ids:
        return []
    seen_page_ids.add(page_id)

    last_edited_at = _parse_timestamp(page.get("last_edited_time"))
    if last_edited_at is None:
        logger.warning("Notion page %s has no last_edited_time, skipping", page_id)
        return []

    try:
        text, subpage_blocks = _page_text_and_subpages(client, page_id)
    except Exception as exc:
        logger.warning("Could not extract text for Notion page %s: %s", page_id, exc)
        return []

    items: list[ExtractedItem] = []
    qualifies = since is None or last_edited_at > since
    if qualifies and text.strip():
        items.append(
            ExtractedItem(
                source_type=SOURCE_TYPE,
                source_ref_id=page_id,
                title=_extract_title(page),
                url_or_path=page.get("url", ""),
                raw_text=text,
                author_or_sender=None,
                created_at=_parse_timestamp(page.get("created_time")),
                last_edited_at=last_edited_at,
            )
        )

    for block in subpage_blocks:
        try:
            subpage = client.pages.retrieve(page_id=block["id"])
        except Exception as exc:
            logger.warning("Could not fetch Notion subpage %s: %s", block["id"], exc)
            continue
        items.extend(_extract_page_and_subpages(client, subpage, since, seen_page_ids))
    return items


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


def _page_text_and_subpages(client: Client, block_id: str) -> tuple[str, list[dict]]:
    parts: list[str] = []
    subpages: list[dict] = []
    _collect_block_text(client, block_id, parts, subpages)
    return "\n\n".join(part for part in parts if part), subpages


def _collect_block_text(
    client: Client, block_id: str, parts: list[str], subpages: list[dict]
) -> None:
    cursor = None
    while True:
        response = client.blocks.children.list(block_id=block_id, start_cursor=cursor)
        for block in response["results"]:
            if block.get("type") == "child_page":
                # A nested Notion page — extracted separately as its own
                # item (own since-filtering, own title/url), never folded
                # into this page's text. See DECISIONS.md.
                subpages.append(block)
                continue
            parts.append(_block_to_text(block))
            if block.get("has_children"):
                _collect_block_text(client, block["id"], parts, subpages)
        if not response.get("has_more"):
            return
        cursor = response["next_cursor"]


def _block_to_text(block: dict) -> str:
    content = block.get(block.get("type"), {})
    rich_text = content.get("rich_text")
    if rich_text is None:
        return ""
    return _rich_text_to_plain(rich_text)
