"""Tests for the SQLite storage layer."""

from datetime import UTC, datetime

import pytest

from storage.sqlite_store import (
    Chunk,
    Item,
    StorageError,
    complete_ingestion_run,
    connect,
    delete_item,
    get_chunks_for_item,
    get_item,
    get_last_ingestion_run,
    get_last_run_timestamp,
    get_messages_for_session,
    get_session,
    insert_chunk,
    insert_item,
    keyword_search,
    list_sessions,
    record_conversation_turn,
    replace_chunks,
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
        source_ref_id="page-abc",
        title="Design notes",
        url_or_path="https://notion.so/page-abc",
        author_or_sender="shivam",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        last_edited_at=datetime(2026, 8, 2, tzinfo=UTC),
        ingested_at=datetime(2026, 8, 3, tzinfo=UTC),
        project_name="pkg-agent",
        topic="architecture",
    )
    defaults.update(overrides)
    return Item(**defaults)


def make_chunk(**overrides) -> Chunk:
    defaults = dict(
        id="chunk-1",
        item_id="item-1",
        chunk_index=0,
        text="Extractors never import from storage directly.",
        embedding_id="emb-1",
        token_count=9,
    )
    defaults.update(overrides)
    return Chunk(**defaults)


class TestConnect:
    def test_creates_schema_idempotently(self):
        conn = connect(":memory:")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            ).fetchall()
        }
        assert {"items", "chunks", "chunks_fts", "ingestion_runs"} <= tables
        conn.close()

    def test_foreign_keys_are_enforced(self):
        conn = connect(":memory:")
        with pytest.raises(StorageError):
            insert_chunk(conn, make_chunk(item_id="does-not-exist"))
        conn.close()


class TestInsertAndGetItem:
    def test_round_trips_an_item(self, conn):
        effective_id = insert_item(conn, make_item())

        assert effective_id == "item-1"
        stored = get_item(conn, "item-1")
        assert stored.title == "Design notes"
        assert stored.source_type == "notion"
        assert stored.created_at == datetime(2026, 8, 1, tzinfo=UTC)

    def test_missing_item_returns_none(self, conn):
        assert get_item(conn, "nope") is None

    def test_reingesting_same_source_ref_updates_in_place(self, conn):
        insert_item(conn, make_item())
        effective_id = insert_item(
            conn, make_item(id="item-1-regenerated", title="Design notes (edited)")
        )

        assert effective_id == "item-1"
        stored = get_item(conn, "item-1")
        assert stored.title == "Design notes (edited)"
        assert get_item(conn, "item-1-regenerated") is None

    def test_distinct_source_refs_do_not_collide(self, conn):
        insert_item(conn, make_item())
        insert_item(
            conn, make_item(id="item-2", source_ref_id="page-def", title="Other page")
        )

        assert get_item(conn, "item-1").title == "Design notes"
        assert get_item(conn, "item-2").title == "Other page"


class TestChunks:
    def test_insert_and_fetch_chunks_in_order(self, conn):
        insert_item(conn, make_item())
        insert_chunk(conn, make_chunk(id="c0", chunk_index=0, embedding_id="e0"))
        insert_chunk(conn, make_chunk(id="c1", chunk_index=1, embedding_id="e1"))

        chunks = get_chunks_for_item(conn, "item-1")

        assert [c.id for c in chunks] == ["c0", "c1"]

    def test_replace_chunks_removes_stale_chunks(self, conn):
        insert_item(conn, make_item())
        insert_chunk(
            conn, make_chunk(id="stale", chunk_index=0, embedding_id="e-stale")
        )

        replace_chunks(
            conn,
            "item-1",
            [make_chunk(id="fresh", chunk_index=0, embedding_id="e-fresh")],
        )

        chunks = get_chunks_for_item(conn, "item-1")
        assert [c.id for c in chunks] == ["fresh"]

    def test_replace_chunks_ignores_a_mismatched_chunk_item_id(self, conn):
        insert_item(conn, make_item())

        replace_chunks(
            conn,
            "item-1",
            [make_chunk(id="c0", item_id="some-other-item", embedding_id="e0")],
        )

        chunks = get_chunks_for_item(conn, "item-1")
        assert [c.id for c in chunks] == ["c0"]
        assert chunks[0].item_id == "item-1"

    def test_deleting_item_cascades_to_chunks(self, conn):
        insert_item(conn, make_item())
        insert_chunk(conn, make_chunk())

        delete_item(conn, "item-1")

        assert get_chunks_for_item(conn, "item-1") == []


class TestKeywordSearch:
    def test_finds_matching_chunk(self, conn):
        insert_item(conn, make_item())
        insert_chunk(
            conn,
            make_chunk(
                id="c0",
                embedding_id="e0",
                text="The router decides between vector and keyword search.",
            ),
        )
        insert_chunk(
            conn,
            make_chunk(
                id="c1",
                embedding_id="e1",
                text="Browser history is the lightest-weight source.",
            ),
        )

        results = keyword_search(conn, "router")

        assert [r.chunk.id for r in results] == ["c0"]

    def test_no_match_returns_empty_list(self, conn):
        insert_item(conn, make_item())
        insert_chunk(conn, make_chunk())

        assert keyword_search(conn, "nonexistent_term_xyz") == []

    def test_fts_index_updates_after_replace_chunks(self, conn):
        insert_item(conn, make_item())
        insert_chunk(
            conn, make_chunk(id="c0", embedding_id="e0", text="original wording")
        )

        replace_chunks(
            conn,
            "item-1",
            [make_chunk(id="c1", embedding_id="e1", text="rewritten wording")],
        )

        assert keyword_search(conn, "original") == []
        assert [r.chunk.id for r in keyword_search(conn, "rewritten")] == ["c1"]


class TestIngestionRuns:
    def test_last_run_timestamp_is_none_before_any_success(self, conn):
        assert get_last_run_timestamp(conn) is None

    def test_successful_run_advances_watermark(self, conn):
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(conn, run_id, status="success", items_processed=12)

        assert get_last_run_timestamp(conn) is not None

    def test_partial_failure_does_not_advance_watermark(self, conn):
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(
            conn,
            run_id,
            status="partial_failure",
            items_processed=5,
            error_log="gmail: token expired",
        )

        assert get_last_run_timestamp(conn) is None

    def test_only_the_latest_success_counts(self, conn):
        first = start_ingestion_run(conn)
        complete_ingestion_run(conn, first, status="success", items_processed=1)
        first_ts = get_last_run_timestamp(conn)

        second = start_ingestion_run(conn)
        complete_ingestion_run(conn, second, status="success", items_processed=2)

        assert get_last_run_timestamp(conn) >= first_ts


class TestGetLastIngestionRun:
    def test_none_before_any_run(self, conn):
        assert get_last_ingestion_run(conn) is None

    def test_returns_the_most_recent_run_even_if_it_failed(self, conn):
        first = start_ingestion_run(conn)
        complete_ingestion_run(conn, first, status="success", items_processed=1)
        second = start_ingestion_run(conn)
        complete_ingestion_run(
            conn,
            second,
            status="failed",
            items_processed=0,
            error_log="notion: unreachable",
        )

        run = get_last_ingestion_run(conn)

        assert run.id == second
        assert run.status == "failed"
        assert run.error_log == "notion: unreachable"

    def test_fields_round_trip(self, conn):
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(conn, run_id, status="success", items_processed=7)

        run = get_last_ingestion_run(conn)

        assert run.id == run_id
        assert run.items_processed == 7
        assert run.run_started_at is not None
        assert run.run_completed_at is not None
        assert run.error_log is None


class TestRecordConversationTurn:
    def test_creates_a_new_session_with_a_title_from_the_question(self, conn):
        record_conversation_turn(
            conn, "sess-1", "What did I work on related to RAG?", "Answer.", None
        )

        session = get_session(conn, "sess-1")

        assert session.title == "What did I work on related to RAG?"
        assert session.created_at is not None
        assert session.updated_at is not None

    def test_long_question_title_is_truncated(self, conn):
        long_question = "A" * 100

        record_conversation_turn(conn, "sess-1", long_question, "Answer.", None)

        session = get_session(conn, "sess-1")

        assert len(session.title) <= 63  # 60 chars + "..."
        assert session.title.endswith("...")

    def test_a_second_turn_does_not_change_the_title(self, conn):
        record_conversation_turn(conn, "sess-1", "First question", "Answer 1.", None)
        record_conversation_turn(
            conn, "sess-1", "Follow-up question", "Answer 2.", None
        )

        session = get_session(conn, "sess-1")

        assert session.title == "First question"

    def test_a_second_turn_advances_updated_at(self, conn):
        record_conversation_turn(conn, "sess-1", "First question", "Answer 1.", None)
        first_updated_at = get_session(conn, "sess-1").updated_at

        record_conversation_turn(
            conn, "sess-1", "Follow-up question", "Answer 2.", None
        )

        assert get_session(conn, "sess-1").updated_at >= first_updated_at

    def test_records_both_the_question_and_the_answer_as_messages(self, conn):
        record_conversation_turn(
            conn, "sess-1", "What did I work on?", "You worked on X.", None
        )

        messages = get_messages_for_session(conn, "sess-1")

        assert [m.role for m in messages] == ["user", "agent"]
        assert messages[0].text == "What did I work on?"
        assert messages[1].text == "You worked on X."

    def test_messages_across_turns_stay_in_order(self, conn):
        record_conversation_turn(conn, "sess-1", "Q1", "A1", None)
        record_conversation_turn(conn, "sess-1", "Q2", "A2", None)

        messages = get_messages_for_session(conn, "sess-1")

        assert [m.text for m in messages] == ["Q1", "A1", "Q2", "A2"]

    def test_agent_message_stores_sources(self, conn):
        sources = [{"item_id": "a", "source_type": "notion", "title": "T", "url": "u"}]

        record_conversation_turn(conn, "sess-1", "Q", "A", sources)

        messages = get_messages_for_session(conn, "sess-1")
        agent_message = next(m for m in messages if m.role == "agent")
        assert agent_message.sources == sources

    def test_user_message_never_has_sources(self, conn):
        record_conversation_turn(
            conn,
            "sess-1",
            "Q",
            "A",
            [{"item_id": "a", "source_type": "notion", "title": "T", "url": "u"}],
        )

        messages = get_messages_for_session(conn, "sess-1")
        user_message = next(m for m in messages if m.role == "user")
        assert user_message.sources is None


class TestListSessions:
    def test_empty_before_any_conversation(self, conn):
        assert list_sessions(conn) == []

    def test_lists_most_recently_active_first(self, conn):
        record_conversation_turn(conn, "sess-old", "Old question", "A", None)
        record_conversation_turn(conn, "sess-new", "New question", "A", None)

        sessions = list_sessions(conn)

        assert [s.id for s in sessions] == ["sess-new", "sess-old"]

    def test_a_touched_session_moves_to_the_front(self, conn):
        record_conversation_turn(conn, "sess-a", "Q", "A", None)
        record_conversation_turn(conn, "sess-b", "Q", "A", None)
        record_conversation_turn(conn, "sess-a", "Follow-up", "A", None)

        sessions = list_sessions(conn)

        assert sessions[0].id == "sess-a"


class TestGetSession:
    def test_none_for_unknown_session(self, conn):
        assert get_session(conn, "does-not-exist") is None


class TestGetMessagesForSession:
    def test_empty_for_unknown_session(self, conn):
        assert get_messages_for_session(conn, "does-not-exist") == []

    def test_only_returns_messages_for_the_given_session(self, conn):
        record_conversation_turn(conn, "sess-a", "Q-a", "A-a", None)
        record_conversation_turn(conn, "sess-b", "Q-b", "A-b", None)

        messages = get_messages_for_session(conn, "sess-a")

        assert [m.text for m in messages] == ["Q-a", "A-a"]
