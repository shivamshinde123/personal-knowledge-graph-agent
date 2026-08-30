"""Tests for agent/connection_check.py.

The Notion check is faked (a real check_all_connections() run would make
a real, billed-nothing-but-still-real API call) — everything else here
exercises real filesystem checks against real tmp_path directories/files.
"""

from types import SimpleNamespace

import pytest

import agent.connection_check as connection_check_module
from agent.connection_check import (
    ConnectionStatus,
    check_all_connections,
    get_connection_status,
)


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(connection_check_module, "_cache", None)
    monkeypatch.setattr(connection_check_module, "_cache_time", None)


def fake_env(
    *,
    watch_dirs=(),
    notion_api_key=None,
    gmail_credentials_path=None,
    github_token=None,
    browser_history_path=None,
):
    return SimpleNamespace(
        watch_dirs=list(watch_dirs),
        notion_api_key=notion_api_key,
        gmail_credentials_path=gmail_credentials_path,
        github_token=github_token,
        browser_history_path=browser_history_path,
    )


def patch_settings(monkeypatch, env):
    monkeypatch.setattr(
        "agent.connection_check.get_settings", lambda: SimpleNamespace(env=env)
    )


class TestCheckAllConnections:
    def test_returns_all_six_sources_in_a_fixed_order(self, monkeypatch):
        patch_settings(monkeypatch, fake_env())

        results = check_all_connections()

        assert [r.source_type for r in results] == [
            "local_file",
            "notion",
            "gmail",
            "github",
            "calendar",
            "browser_history",
        ]

    def test_calendar_is_always_not_configured(self, monkeypatch):
        patch_settings(monkeypatch, fake_env())

        results = check_all_connections()

        by_type = {r.source_type: r for r in results}
        calendar = by_type["calendar"]
        assert calendar.status == "not_configured"
        assert "not yet built" in calendar.detail


class TestCheckLocalFiles:
    def test_no_watch_dirs_is_not_configured(self, monkeypatch):
        patch_settings(monkeypatch, fake_env(watch_dirs=[]))

        results = check_all_connections()

        local = next(r for r in results if r.source_type == "local_file")
        assert local.status == "not_configured"

    def test_an_existing_directory_is_ok(self, monkeypatch, tmp_path):
        patch_settings(monkeypatch, fake_env(watch_dirs=[tmp_path]))

        results = check_all_connections()

        local = next(r for r in results if r.source_type == "local_file")
        assert local.status == "ok"

    def test_a_missing_directory_is_an_error(self, monkeypatch, tmp_path):
        missing = tmp_path / "does-not-exist"
        patch_settings(monkeypatch, fake_env(watch_dirs=[missing]))

        results = check_all_connections()

        local = next(r for r in results if r.source_type == "local_file")
        assert local.status == "error"
        assert str(missing) in local.detail


class TestCheckBrowserHistory:
    def test_no_path_is_not_configured(self, monkeypatch):
        patch_settings(monkeypatch, fake_env(browser_history_path=None))

        results = check_all_connections()

        bh = next(r for r in results if r.source_type == "browser_history")
        assert bh.status == "not_configured"

    def test_an_existing_file_is_ok(self, monkeypatch, tmp_path):
        history_file = tmp_path / "History"
        history_file.write_text("fake")
        patch_settings(monkeypatch, fake_env(browser_history_path=history_file))

        results = check_all_connections()

        bh = next(r for r in results if r.source_type == "browser_history")
        assert bh.status == "ok"

    def test_a_missing_file_is_an_error(self, monkeypatch, tmp_path):
        missing = tmp_path / "History"
        patch_settings(monkeypatch, fake_env(browser_history_path=missing))

        results = check_all_connections()

        bh = next(r for r in results if r.source_type == "browser_history")
        assert bh.status == "error"


class TestCheckNotion:
    def test_no_api_key_is_not_configured(self, monkeypatch):
        patch_settings(monkeypatch, fake_env(notion_api_key=None))

        results = check_all_connections()

        notion = next(r for r in results if r.source_type == "notion")
        assert notion.status == "not_configured"

    def test_a_working_token_is_ok(self, monkeypatch):
        patch_settings(monkeypatch, fake_env(notion_api_key="fake-token"))

        class FakeUsers:
            def me(self):
                return {"id": "bot-user"}

        class FakeClient:
            def __init__(self, auth):
                self.users = FakeUsers()

        monkeypatch.setattr("notion_client.Client", FakeClient)

        results = check_all_connections()

        notion = next(r for r in results if r.source_type == "notion")
        assert notion.status == "ok"

    def test_a_failing_call_is_an_error(self, monkeypatch):
        patch_settings(monkeypatch, fake_env(notion_api_key="bad-token"))

        class FakeUsers:
            def me(self):
                raise RuntimeError("API token is invalid.")

        class FakeClient:
            def __init__(self, auth):
                self.users = FakeUsers()

        monkeypatch.setattr("notion_client.Client", FakeClient)

        results = check_all_connections()

        notion = next(r for r in results if r.source_type == "notion")
        assert notion.status == "error"
        assert "invalid" in notion.detail


class TestCheckGmail:
    def test_no_credentials_path_is_not_configured(self, monkeypatch):
        patch_settings(monkeypatch, fake_env(gmail_credentials_path=None))

        results = check_all_connections()

        gmail = next(r for r in results if r.source_type == "gmail")
        assert gmail.status == "not_configured"

    def test_credentials_configured_but_not_yet_authorized_is_not_configured(
        self, monkeypatch, tmp_path
    ):
        from extractors.base import ExtractorError

        patch_settings(
            monkeypatch, fake_env(gmail_credentials_path=tmp_path / "creds.json")
        )

        def raise_not_authorized():
            raise ExtractorError("Gmail is not authorized yet. Run ...")

        monkeypatch.setattr("extractors.gmail._get_credentials", raise_not_authorized)

        results = check_all_connections()

        gmail = next(r for r in results if r.source_type == "gmail")
        assert gmail.status == "not_configured"

    def test_a_working_authorization_is_ok(self, monkeypatch, tmp_path):
        patch_settings(
            monkeypatch, fake_env(gmail_credentials_path=tmp_path / "creds.json")
        )
        monkeypatch.setattr("extractors.gmail._get_credentials", lambda: object())

        class FakeProfile:
            def execute(self):
                return {"emailAddress": "me@example.com"}

        class FakeUsers:
            def getProfile(self, userId):
                return FakeProfile()

        class FakeService:
            def users(self):
                return FakeUsers()

        monkeypatch.setattr(
            "extractors.gmail._build_service", lambda creds: FakeService()
        )

        results = check_all_connections()

        gmail = next(r for r in results if r.source_type == "gmail")
        assert gmail.status == "ok"

    def test_a_revoked_token_is_an_error(self, monkeypatch, tmp_path):
        patch_settings(
            monkeypatch, fake_env(gmail_credentials_path=tmp_path / "creds.json")
        )

        def raise_revoked():
            raise RuntimeError("invalid_grant: Token has been revoked.")

        monkeypatch.setattr("extractors.gmail._get_credentials", raise_revoked)

        results = check_all_connections()

        gmail = next(r for r in results if r.source_type == "gmail")
        assert gmail.status == "error"
        assert "revoked" in gmail.detail


class TestCheckGitHub:
    def test_no_token_is_not_configured(self, monkeypatch):
        patch_settings(monkeypatch, fake_env(github_token=None))

        results = check_all_connections()

        github = next(r for r in results if r.source_type == "github")
        assert github.status == "not_configured"

    def test_a_working_token_is_ok(self, monkeypatch):
        import httpx

        patch_settings(monkeypatch, fake_env(github_token="fake-token"))

        def handler(request):
            return httpx.Response(200, json={"login": "octocat"})

        monkeypatch.setattr(
            "httpx.get",
            lambda url, **kwargs: httpx.Client(
                transport=httpx.MockTransport(handler)
            ).get(url, **{k: v for k, v in kwargs.items() if k != "timeout"}),
        )

        results = check_all_connections()

        github = next(r for r in results if r.source_type == "github")
        assert github.status == "ok"

    def test_a_bad_token_is_an_error(self, monkeypatch):
        import httpx

        patch_settings(monkeypatch, fake_env(github_token="bad-token"))

        def handler(request):
            return httpx.Response(401, json={"message": "Bad credentials"})

        monkeypatch.setattr(
            "httpx.get",
            lambda url, **kwargs: httpx.Client(
                transport=httpx.MockTransport(handler)
            ).get(url, **{k: v for k, v in kwargs.items() if k != "timeout"}),
        )

        results = check_all_connections()

        github = next(r for r in results if r.source_type == "github")
        assert github.status == "error"


class TestGetConnectionStatus:
    def test_a_fresh_cache_is_reused_without_rechecking(self, monkeypatch):
        patch_settings(monkeypatch, fake_env())
        calls = []
        monkeypatch.setattr(
            "agent.connection_check.check_all_connections",
            lambda: calls.append(1) or [],
        )

        get_connection_status()
        get_connection_status()

        assert len(calls) == 1

    def test_force_refresh_bypasses_a_fresh_cache(self, monkeypatch):
        patch_settings(monkeypatch, fake_env())
        calls = []
        monkeypatch.setattr(
            "agent.connection_check.check_all_connections",
            lambda: calls.append(1) or [],
        )

        get_connection_status()
        get_connection_status(force_refresh=True)

        assert len(calls) == 2

    def test_a_stale_cache_triggers_a_recheck(self, monkeypatch):
        from datetime import UTC, datetime, timedelta

        patch_settings(monkeypatch, fake_env())
        stale_result = [
            ConnectionStatus(
                source_type="local_file",
                status="ok",
                detail=None,
                checked_at=datetime.now(UTC) - timedelta(seconds=1000),
            )
        ]
        monkeypatch.setattr(connection_check_module, "_cache", stale_result)
        monkeypatch.setattr(
            connection_check_module,
            "_cache_time",
            datetime.now(UTC) - timedelta(seconds=1000),
        )
        calls = []
        monkeypatch.setattr(
            "agent.connection_check.check_all_connections",
            lambda: calls.append(1) or [],
        )

        get_connection_status(max_age_seconds=300)

        assert len(calls) == 1
