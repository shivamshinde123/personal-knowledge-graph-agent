"""Tests for extractors/google_oauth.py, the guided Gmail+Calendar OAuth flow.

Real network calls (building the auth URL, exchanging a code) are never
made — ``Flow.authorization_url()``/``Flow.fetch_token()`` are monkeypatched
directly on the ``Flow`` class, the same "fake the SDK boundary, not our
own code" approach ``tests/test_providers`` already uses.
"""

from types import SimpleNamespace

import pytest
from google_auth_oauthlib.flow import Flow

import extractors.google_oauth as google_oauth
from extractors.google_oauth import (
    GoogleOAuthError,
    complete_authorization,
    is_connected,
    load_credentials,
    start_authorization,
)


@pytest.fixture(autouse=True)
def isolated_token_path(tmp_path, monkeypatch):
    """Never let a test touch the real data/google_oauth_token.json."""
    fake_path = tmp_path / "google_oauth_token.json"
    monkeypatch.setattr("extractors.google_oauth.token_path", lambda: fake_path)
    return fake_path


@pytest.fixture(autouse=True)
def clear_pending_flows():
    """Each test starts with no leftover pending-flow state from another."""
    google_oauth._pending_flows.clear()
    yield
    google_oauth._pending_flows.clear()


@pytest.fixture(autouse=True)
def fake_update_google_oauth_config(monkeypatch):
    """Never write to the real config/.env from these tests."""
    calls = []
    monkeypatch.setattr(
        "extractors.google_oauth.update_google_oauth_config",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


class TestIsConnected:
    def test_false_when_no_token_file_exists(self, isolated_token_path):
        assert is_connected() is False

    def test_true_once_a_token_file_exists(self, isolated_token_path):
        isolated_token_path.write_text("{}", encoding="utf-8")

        assert is_connected() is True


class TestStartAuthorization:
    def test_rejects_empty_credentials(self):
        with pytest.raises(GoogleOAuthError, match="required"):
            start_authorization(
                client_id="", client_secret="secret", redirect_uri="http://x"
            )

    def test_saves_the_credentials_before_building_the_url(
        self, monkeypatch, fake_update_google_oauth_config
    ):
        monkeypatch.setattr(
            Flow, "authorization_url", lambda self, **kw: ("http://auth-url", "state-1")
        )

        start_authorization(
            client_id="cid", client_secret="csecret", redirect_uri="http://cb"
        )

        assert fake_update_google_oauth_config == [
            {"client_id": "cid", "client_secret": "csecret"}
        ]

    def test_returns_the_authorization_url_and_tracks_the_pending_flow(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            Flow, "authorization_url", lambda self, **kw: ("http://auth-url", "state-1")
        )

        url = start_authorization(
            client_id="cid", client_secret="csecret", redirect_uri="http://cb"
        )

        assert url == "http://auth-url"
        assert "state-1" in google_oauth._pending_flows

    def test_wraps_a_failure_building_the_url(self, monkeypatch):
        def raise_error(self, **kw):
            raise ValueError("boom")

        monkeypatch.setattr(Flow, "authorization_url", raise_error)

        with pytest.raises(GoogleOAuthError, match="Could not start"):
            start_authorization(
                client_id="cid", client_secret="csecret", redirect_uri="http://cb"
            )


class TestCompleteAuthorization:
    def test_unknown_state_raises(self):
        with pytest.raises(GoogleOAuthError, match="expired"):
            complete_authorization(
                state="never-started", authorization_response="http://cb?code=x"
            )

    def test_exchanges_the_code_and_caches_the_token(
        self, monkeypatch, isolated_token_path
    ):
        fake_creds = SimpleNamespace(to_json=lambda: '{"token": "abc"}')

        monkeypatch.setattr(Flow, "fetch_token", lambda self, **kw: None)
        monkeypatch.setattr(Flow, "credentials", property(lambda self: fake_creds))
        flow = Flow.from_client_config(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "auth_uri": "http://auth",
                    "token_uri": "http://token",
                }
            },
            scopes=["scope"],
            redirect_uri="http://cb",
        )
        google_oauth._pending_flows["state-1"] = flow

        complete_authorization(
            state="state-1", authorization_response="http://cb?code=x&state=state-1"
        )

        assert isolated_token_path.read_text(encoding="utf-8") == '{"token": "abc"}'
        assert "state-1" not in google_oauth._pending_flows  # consumed, not reusable

    def test_wraps_a_failed_token_exchange(self, monkeypatch):
        def raise_error(self, **kw):
            raise ValueError("denied")

        monkeypatch.setattr(Flow, "fetch_token", raise_error)
        flow = Flow.from_client_config(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "auth_uri": "http://auth",
                    "token_uri": "http://token",
                }
            },
            scopes=["scope"],
            redirect_uri="http://cb",
        )
        google_oauth._pending_flows["state-1"] = flow

        with pytest.raises(GoogleOAuthError, match="authorization failed"):
            complete_authorization(
                state="state-1",
                authorization_response="http://cb?error=access_denied",
            )


class TestLoadCredentials:
    def test_none_when_no_token_cached(self, isolated_token_path):
        assert load_credentials() is None

    def test_returns_valid_cached_credentials_without_refreshing(
        self, monkeypatch, isolated_token_path
    ):
        isolated_token_path.write_text(
            '{"token": "abc", "refresh_token": "r", "client_id": "cid", '
            '"client_secret": "cs", "token_uri": "http://token", '
            '"scopes": ["https://www.googleapis.com/auth/gmail.readonly", '
            '"https://www.googleapis.com/auth/calendar.readonly"]}',
            encoding="utf-8",
        )
        refreshed = []
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.refresh",
            lambda self, request: refreshed.append(True),
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.expired",
            property(lambda self: False),
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.valid",
            property(lambda self: True),
        )

        creds = load_credentials()

        assert creds is not None
        assert refreshed == []

    def test_refreshes_an_expired_token_and_rewrites_the_file(
        self, monkeypatch, isolated_token_path
    ):
        isolated_token_path.write_text(
            '{"token": "abc", "refresh_token": "r", "client_id": "cid", '
            '"client_secret": "cs", "token_uri": "http://token", '
            '"scopes": ["https://www.googleapis.com/auth/gmail.readonly", '
            '"https://www.googleapis.com/auth/calendar.readonly"]}',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.expired",
            property(lambda self: True),
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.valid",
            property(lambda self: True),
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.refresh",
            lambda self, request: None,
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.to_json",
            lambda self: '{"token": "refreshed"}',
        )

        creds = load_credentials()

        assert creds is not None
        assert (
            isolated_token_path.read_text(encoding="utf-8") == '{"token": "refreshed"}'
        )

    def test_invalid_credentials_raises(self, monkeypatch, isolated_token_path):
        isolated_token_path.write_text(
            '{"token": "abc", "refresh_token": "r", "client_id": "cid", '
            '"client_secret": "cs", "token_uri": "http://token", '
            '"scopes": ["https://www.googleapis.com/auth/gmail.readonly", '
            '"https://www.googleapis.com/auth/calendar.readonly"]}',
            encoding="utf-8",
        )
        # Not expired (so no refresh is attempted -- no real network call),
        # but revoked/otherwise invalid regardless.
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.expired",
            property(lambda self: False),
        )
        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.valid",
            property(lambda self: False),
        )

        with pytest.raises(GoogleOAuthError, match="invalid or was revoked"):
            load_credentials()
