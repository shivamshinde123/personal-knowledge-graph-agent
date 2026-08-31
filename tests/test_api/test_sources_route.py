"""Tests for GET /api/sources/status and /api/sources/connections."""

from datetime import UTC, datetime

import pytest

from agent.connection_check import ConnectionStatus
from storage.sqlite_store import (
    Item,
    complete_ingestion_run,
    insert_item,
    start_ingestion_run,
    update_ingestion_run_progress,
)


@pytest.fixture(autouse=True)
def reset_connection_cache(monkeypatch):
    import agent.connection_check as connection_check_module

    monkeypatch.setattr(connection_check_module, "_cache", None)
    monkeypatch.setattr(connection_check_module, "_cache_time", None)


class TestGetSourcesStatus:
    def test_no_run_yet_reports_every_source_at_zero(self, client):
        response = client.get("/api/sources/status")

        assert response.status_code == 200
        body = response.json()
        assert body["last_run"] is None
        assert len(body["sources"]) == 6
        assert all(s["items_processed"] == 0 for s in body["sources"])

    def test_reflects_a_real_completed_run(self, conn, client):
        run_id = start_ingestion_run(conn)
        insert_item(
            conn,
            Item(
                id="item-1",
                source_type="notion",
                source_ref_id="page-1",
                ingested_at=datetime.now(UTC),
            ),
        )
        complete_ingestion_run(conn, run_id, status="success", items_processed=1)

        response = client.get("/api/sources/status")

        assert response.status_code == 200
        body = response.json()
        assert body["last_run"]["run_id"] == run_id
        assert body["last_run"]["status"] == "success"
        assert body["last_run"]["items_processed"] == 1
        notion = next(s for s in body["sources"] if s["source_type"] == "notion")
        assert notion["items_processed"] == 1
        assert notion["status"] == "ok"

    def test_last_run_items_processed_updates_while_still_running(self, conn, client):
        """Live progress, not just a final count once the whole run finishes.

        Verifies the fix behind the field, not just its presence.
        """
        run_id = start_ingestion_run(conn)
        update_ingestion_run_progress(conn, run_id, 7)

        response = client.get("/api/sources/status")

        body = response.json()
        assert body["last_run"]["status"] == "running"
        assert body["last_run"]["items_processed"] == 7

    def test_last_run_current_item_reflects_live_progress(self, conn, client):
        run_id = start_ingestion_run(conn)
        update_ingestion_run_progress(
            conn, run_id, 7, current_item="github: my-repo: fix the bug"
        )

        response = client.get("/api/sources/status")

        assert response.json()["last_run"]["current_item"] == (
            "github: my-repo: fix the bug"
        )

    def test_total_items_reflects_items_from_before_the_latest_run(self, conn, client):
        insert_item(
            conn,
            Item(
                id="item-1",
                source_type="notion",
                source_ref_id="page-1",
                ingested_at=datetime(2020, 1, 1, tzinfo=UTC),
            ),
        )
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(conn, run_id, status="success", items_processed=0)

        response = client.get("/api/sources/status")

        notion = next(
            s for s in response.json()["sources"] if s["source_type"] == "notion"
        )
        assert notion["items_processed"] == 0
        assert notion["total_items"] == 1


class TestGetConnections:
    def test_reports_every_unconfigured_source_as_not_configured(
        self, monkeypatch, client
    ):
        from types import SimpleNamespace

        monkeypatch.setattr(
            "agent.connection_check.get_settings",
            lambda: SimpleNamespace(
                env=SimpleNamespace(
                    watch_dirs=[],
                    notion_api_key=None,
                    gmail_credentials_path=None,
                    github_token=None,
                    google_calendar_credentials_path=None,
                    browser_history_path=None,
                )
            ),
        )

        response = client.get("/api/sources/connections")

        assert response.status_code == 200
        by_type = {c["source_type"]: c for c in response.json()["connections"]}
        for source in ("gmail", "github", "calendar", "notion", "local_file"):
            assert by_type[source]["status"] == "not_configured"

    def test_reuses_the_cache_on_a_second_call(self, monkeypatch, client):
        calls = []
        monkeypatch.setattr(
            "agent.connection_check.check_all_connections",
            lambda: calls.append(1)
            or [
                ConnectionStatus(
                    source_type="local_file",
                    status="ok",
                    detail=None,
                    checked_at=datetime.now(UTC),
                )
            ],
        )

        client.get("/api/sources/connections")
        client.get("/api/sources/connections")

        assert len(calls) == 1


class TestVerifyConnections:
    def test_forces_a_fresh_check_even_with_a_warm_cache(self, monkeypatch, client):
        calls = []
        monkeypatch.setattr(
            "agent.connection_check.check_all_connections",
            lambda: calls.append(1)
            or [
                ConnectionStatus(
                    source_type="local_file",
                    status="ok",
                    detail=None,
                    checked_at=datetime.now(UTC),
                )
            ],
        )

        client.get("/api/sources/connections")
        response = client.post("/api/sources/connections/verify")

        assert response.status_code == 200
        assert len(calls) == 2
