"""Tests for GET /api/health."""


class TestGetHealth:
    def test_returns_ok_when_every_service_is_reachable(self, client):
        response = client.get("/api/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["services"] == {
            "sqlite": "ok",
            "chroma": "ok",
            "neo4j": "ok",
            "llm_provider": "ok",
        }

    def test_reports_degraded_when_a_service_is_down(self, conn, client):
        conn.close()

        response = client.get("/api/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["services"]["sqlite"] == "error"
