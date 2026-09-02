"""Tests for the guided setup wizard's Google OAuth endpoints (issue #92)."""


class TestStartGoogleOAuth:
    def test_returns_the_authorization_url(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.setup.start_google_authorization",
            lambda **kwargs: "https://accounts.google.com/o/oauth2/auth?foo=bar",
        )

        response = client.post(
            "/api/setup/google/oauth/start",
            json={"client_id": "cid", "client_secret": "secret"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "authorization_url": "https://accounts.google.com/o/oauth2/auth?foo=bar"
        }

    def test_builds_the_redirect_uri_from_the_inbound_request(
        self, client, monkeypatch
    ):
        captured = {}

        def fake_start(*, client_id, client_secret, redirect_uri):
            captured["redirect_uri"] = redirect_uri
            return "http://auth"

        monkeypatch.setattr("api.routes.setup.start_google_authorization", fake_start)

        client.post(
            "/api/setup/google/oauth/start",
            json={"client_id": "cid", "client_secret": "secret"},
        )

        assert captured["redirect_uri"].endswith("/api/setup/google/oauth/callback")

    def test_empty_credentials_return_a_400(self, client, monkeypatch):
        from agent.google_oauth import GoogleOAuthError

        def raise_error(**kwargs):
            raise GoogleOAuthError("Client ID and Client Secret are both required.")

        monkeypatch.setattr("api.routes.setup.start_google_authorization", raise_error)

        response = client.post(
            "/api/setup/google/oauth/start",
            json={"client_id": "", "client_secret": ""},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "google_oauth_error"


class TestGoogleOAuthCallback:
    def test_a_successful_exchange_renders_a_success_page(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.setup.complete_authorization", lambda **kwargs: None
        )

        response = client.get("/api/setup/google/oauth/callback?code=abc&state=xyz")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Google connected" in response.text

    def test_a_denied_consent_renders_a_failure_page(self, client):
        response = client.get("/api/setup/google/oauth/callback?error=access_denied")

        assert response.status_code == 200
        assert "Connection failed" in response.text
        assert "access_denied" in response.text

    def test_a_missing_state_renders_a_failure_page(self, client):
        response = client.get("/api/setup/google/oauth/callback?code=abc")

        assert response.status_code == 200
        assert "Connection failed" in response.text

    def test_a_failed_exchange_renders_a_failure_page_not_a_500(
        self, client, monkeypatch
    ):
        from agent.google_oauth import GoogleOAuthError

        def raise_error(**kwargs):
            raise GoogleOAuthError("This authorization link has expired.")

        monkeypatch.setattr("api.routes.setup.complete_authorization", raise_error)

        response = client.get("/api/setup/google/oauth/callback?code=abc&state=xyz")

        assert response.status_code == 200
        assert "Connection failed" in response.text
        assert "expired" in response.text


class TestGoogleOAuthStatus:
    def test_reports_not_connected(self, client, monkeypatch):
        monkeypatch.setattr("api.routes.setup.is_connected", lambda: False)

        response = client.get("/api/setup/google/oauth/status")

        assert response.status_code == 200
        assert response.json() == {"connected": False}

    def test_reports_connected(self, client, monkeypatch):
        monkeypatch.setattr("api.routes.setup.is_connected", lambda: True)

        response = client.get("/api/setup/google/oauth/status")

        assert response.json() == {"connected": True}
