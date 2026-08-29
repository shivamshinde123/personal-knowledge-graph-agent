"""Tests for the health check.

Real integration tests: real SQLite, a real Chroma collection, and a live
Neo4j instance (skipped automatically if none is reachable, matching the
rest of the agent test suite).
"""

import os
from urllib.parse import urlsplit

import neo4j
import pytest

from agent.health import check_health
from storage.chroma_store import get_collection
from storage.neo4j_store import ensure_constraints, get_driver
from storage.sqlite_store import connect

TEST_URI = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7687")
TEST_USER = os.environ.get("NEO4J_TEST_USER", "neo4j")
TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "testpassword123")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_local(uri: str) -> bool:
    return urlsplit(uri).hostname in _LOCAL_HOSTS


if not _is_local(TEST_URI) and not os.environ.get("NEO4J_TEST_ALLOW_NONLOCAL_WIPE"):
    pytest.skip(
        f"NEO4J_TEST_URI={TEST_URI!r} is not localhost, and this suite wipes "
        "the entire database on every run.",
        allow_module_level=True,
    )


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


pytestmark = pytest.mark.skipif(
    not _server_available(), reason="No Neo4j server reachable at NEO4J_TEST_URI"
)


@pytest.fixture
def driver():
    d = get_driver(uri=TEST_URI, user=TEST_USER, password=TEST_PASSWORD)
    ensure_constraints(d)
    yield d
    d.close()


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def collection(tmp_path):
    return get_collection(tmp_path / "chroma")


@pytest.fixture(autouse=True)
def fully_cloud_with_key(monkeypatch):
    """A configured provider, so the llm_provider check succeeds by default."""
    monkeypatch.setattr("agent.health.get_provider", lambda task: object())


class TestCheckHealth:
    def test_every_service_ok_reports_overall_ok(self, conn, collection, driver):
        result = check_health(conn, collection, driver)

        assert result.status == "ok"
        assert result.services == {
            "sqlite": "ok",
            "chroma": "ok",
            "neo4j": "ok",
            "llm_provider": "ok",
        }

    def test_a_closed_sqlite_connection_reports_error(self, conn, collection, driver):
        conn.close()

        result = check_health(conn, collection, driver)

        assert result.services["sqlite"] == "error"
        assert result.status == "degraded"

    def test_a_closed_neo4j_driver_reports_error(self, conn, collection, driver):
        driver.close()

        result = check_health(conn, collection, driver)

        assert result.services["neo4j"] == "error"
        assert result.status == "degraded"

    def test_provider_construction_failure_reports_error(
        self, conn, collection, driver, monkeypatch
    ):
        from providers.base import ProviderError

        def boom(task):
            raise ProviderError("OPENROUTER_API_KEY is not configured")

        monkeypatch.setattr("agent.health.get_provider", boom)

        result = check_health(conn, collection, driver)

        assert result.services["llm_provider"] == "error"
        assert result.status == "degraded"
