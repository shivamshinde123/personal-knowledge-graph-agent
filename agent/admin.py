"""Admin operations: full-database reset, for ``POST /api/admin/reset``.

Lives in the agent layer, not ``api/``, for the same reason as every other
thin wrapper here (``agent/health.py``, ``agent/sources_status.py``,
``agent/ingest_trigger.py``, ``agent/graph_view.py``) — the API layer
depends only on the agent's public entrypoints, never on ``storage``
directly (see ``api/__init__.py``, ``docs/Component_Map.docx``).

A destructive, hard-to-reverse operation — wiping SQLite, Chroma, and
Neo4j back to empty, so the next ingestion run starts completely fresh.
Requested directly by the user for local development/testing (e.g.
recovering from a bad ingestion run, or restarting a demo from a clean
slate). See ``DECISIONS.md``.
"""

from __future__ import annotations

import sqlite3

import neo4j
from chromadb.api.models.Collection import Collection

from storage.chroma_store import VectorStoreError
from storage.chroma_store import reset_all as reset_chroma
from storage.neo4j_store import GraphStoreError
from storage.neo4j_store import reset_all as reset_neo4j
from storage.sqlite_store import StorageError
from storage.sqlite_store import reset_all as reset_sqlite


class AdminError(Exception):
    """Raised when a full reset only partially succeeds.

    The three stores have no shared transaction — each is reset
    independently, so a failure partway through can leave some stores
    wiped and others untouched. Every store is still attempted, even after
    an earlier one fails, so the reset gets as far as it can rather than
    stopping at the first error; the message names exactly which store(s)
    failed so the caller knows what state things were left in.
    """


def reset_all_data(
    conn: sqlite3.Connection, collection: Collection, driver: neo4j.Driver
) -> None:
    """Wipe SQLite, Chroma, and Neo4j back to empty.

    Args:
        conn: An open SQLite connection from ``storage/sqlite_store.py::connect()``.
        collection: An open Chroma collection from
            ``storage/chroma_store.py::get_collection()``.
        driver: An open Neo4j driver from ``storage/neo4j_store.py::get_driver()``.

    Raises:
        AdminError: If one or more stores failed to reset. Every store is
            attempted regardless of an earlier failure — see the class
            docstring.
    """
    failures: list[str] = []

    try:
        reset_sqlite(conn)
    except StorageError as exc:
        failures.append(f"sqlite: {exc}")

    try:
        reset_chroma(collection)
    except VectorStoreError as exc:
        failures.append(f"chroma: {exc}")

    try:
        reset_neo4j(driver)
    except GraphStoreError as exc:
        failures.append(f"neo4j: {exc}")

    if failures:
        raise AdminError("Reset did not fully succeed — " + "; ".join(failures))
