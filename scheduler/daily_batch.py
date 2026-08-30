"""Daily batch entrypoint: coordinates every extractor and the ingestion pipeline.

Run via ``uv run python -m scheduler.daily_batch`` (module invocation, not
a raw script path — ``uv run python scheduler/daily_batch.py`` only adds
this file's own directory to ``sys.path``, not the project root, so the
absolute imports below fail with ``ModuleNotFoundError``; verified
directly — see DECISIONS.md, 2026-08-29), registered with cron (Mac/Linux)
or Task Scheduler (Windows) on ``config.yaml``'s ``ingestion.schedule``.
Per ``docs/File_Folder_Structure.docx`` section 4, adding a new source
means adding one entry to ``_EXTRACTORS`` below — no other module needs to
change.

An item that was already ingested and gets re-extracted (edited since its
last ingestion) has its existing relationships cleared before relationship
detection re-runs for it — otherwise ``has_any_relationship()`` would skip
re-judging any candidate it was already related to, silently freezing a
relationship confirmed against content that no longer exists. See
``DECISIONS.md``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import neo4j
from chromadb.api.models.Collection import Collection

from config.settings import get_settings
from extractors import browser_history, local_files, notion
from extractors.base import ExtractedItem, ExtractorError
from pipeline.chunking import chunk_text
from pipeline.embeddings import embed_chunks
from pipeline.filters import apply_noise_filter
from pipeline.metadata import generate_metadata
from pipeline.relationships import detect_relationships
from providers.base import ItemMetadata
from storage.chroma_store import get_collection
from storage.neo4j_store import delete_relationships_for_item, get_driver
from storage.sqlite_store import (
    Item,
    complete_ingestion_run,
    connect,
    delete_item,
    get_last_run_timestamp,
    insert_item,
    replace_chunks,
    start_ingestion_run,
    update_ingestion_run_progress,
)

logger = logging.getLogger(__name__)

_EXTRACTORS: list[tuple[str, Callable[[datetime | None], list[ExtractedItem]]]] = [
    ("local_file", local_files.extract_new_items),
    ("notion", notion.extract_new_items),
    ("browser_history", browser_history.extract_new_items),
]


@dataclass(frozen=True, slots=True)
class _ProcessedItem:
    """The result of storing one item, including whether it already existed."""

    item_id: str
    was_update: bool


def main() -> None:
    """Run one daily ingestion batch: extract, process, and relate every source."""
    conn = connect()
    collection = get_collection()
    driver = get_driver()
    try:
        _run(conn, collection, driver)
    finally:
        driver.close()
        conn.close()


def _run(
    conn: sqlite3.Connection, collection: Collection, driver: neo4j.Driver
) -> None:
    """Run one ingestion batch against already-open storage clients.

    Kept separate from :func:`main` so tests can pass in test doubles
    instead of the real, settings-derived connections.
    """
    since = get_last_run_timestamp(conn)
    run_id = start_ingestion_run(conn)
    errors: list[str] = []
    items_processed = 0
    processed_item_ids: list[str] = []
    updated_item_ids: list[str] = []

    for source_name, extract in _EXTRACTORS:
        try:
            raw_items = extract(since)
        except ExtractorError as exc:
            logger.error("%s extraction failed: %s", source_name, exc)
            errors.append(f"{source_name}: {exc}")
            continue

        filtered_items = [item for item in raw_items if apply_noise_filter(item)]
        if not filtered_items:
            continue
        metadata_list = generate_metadata(filtered_items)

        for item, metadata in zip(filtered_items, metadata_list, strict=True):
            try:
                processed = _process_item(conn, collection, item, metadata)
            except Exception as exc:
                logger.error(
                    "%s: failed to process item %r: %s",
                    source_name,
                    item.source_ref_id,
                    exc,
                )
                errors.append(f"{source_name}/{item.source_ref_id}: {exc}")
                continue
            items_processed += 1
            processed_item_ids.append(processed.item_id)
            if processed.was_update:
                updated_item_ids.append(processed.item_id)
            # Live progress, not just a final count once everything is
            # done — see storage/sqlite_store.py::update_ingestion_run_progress()'s
            # own docstring, DECISIONS.md. A failure here is logged but
            # never aborts the run — a stale progress count is a display
            # quirk, not a reason to lose real ingestion work.
            try:
                update_ingestion_run_progress(conn, run_id, items_processed)
            except Exception as exc:
                logger.warning("Could not update live progress for %r: %s", run_id, exc)

    for item_id in updated_item_ids:
        try:
            # Content changed — existing relationships were judged against
            # what's no longer there. Clear them so has_any_relationship()
            # doesn't skip re-judging every candidate this item is already
            # connected to (see module docstring, DECISIONS.md).
            delete_relationships_for_item(driver, item_id)
        except Exception as exc:
            logger.error("Could not clear stale relationships for %r: %s", item_id, exc)
            errors.append(f"relationships-cleanup/{item_id}: {exc}")

    for item_id in processed_item_ids:
        try:
            detect_relationships(conn, driver, collection, item_id)
        except Exception as exc:
            logger.error("Relationship detection failed for %r: %s", item_id, exc)
            errors.append(f"relationships/{item_id}: {exc}")

    if not errors:
        status = "success"
    elif items_processed > 0:
        status = "partial_failure"
    else:
        status = "failed"
    complete_ingestion_run(
        conn,
        run_id,
        status=status,
        items_processed=items_processed,
        error_log="; ".join(errors) if errors else None,
    )


def _process_item(
    conn: sqlite3.Connection,
    collection: Collection,
    item: ExtractedItem,
    metadata: ItemMetadata,
) -> _ProcessedItem:
    """Store, chunk, and embed one filtered item.

    If chunking/embedding fails after the item row is already written, the
    row is deleted rather than left orphaned with no chunks — an item in
    SQLite either has its chunks too, or doesn't exist.

    Returns:
        The item's effective SQLite id, and whether this updated a
        pre-existing row rather than inserting a new one —
        ``insert_item()``'s upsert keeps an existing row's original id, so
        comparing its return value to the freshly generated id passed in
        is enough to tell the two cases apart without any extra query.
    """
    generated_id = str(uuid4())
    item_id = insert_item(
        conn,
        Item(
            id=generated_id,
            source_type=item.source_type,
            source_ref_id=item.source_ref_id,
            title=item.title,
            url_or_path=item.url_or_path,
            author_or_sender=item.author_or_sender,
            created_at=item.created_at,
            last_edited_at=item.last_edited_at,
            project_name=metadata.project_name,
            topic=metadata.topic,
        ),
    )
    was_update = item_id != generated_id

    try:
        chunks = chunk_text(item.raw_text)
        sqlite_chunks = embed_chunks(
            collection,
            item_id,
            item.source_type,
            chunks,
            project_name=metadata.project_name,
            topic=metadata.topic,
            created_at=item.created_at,
        )
        replace_chunks(conn, item_id, sqlite_chunks)
    except Exception:
        delete_item(conn, item_id)
        raise
    return _ProcessedItem(item_id=item_id, was_update=was_update)


if __name__ == "__main__":
    logging.basicConfig(level=get_settings().env.log_level)
    main()
