"""Tests for api/main.py's startup helpers."""

from types import SimpleNamespace

from api.main import _select_bind_host


class TestSelectBindHost:
    def test_binds_loopback_on_a_bare_metal_setup(self, monkeypatch):
        monkeypatch.setattr(
            "api.main.get_settings",
            lambda: SimpleNamespace(env=SimpleNamespace(running_in_docker=False)),
        )

        assert _select_bind_host() == "127.0.0.1"

    def test_binds_all_interfaces_when_running_in_docker(self, monkeypatch):
        """127.0.0.1 inside a container is invisible to Docker's port publishing.

        See _select_bind_host()'s own docstring, DECISIONS.md.
        """
        monkeypatch.setattr(
            "api.main.get_settings",
            lambda: SimpleNamespace(env=SimpleNamespace(running_in_docker=True)),
        )

        assert _select_bind_host() == "0.0.0.0"
