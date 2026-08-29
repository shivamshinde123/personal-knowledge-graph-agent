"""Tests for POST /api/query."""

import pytest

from agent.graph import QueryResult
from agent.synthesizer import Source
from providers.base import ProviderError
from storage.chroma_store import VectorStoreError
from storage.neo4j_store import GraphStoreError
from storage.sqlite_store import StorageError


def fake_result(**overrides):
    defaults = dict(
        session_id="sess-123",
        answer="The storage layer uses SQLite, Chroma, and Neo4j.",
        sources=[
            Source(
                item_id="item-1",
                source_type="notion",
                title="Storage design",
                url="https://notion.so/page-1",
            )
        ],
        retrieval_methods_used=["vector_search", "keyword_search"],
    )
    defaults.update(overrides)
    return QueryResult(**defaults)


class TestPostQuery:
    def test_returns_the_synthesized_answer_and_sources(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.query.run",
            lambda conn, collection, driver, q, sid: fake_result(),
        )

        response = client.post(
            "/api/query", json={"question": "What storage does it use?"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == "sess-123"
        assert body["answer"] == "The storage layer uses SQLite, Chroma, and Neo4j."
        assert body["sources"] == [
            {
                "item_id": "item-1",
                "source_type": "notion",
                "title": "Storage design",
                "url": "https://notion.so/page-1",
            }
        ]
        assert body["retrieval_methods_used"] == ["vector_search", "keyword_search"]
        assert isinstance(body["latency_ms"], int)
        assert body["latency_ms"] >= 0

    def test_passes_session_id_through_to_run(self, client, monkeypatch):
        captured = {}

        def fake_run(conn, collection, driver, question, session_id):
            captured["question"] = question
            captured["session_id"] = session_id
            return fake_result(session_id=session_id or "generated")

        monkeypatch.setattr("api.routes.query.run", fake_run)

        client.post(
            "/api/query", json={"question": "hello", "session_id": "sess-existing"}
        )

        assert captured["question"] == "hello"
        assert captured["session_id"] == "sess-existing"

    def test_missing_question_returns_422(self, client):
        response = client.post("/api/query", json={})

        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_error"
        assert "detail" in body


class TestErrorMapping:
    def test_provider_error_returns_502(self, client, monkeypatch):
        def boom(conn, collection, driver, q, sid):
            raise ProviderError("model call failed after retries")

        monkeypatch.setattr("api.routes.query.run", boom)

        response = client.post("/api/query", json={"question": "hello"})

        assert response.status_code == 502
        body = response.json()
        assert body["error"] == "provider_error"
        assert "model call failed" in body["detail"]

    @pytest.mark.parametrize(
        "exc_cls",
        [VectorStoreError, GraphStoreError, StorageError],
    )
    def test_storage_errors_return_500(self, client, monkeypatch, exc_cls):
        def boom(conn, collection, driver, q, sid):
            raise exc_cls("backing store unreachable")

        monkeypatch.setattr("api.routes.query.run", boom)

        response = client.post("/api/query", json={"question": "hello"})

        assert response.status_code == 500
        body = response.json()
        assert body["error"] == "storage_error"

    def test_unexpected_error_returns_500_internal_error(self, client, monkeypatch):
        def boom(conn, collection, driver, q, sid):
            raise RuntimeError("something truly unexpected")

        monkeypatch.setattr("api.routes.query.run", boom)

        response = client.post("/api/query", json={"question": "hello"})

        assert response.status_code == 500
        body = response.json()
        assert body["error"] == "internal_error"
