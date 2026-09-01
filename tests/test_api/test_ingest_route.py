"""Tests for POST /api/ingest/trigger and POST /api/ingest/cancel."""

from storage.sqlite_store import (
    get_ingestion_run,
    is_cancellation_requested,
    start_ingestion_run,
)


class TestTriggerIngestion:
    def test_returns_202_with_a_started_status_and_run_id(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.ingest.trigger_ingestion", lambda: "run_manual_20260829_1200"
        )

        response = client.post("/api/ingest/trigger")

        assert response.status_code == 202
        assert response.json() == {
            "status": "started",
            "run_id": "run_manual_20260829_1200",
        }

    def test_calls_trigger_ingestion_exactly_once(self, client, monkeypatch):
        calls = []

        def fake_trigger():
            calls.append(1)
            return "run_manual_x"

        monkeypatch.setattr("api.routes.ingest.trigger_ingestion", fake_trigger)

        client.post("/api/ingest/trigger")

        assert len(calls) == 1


class TestCancelIngestion:
    # start_ingestion_run() records this test process's own pid (there's no
    # real subprocess spawned here), so os.kill must always be faked below
    # — the real one would send SIGTERM to the test runner itself.

    def test_sets_the_cancellation_flag_on_the_given_run(
        self, client, conn, monkeypatch
    ):
        monkeypatch.setattr("agent.ingest_trigger.os.kill", lambda pid, sig: None)
        run_id = start_ingestion_run(conn)

        response = client.post("/api/ingest/cancel", json={"run_id": run_id})

        assert response.status_code == 200
        assert response.json() == {"status": "cancel_requested"}
        assert is_cancellation_requested(conn, run_id) is True

    def test_force_kills_and_finalizes_the_run(self, client, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "agent.ingest_trigger.os.kill",
            lambda pid, sig: calls.append((pid, sig)),
        )
        run_id = start_ingestion_run(conn)

        client.post("/api/ingest/cancel", json={"run_id": run_id})

        assert len(calls) == 1
        run = get_ingestion_run(conn, run_id)
        assert run.status == "cancelled"

    def test_an_unknown_run_id_still_acknowledges(self, client):
        response = client.post("/api/ingest/cancel", json={"run_id": "no-such-run"})

        assert response.status_code == 200
        assert response.json() == {"status": "cancel_requested"}
