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


class TestValidateCredential:
    def test_openrouter_ok(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.setup.openrouter_key_works",
            lambda value: (True, "OpenRouter API key verified."),
        )

        response = client.post(
            "/api/setup/validate", json={"source": "openrouter", "value": "sk-or-x"}
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "detail": "OpenRouter API key verified.",
        }

    def test_notion_error(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.setup.notion_key_works",
            lambda value: (False, "API token is invalid."),
        )

        response = client.post(
            "/api/setup/validate", json={"source": "notion", "value": "bad"}
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "error",
            "detail": "API token is invalid.",
        }

    def test_github_ok(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.setup.github_token_works",
            lambda value: (True, "GitHub token verified."),
        )

        response = client.post(
            "/api/setup/validate", json={"source": "github", "value": "ghp-x"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_never_saves_the_value(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.setup.openrouter_key_works", lambda value: (True, "ok")
        )
        called = False

        def fail_if_called(**kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(
            "api.routes.setup.update_credentials_config", fail_if_called
        )

        client.post("/api/setup/validate", json={"source": "openrouter", "value": "x"})

        assert called is False

    def test_an_invalid_source_returns_422(self, client):
        response = client.post(
            "/api/setup/validate", json={"source": "not_a_real_source", "value": "x"}
        )

        assert response.status_code == 422


class TestSaveCredentials:
    def test_saves_the_given_fields(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "api.routes.setup.update_credentials_config",
            lambda **kwargs: captured.update(kwargs),
        )

        response = client.post(
            "/api/setup/credentials",
            json={"notion_api_key": "ntn-123", "github_token": "ghp-456"},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "updated"}
        assert captured == {
            "notion_api_key": "ntn-123",
            "github_token": "ghp-456",
            "openrouter_api_key": None,
        }

    def test_config_error_is_mapped_to_500(self, client, monkeypatch):
        from config.settings import ConfigError

        def boom(**kwargs):
            raise ConfigError("could not write .env")

        monkeypatch.setattr("api.routes.setup.update_credentials_config", boom)

        response = client.post("/api/setup/credentials", json={"notion_api_key": "x"})

        assert response.status_code == 500
        assert response.json()["error"] == "config_error"
