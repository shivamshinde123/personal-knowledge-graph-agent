"""Tests for the sources-status report."""

from datetime import UTC, datetime

import pytest

from agent.sources_status import get_sources_status
from storage.sqlite_store import (
    Item,
    complete_ingestion_run,
    connect,
    insert_item,
    start_ingestion_run,
)


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def make_item(**overrides) -> Item:
    defaults = dict(
        id="item-1",
        source_type="notion",
        source_ref_id="page-1",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Item(**defaults)


class TestNoRunYet:
    def test_returns_every_source_at_zero_with_ok_status(self, conn):
        result = get_sources_status(conn)

        assert result.last_run is None
        assert {s.source_type for s in result.sources} == {
            "local_file",
            "notion",
            "gmail",
            "github",
            "calendar",
            "browser_history",
        }
        assert all(s.items_processed == 0 for s in result.sources)
        assert all(s.total_items == 0 for s in result.sources)
        assert all(s.status == "ok" for s in result.sources)


class TestAfterARun:
    def test_counts_items_by_source_type_ingested_during_the_run(self, conn):
        run_id = start_ingestion_run(conn)
        now = datetime.now(UTC)
        insert_item(
            conn,
            make_item(
                id="a", source_type="notion", source_ref_id="p1", ingested_at=now
            ),
        )
        insert_item(
            conn,
            make_item(
                id="b", source_type="notion", source_ref_id="p2", ingested_at=now
            ),
        )
        insert_item(
            conn,
            make_item(
                id="c", source_type="local_file", source_ref_id="p3", ingested_at=now
            ),
        )
        complete_ingestion_run(conn, run_id, status="success", items_processed=3)

        result = get_sources_status(conn)

        by_type = {s.source_type: s.items_processed for s in result.sources}
        assert by_type["notion"] == 2
        assert by_type["local_file"] == 1
        assert by_type["gmail"] == 0

    def test_a_source_named_in_the_error_log_reports_error(self, conn):
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(
            conn,
            run_id,
            status="partial_failure",
            items_processed=1,
            error_log="gmail: token expired",
        )

        result = get_sources_status(conn)

        by_type = {s.source_type: s.status for s in result.sources}
        assert by_type["gmail"] == "error"
        assert by_type["notion"] == "ok"

    def test_last_run_reflects_the_most_recent_run(self, conn):
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(conn, run_id, status="success", items_processed=1)

        result = get_sources_status(conn)

        assert result.last_run.id == run_id
        assert result.last_run.status == "success"

    def test_items_ingested_before_this_run_are_not_counted(self, conn):
        insert_item(
            conn,
            make_item(
                id="old",
                source_type="notion",
                ingested_at=datetime(2020, 1, 1, tzinfo=UTC),
            ),
        )
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(conn, run_id, status="success", items_processed=0)

        result = get_sources_status(conn)

        by_type = {s.source_type: s.items_processed for s in result.sources}
        assert by_type["notion"] == 0

    def test_total_items_reflects_everything_ever_ingested_not_just_this_run(
        self, conn
    ):
        # Real scenario this fixes: a source has real items from a past
        # run, but the *latest* run found nothing new — items_processed is
        # legitimately 0, but total_items should still show the real count
        # rather than reading as "nothing has ever been ingested."
        insert_item(
            conn,
            make_item(
                id="old-1",
                source_type="notion",
                source_ref_id="p1",
                ingested_at=datetime(2020, 1, 1, tzinfo=UTC),
            ),
        )
        insert_item(
            conn,
            make_item(
                id="old-2",
                source_type="notion",
                source_ref_id="p2",
                ingested_at=datetime(2020, 1, 2, tzinfo=UTC),
            ),
        )
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(conn, run_id, status="success", items_processed=0)

        result = get_sources_status(conn)

        notion = next(s for s in result.sources if s.source_type == "notion")
        assert notion.items_processed == 0
        assert notion.total_items == 2
