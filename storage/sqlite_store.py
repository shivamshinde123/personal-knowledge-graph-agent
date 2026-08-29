"""SQLite storage: items, chunks, and ingestion run tracking.

This module owns the raw-text and reference-metadata store defined in
``docs/Database_Schema.docx``. It belongs to the storage layer: it imports
only ``config.settings`` and holds the only live SQLite connections in the
system, per ``docs/Component_Map.docx`` and ``docs/Coding_Conventions.docx``.

Typical use::

    from storage.sqlite_store import connect, insert_item, replace_chunks

    conn = connect()
    item_id = insert_item(conn, item)
    replace_chunks(conn, item_id, chunks)
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from config.settings import get_settings

logger = logging.getLogger(__name__)

IngestionStatus = Literal["running", "success", "partial_failure", "failed"]


class StorageError(Exception):
    """Raised when a SQLite storage operation fails."""


@dataclass(slots=True)
class Item:
    """One ingested item: a Notion page, email, commit, event, file, or history."""

    id: str
    source_type: str
    source_ref_id: str
    title: str | None = None
    url_or_path: str | None = None
    author_or_sender: str | None = None
    created_at: datetime | None = None
    last_edited_at: datetime | None = None
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    project_name: str | None = None
    topic: str | None = None


@dataclass(slots=True)
class Chunk:
    """One chunk of an item's text, paired with its Chroma ``embedding_id``."""

    id: str
    item_id: str
    chunk_index: int
    text: str
    embedding_id: str
    token_count: int | None = None


@dataclass(slots=True)
class SearchResult:
    """A chunk matched by keyword search, with its BM25 rank score."""

    chunk: Chunk
    score: float


@dataclass(slots=True)
class IngestionRun:
    """One daily batch execution, used for reliability tracking."""

    id: str
    run_started_at: datetime
    status: IngestionStatus
    run_completed_at: datetime | None = None
    items_processed: int = 0
    error_log: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id                TEXT PRIMARY KEY,
  source_type       TEXT NOT NULL,
  source_ref_id     TEXT NOT NULL,
  title             TEXT,
  url_or_path       TEXT,
  author_or_sender  TEXT,
  created_at        TIMESTAMP,
  last_edited_at    TIMESTAMP,
  ingested_at       TIMESTAMP NOT NULL,
  project_name      TEXT,
  topic             TEXT,
  UNIQUE(source_type, source_ref_id)
);

CREATE TABLE IF NOT EXISTS chunks (
  id             TEXT PRIMARY KEY,
  item_id        TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  chunk_index    INTEGER NOT NULL,
  text           TEXT NOT NULL,
  token_count    INTEGER,
  embedding_id   TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_chunks_item_id ON chunks(item_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, content='chunks', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
  INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS ingestion_runs (
  id                 TEXT PRIMARY KEY,
  run_started_at     TIMESTAMP NOT NULL,
  run_completed_at   TIMESTAMP,
  status             TEXT NOT NULL,
  items_processed    INTEGER DEFAULT 0,
  error_log          TEXT
);
"""

_INSERT_ITEM = """
INSERT INTO items (
    id, source_type, source_ref_id, title, url_or_path, author_or_sender,
    created_at, last_edited_at, ingested_at, project_name, topic
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source_type, source_ref_id) DO UPDATE SET
    title = excluded.title,
    url_or_path = excluded.url_or_path,
    author_or_sender = excluded.author_or_sender,
    created_at = excluded.created_at,
    last_edited_at = excluded.last_edited_at,
    ingested_at = excluded.ingested_at,
    project_name = excluded.project_name,
    topic = excluded.topic
"""


def _to_iso(value: datetime | None) -> str | None:
    """Serialize a datetime for storage, leaving ``None`` as ``None``."""
    return None if value is None else value.isoformat()


def _from_iso(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp back into a datetime."""
    return None if value is None else datetime.fromisoformat(value)


def _item_from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source_type=row["source_type"],
        source_ref_id=row["source_ref_id"],
        title=row["title"],
        url_or_path=row["url_or_path"],
        author_or_sender=row["author_or_sender"],
        created_at=_from_iso(row["created_at"]),
        last_edited_at=_from_iso(row["last_edited_at"]),
        ingested_at=_from_iso(row["ingested_at"]),
        project_name=row["project_name"],
        topic=row["topic"],
    )


def _chunk_from_row(row: sqlite3.Row) -> Chunk:
    return Chunk(
        id=row["id"],
        item_id=row["item_id"],
        chunk_index=row["chunk_index"],
        text=row["text"],
        embedding_id=row["embedding_id"],
        token_count=row["token_count"],
    )


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with the schema initialized.

    Args:
        db_path: Path to the database file, or the literal ``":memory:"`` for
            an in-process database (used by tests). Defaults to
            ``settings.env.sqlite_db_path``.

    Returns:
        An open connection with ``row_factory`` set to ``sqlite3.Row`` and
        foreign key enforcement turned on. Opened with
        ``check_same_thread=False`` since ``api/main.py`` holds one
        connection for the app's lifetime and FastAPI runs sync route
        handlers in a threadpool — see DECISIONS.md. Callers doing their
        own multi-threaded concurrent access are responsible for
        serializing writes themselves; this only lifts sqlite3's
        same-thread restriction, it doesn't add locking.

    Raises:
        StorageError: If the connection or schema initialization fails.
    """
    target: Path | str = (
        get_settings().env.sqlite_db_path if db_path is None else db_path
    )
    try:
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        conn.commit()
    except sqlite3.Error as exc:
        raise StorageError(
            f"Could not open SQLite database at {target!r}: {exc}"
        ) from exc
    logger.info("Opened SQLite connection at %s", target)
    return conn


def insert_item(conn: sqlite3.Connection, item: Item) -> str:
    """Insert an item, or update it in place if its source ref was already ingested.

    Upserts on the ``(source_type, source_ref_id)`` unique key rather than
    inserting only, so a source item edited since its last ingestion is
    updated rather than silently skipped. The row's ``id`` is not part of the
    conflict update, so an existing row keeps its original id — callers must
    use the returned value, not ``item.id``, when inserting that item's chunks.

    Args:
        conn: An open connection from :func:`connect`.
        item: The item to insert or update.

    Returns:
        The effective item id: ``item.id`` for a new row, or the pre-existing
        row's id if this source ref was already ingested.

    Raises:
        StorageError: If the insert fails.
    """
    try:
        conn.execute(
            _INSERT_ITEM,
            (
                item.id,
                item.source_type,
                item.source_ref_id,
                item.title,
                item.url_or_path,
                item.author_or_sender,
                _to_iso(item.created_at),
                _to_iso(item.last_edited_at),
                _to_iso(item.ingested_at),
                item.project_name,
                item.topic,
            ),
        )
        row = conn.execute(
            "SELECT id FROM items WHERE source_type = ? AND source_ref_id = ?",
            (item.source_type, item.source_ref_id),
        ).fetchone()
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"Could not insert item {item.id!r}: {exc}") from exc
    return row["id"]


def get_item(conn: sqlite3.Connection, item_id: str) -> Item | None:
    """Fetch a single item by id.

    Args:
        conn: An open connection from :func:`connect`.
        item_id: The item's id.

    Returns:
        The item, or ``None`` if no item has that id.
    """
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return None if row is None else _item_from_row(row)


def delete_item(conn: sqlite3.Connection, item_id: str) -> None:
    """Delete an item and cascade-delete its chunks.

    Does not touch Chroma or Neo4j; per ``docs/Database_Schema.docx`` section
    5, callers deleting an item must also remove it from those stores
    explicitly, since neither enforces foreign keys against SQLite.

    Args:
        conn: An open connection from :func:`connect`.
        item_id: The item's id.

    Raises:
        StorageError: If the delete fails.
    """
    try:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"Could not delete item {item_id!r}: {exc}") from exc


def insert_chunk(conn: sqlite3.Connection, chunk: Chunk) -> None:
    """Insert a single chunk.

    Args:
        conn: An open connection from :func:`connect`.
        chunk: The chunk to insert. Its ``item_id`` must reference an
            existing item.

    Raises:
        StorageError: If the insert fails, including a foreign key violation
            when ``chunk.item_id`` does not exist.
    """
    try:
        conn.execute(
            "INSERT INTO chunks (id, item_id, chunk_index, text, token_count, "
            "embedding_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk.id,
                chunk.item_id,
                chunk.chunk_index,
                chunk.text,
                chunk.token_count,
                chunk.embedding_id,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"Could not insert chunk {chunk.id!r}: {exc}") from exc


def replace_chunks(
    conn: sqlite3.Connection, item_id: str, chunks: Iterable[Chunk]
) -> None:
    """Replace all of an item's chunks with a new set, in one transaction.

    Used when re-ingesting an edited item: the old chunk boundaries no
    longer apply, so stale chunks (and their FTS entries) are removed before
    the freshly chunked text is inserted.

    Args:
        conn: An open connection from :func:`connect`.
        item_id: The item whose chunks are being replaced. Each chunk is
            inserted under this id regardless of its own ``chunk.item_id``,
            so a caller cannot accidentally attach a chunk to the wrong item.
        chunks: The new chunks.

    Raises:
        StorageError: If the replacement fails; the transaction is rolled
            back so the item is left with its previous chunks.
    """
    try:
        conn.execute("DELETE FROM chunks WHERE item_id = ?", (item_id,))
        conn.executemany(
            "INSERT INTO chunks (id, item_id, chunk_index, text, token_count, "
            "embedding_id) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    c.id,
                    item_id,
                    c.chunk_index,
                    c.text,
                    c.token_count,
                    c.embedding_id,
                )
                for c in chunks
            ],
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(
            f"Could not replace chunks for item {item_id!r}: {exc}"
        ) from exc


def get_chunks_for_item(conn: sqlite3.Connection, item_id: str) -> list[Chunk]:
    """Fetch all chunks belonging to an item, in chunk order.

    Args:
        conn: An open connection from :func:`connect`.
        item_id: The item's id.

    Returns:
        The item's chunks ordered by ``chunk_index``.
    """
    rows = conn.execute(
        "SELECT * FROM chunks WHERE item_id = ? ORDER BY chunk_index", (item_id,)
    ).fetchall()
    return [_chunk_from_row(row) for row in rows]


def keyword_search(
    conn: sqlite3.Connection, query: str, top_k: int = 8
) -> list[SearchResult]:
    """Run a BM25-ranked full-text search over chunk text.

    Args:
        conn: An open connection from :func:`connect`.
        query: An FTS5 match expression (e.g. ``"vector search"``).
        top_k: Maximum number of results to return.

    Returns:
        Matching chunks ordered by relevance (best match first).

    Raises:
        StorageError: If the query is not valid FTS5 syntax.
    """
    try:
        rows = conn.execute(
            """
            SELECT c.*, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.rowid = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, top_k),
        ).fetchall()
    except sqlite3.Error as exc:
        raise StorageError(f"Keyword search failed for query {query!r}: {exc}") from exc
    return [SearchResult(chunk=_chunk_from_row(row), score=row["rank"]) for row in rows]


def start_ingestion_run(conn: sqlite3.Connection) -> str:
    """Record the start of a daily batch run.

    Args:
        conn: An open connection from :func:`connect`.

    Returns:
        The new run's id.

    Raises:
        StorageError: If the insert fails.
    """
    run_id = str(uuid4())
    try:
        conn.execute(
            "INSERT INTO ingestion_runs (id, run_started_at, status, "
            "items_processed) VALUES (?, ?, 'running', 0)",
            (run_id, _to_iso(datetime.now(UTC))),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"Could not start ingestion run: {exc}") from exc
    return run_id


def complete_ingestion_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: IngestionStatus,
    items_processed: int,
    error_log: str | None = None,
) -> None:
    """Record the completion of a daily batch run.

    Args:
        conn: An open connection from :func:`connect`.
        run_id: The run's id, from :func:`start_ingestion_run`.
        status: The run's final status. Only ``"success"`` advances the
            watermark returned by :func:`get_last_run_timestamp`.
        items_processed: Total items processed across all sources.
        error_log: Aggregated error details, if any source failed.

    Raises:
        StorageError: If the update fails.
    """
    try:
        conn.execute(
            "UPDATE ingestion_runs SET run_completed_at = ?, status = ?, "
            "items_processed = ?, error_log = ? WHERE id = ?",
            (_to_iso(datetime.now(UTC)), status, items_processed, error_log, run_id),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(
            f"Could not complete ingestion run {run_id!r}: {exc}"
        ) from exc


def get_last_run_timestamp(conn: sqlite3.Connection) -> datetime | None:
    """Return the completion time of the most recent successful batch run.

    Args:
        conn: An open connection from :func:`connect`.

    Returns:
        The watermark for ``extract_new_items(since=...)``, or ``None`` if no
        run has ever succeeded.
    """
    row = conn.execute(
        "SELECT MAX(run_completed_at) AS ts FROM ingestion_runs "
        "WHERE status = 'success'"
    ).fetchone()
    return _from_iso(row["ts"])
