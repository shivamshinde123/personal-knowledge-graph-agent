"""Daily batch entrypoint: coordinates every extractor and the ingestion pipeline.

Run via ``uv run python scheduler/daily_batch.py``, registered with cron
(Mac/Linux) or Task Scheduler (Windows) on ``config.yaml``'s
``ingestion.schedule``. Per ``docs/File_Folder_Structure.docx`` section 4,
adding a new source means adding one entry to ``_EXTRACTORS`` below — no
other module needs to change.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

import neo4j
from chromadb.api.models.Collection import Collection

from config.settings import get_settings
from extractors import local_files, notion
from extractors.base import ExtractedItem, ExtractorError
from pipeline.chunking import chunk_text
from pipeline.embeddings import embed_chunks
from pipeline.filters import apply_noise_filter
from pipeline.metadata import generate_metadata
from pipeline.relationships import detect_relationships
from providers.base import ItemMetadata
from storage.chroma_store import get_collection
from storage.neo4j_store import get_driver
from storage.sqlite_store import (
    Item,
    complete_ingestion_run,
    connect,
    delete_item,
    get_last_run_timestamp,
    insert_item,
    replace_chunks,
    start_ingestion_run,
)

logger = logging.getLogger(__name__)

_EXTRACTORS: list[tuple[str, Callable[[datetime | None], list[ExtractedItem]]]] = [
    ("local_file", local_files.extract_new_items),
    ("notion", notion.extract_new_items),
]


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
    new_item_ids: list[str] = []

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
                item_id = _process_item(conn, collection, item, metadata)
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
            new_item_ids.append(item_id)

    for item_id in new_item_ids:
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
) -> str:
    """Store, chunk, and embed one filtered item.

    If chunking/embedding fails after the item row is already written, the
    row is deleted rather than left orphaned with no chunks — an item in
    SQLite either has its chunks too, or doesn't exist.

    Returns:
        The item's effective SQLite id.
    """
    item_id = insert_item(
        conn,
        Item(
            id=str(uuid4()),
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
    return item_id


if __name__ == "__main__":
    logging.basicConfig(level=get_settings().env.log_level)
    main()
