"""Health check: reports whether every backing service is reachable.

Lives in the agent layer, not ``api/``, even though it isn't on the query
path — ``api/__init__.py``'s own docstring states the API layer depends
only on the agent's public entrypoints and never reaches into ``storage``
or ``providers`` directly, and ``agent/__init__.py``'s docstring is the
layer explicitly allowed to depend on both. See ``DECISIONS.md``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

import neo4j
from chromadb.api.models.Collection import Collection

from providers.base import ProviderError, get_provider

ServiceStatus = Literal["ok", "error"]


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Whether the whole system is healthy, and each backing service."""

    status: Literal["ok", "degraded"]
    services: dict[str, ServiceStatus]


def check_health(
    conn: sqlite3.Connection, collection: Collection, driver: neo4j.Driver
) -> HealthStatus:
    """Check every backing service the agent depends on.

    Each check is cheap and local — in particular, the ``llm_provider``
    check never makes a real LLM API call, only confirms the currently
    configured provider can be constructed (catches the most common
    failure, a missing API key, without cost or latency).

    Args:
        conn: An open SQLite connection.
        collection: An open Chroma collection.
        driver: An open Neo4j driver.

    Returns:
        The overall status (``"ok"`` only if every service reports
        ``"ok"``, else ``"degraded"``) and each service's individual
        status.
    """
    services: dict[str, ServiceStatus] = {
        "sqlite": _check_sqlite(conn),
        "chroma": _check_chroma(collection),
        "neo4j": _check_neo4j(driver),
        "llm_provider": _check_llm_provider(),
    }
    status: Literal["ok", "degraded"] = (
        "ok" if all(s == "ok" for s in services.values()) else "degraded"
    )
    return HealthStatus(status=status, services=services)


def _check_sqlite(conn: sqlite3.Connection) -> ServiceStatus:
    try:
        conn.execute("SELECT 1")
        return "ok"
    except Exception:
        return "error"


def _check_chroma(collection: Collection) -> ServiceStatus:
    try:
        collection.count()
        return "ok"
    except Exception:
        return "error"


def _check_neo4j(driver: neo4j.Driver) -> ServiceStatus:
    try:
        driver.verify_connectivity()
        return "ok"
    except Exception:
        return "error"


def _check_llm_provider() -> ServiceStatus:
    try:
        get_provider("answer")
        return "ok"
    except ProviderError:
        return "error"
