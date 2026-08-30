"""Sources status: the result of the most recent daily batch run, per source.

Lives in the agent layer for the same reason ``agent/health.py`` does —
``api/__init__.py``'s docstring says the API layer never reaches into
``storage``/``providers`` directly, only the agent's public entrypoints.
See ``DECISIONS.md``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from storage.sqlite_store import IngestionRun, get_last_ingestion_run

# The six sources this project ingests from, per CLAUDE.md's locked-in
# scope — listed even if their extractor doesn't exist yet, so the status
# screen shows every source's real state (0 items, not an error) rather
# than silently omitting rows for unbuilt extractors.
_SOURCE_TYPES = (
    "local_file",
    "notion",
    "gmail",
    "github",
    "calendar",
    "browser_history",
)


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """One source's outcome in the most recent run, plus its running total."""

    source_type: str
    items_processed: int
    total_items: int
    status: Literal["ok", "error"]


@dataclass(frozen=True, slots=True)
class SourcesStatus:
    """The most recent run overall, plus each source's individual outcome."""

    last_run: IngestionRun | None
    sources: list[SourceStatus]


def get_sources_status(conn: sqlite3.Connection) -> SourcesStatus:
    """Report the most recent batch run's outcome, per source.

    Args:
        conn: An open SQLite connection.

    Returns:
        ``last_run`` is ``None`` if the batch has never run, in which case
        every source reports 0 for both counts and ``"ok"`` (nothing has
        failed — nothing has run yet, either). Otherwise, each source's
        ``items_processed`` is the count of items with that ``source_type``
        ingested during the *most recent run's* window (``ingested_at``
        between ``run_started_at`` and ``run_completed_at``, or through now
        if the run is still in progress) — legitimately 0 whenever a run
        finds nothing new to ingest, which is not itself a problem, but
        reads as "nothing has ever been ingested" without ``total_items``
        alongside it for context (see DECISIONS.md). ``status`` is
        ``"error"`` if the run's ``error_log`` mentions that source by
        name, else ``"ok"`` — an approximation, not a stored per-source
        breakdown, since ``ingestion_runs`` only tracks a single aggregate
        count and error log across all sources (see DECISIONS.md,
        2026-08-25).
    """
    last_run = get_last_ingestion_run(conn)
    if last_run is None:
        return SourcesStatus(
            last_run=None,
            sources=[
                SourceStatus(
                    source_type=source_type,
                    items_processed=0,
                    total_items=_count_total_items(conn, source_type),
                    status="ok",
                )
                for source_type in _SOURCE_TYPES
            ],
        )

    sources = [
        SourceStatus(
            source_type=source_type,
            items_processed=_count_items_in_window(conn, source_type, last_run),
            total_items=_count_total_items(conn, source_type),
            status=_source_status(source_type, last_run),
        )
        for source_type in _SOURCE_TYPES
    ]
    return SourcesStatus(last_run=last_run, sources=sources)


def _count_total_items(conn: sqlite3.Connection, source_type: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM items WHERE source_type = ?", (source_type,)
    ).fetchone()
    return row["c"]


def _count_items_in_window(
    conn: sqlite3.Connection, source_type: str, run: IngestionRun
) -> int:
    end = run.run_completed_at.isoformat() if run.run_completed_at else None
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM items WHERE source_type = ? "
        "AND ingested_at >= ? AND (? IS NULL OR ingested_at <= ?)",
        (source_type, run.run_started_at.isoformat(), end, end),
    ).fetchone()
    return row["c"]


def _source_status(source_type: str, run: IngestionRun) -> Literal["ok", "error"]:
    if run.error_log and source_type in run.error_log:
        return "error"
    return "ok"
