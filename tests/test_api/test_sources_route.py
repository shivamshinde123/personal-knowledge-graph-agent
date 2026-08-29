"""Tests for GET /api/sources/status."""

from datetime import UTC, datetime

from storage.sqlite_store import (
    Item,
    complete_ingestion_run,
    insert_item,
    start_ingestion_run,
)


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
        notion = next(s for s in body["sources"] if s["source_type"] == "notion")
        assert notion["items_processed"] == 1
        assert notion["status"] == "ok"
