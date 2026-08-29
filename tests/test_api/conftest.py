"""Shared fixtures for API tests.

Every test builds the app with a no-op lifespan and sets ``app.state``
directly to test doubles, so a test run never opens the real,
settings-derived SQLite/Chroma/Neo4j connections that the real lifespan in
``api/main.py`` would — same reasoning as
``tests/test_scheduler/test_daily_batch.py``'s ``_EXTRACTORS`` scoping
fixture (see DECISIONS.md, 2026-08-24).
"""

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import neo4j
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from storage.chroma_store import get_collection
from storage.neo4j_store import ensure_constraints, get_driver
from storage.sqlite_store import connect

TEST_URI = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
TEST_USER = os.environ.get("NEO4J_TEST_USER", "neo4j")
TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "testpassword123")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_local(uri: str) -> bool:
    return urlsplit(uri).hostname in _LOCAL_HOSTS


def _server_available() -> bool:
    try:
        driver = neo4j.GraphDatabase.driver(TEST_URI, auth=(TEST_USER, TEST_PASSWORD))
        try:
            driver.verify_connectivity()
        finally:
            driver.close()
    except Exception:
        return False
    return True


@asynccontextmanager
async def _noop_lifespan(app):
    yield


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def collection(tmp_path):
    return get_collection(tmp_path / "chroma")


@pytest.fixture
def driver():
    if not _is_local(TEST_URI) and not os.environ.get("NEO4J_TEST_ALLOW_NONLOCAL_WIPE"):
        pytest.skip(
            f"NEO4J_TEST_URI={TEST_URI!r} is not localhost, and this suite "
            "wipes the entire database on every run."
        )
    if not _server_available():
        pytest.skip("No Neo4j server reachable at NEO4J_TEST_URI")

    d = get_driver(uri=TEST_URI, user=TEST_USER, password=TEST_PASSWORD)
    ensure_constraints(d)
    yield d
    d.close()


@pytest.fixture(autouse=True)
def configured_llm_provider(monkeypatch):
    """A provider that always constructs successfully.

    Keeps API tests independent of this machine's real config/.env
    (OPENROUTER_API_KEY may or may not be set) — matching
    tests/test_agent/test_health.py's own fixture.
    """
    monkeypatch.setattr("agent.health.get_provider", lambda task: object())


@pytest.fixture
def client(conn, collection, driver):
    app = create_app(lifespan_fn=_noop_lifespan)
    app.state.conn = conn
    app.state.collection = collection
    app.state.driver = driver
    with TestClient(app) as test_client:
        yield test_client
