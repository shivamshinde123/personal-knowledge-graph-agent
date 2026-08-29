"""Tests for GET /api/sessions and GET /api/sessions/{session_id}."""

from storage.sqlite_store import record_conversation_turn


class TestGetSessions:
    def test_empty_before_any_conversation(self, client):
        response = client.get("/api/sessions")

        assert response.status_code == 200
        assert response.json() == {"sessions": []}

    def test_lists_sessions_most_recently_active_first(self, conn, client):
        record_conversation_turn(conn, "sess-old", "Old question", "Answer.", None)
        record_conversation_turn(conn, "sess-new", "New question", "Answer.", None)

        response = client.get("/api/sessions")

        assert response.status_code == 200
        body = response.json()
        assert [s["session_id"] for s in body["sessions"]] == ["sess-new", "sess-old"]
        assert body["sessions"][0]["title"] == "New question"
        assert "updated_at" in body["sessions"][0]


class TestGetSessionHistory:
    def test_returns_404_for_an_unknown_session(self, client):
        response = client.get("/api/sessions/does-not-exist")

        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "not_found"
        assert "detail" in body

    def test_returns_full_message_history(self, conn, client):
        record_conversation_turn(
            conn,
            "sess-1",
            "What did I work on?",
            "You worked on X.",
            [
                {
                    "item_id": "item-1",
                    "source_type": "notion",
                    "title": "Notes",
                    "url": "https://notion.so/x",
                }
            ],
        )

        response = client.get("/api/sessions/sess-1")

        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == "sess-1"
        assert len(body["messages"]) == 2
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][0]["text"] == "What did I work on?"
        assert body["messages"][0]["sources"] is None
        assert body["messages"][1]["role"] == "agent"
        assert body["messages"][1]["text"] == "You worked on X."
        assert body["messages"][1]["sources"] == [
            {
                "item_id": "item-1",
                "source_type": "notion",
                "title": "Notes",
                "url": "https://notion.so/x",
            }
        ]

    def test_messages_across_turns_stay_in_order(self, conn, client):
        record_conversation_turn(conn, "sess-1", "Q1", "A1", None)
        record_conversation_turn(conn, "sess-1", "Q2", "A2", None)

        response = client.get("/api/sessions/sess-1")

        texts = [m["text"] for m in response.json()["messages"]]
        assert texts == ["Q1", "A1", "Q2", "A2"]
