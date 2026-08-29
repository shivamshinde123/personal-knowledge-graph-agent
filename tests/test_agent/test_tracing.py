"""Tests for agent/tracing.py.

Never lets a real key reach os.environ or make this process actually
start tracing — settings and the module's own idempotency flag are both
reset per test.
"""

import os
from types import SimpleNamespace

import pytest

import agent.tracing as tracing_module
from agent.tracing import enable_tracing


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Reset the idempotency flag and any tracing env vars around each test."""
    monkeypatch.setattr(tracing_module, "_enabled", False)
    for key in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.delenv(key, raising=False)


def _settings(api_key):
    return SimpleNamespace(
        env=SimpleNamespace(langsmith_api_key=api_key, langsmith_project="test-project")
    )


class TestEnableTracing:
    def test_no_key_configured_returns_false_and_sets_no_env_vars(self, monkeypatch):
        monkeypatch.setattr("agent.tracing.get_settings", lambda: _settings(None))

        assert enable_tracing() is False
        assert "LANGSMITH_TRACING" not in os.environ

    def test_a_configured_key_enables_tracing_env_vars(self, monkeypatch):
        monkeypatch.setattr(
            "agent.tracing.get_settings", lambda: _settings("fake-key-123")
        )

        assert enable_tracing() is True
        assert os.environ["LANGSMITH_TRACING"] == "true"
        assert os.environ["LANGSMITH_API_KEY"] == "fake-key-123"
        assert os.environ["LANGSMITH_PROJECT"] == "test-project"

    def test_is_idempotent_and_only_reads_settings_once(self, monkeypatch):
        calls = []

        def fake_get_settings():
            calls.append(1)
            return _settings("fake-key-123")

        monkeypatch.setattr("agent.tracing.get_settings", fake_get_settings)

        assert enable_tracing() is True
        assert enable_tracing() is True
        assert len(calls) == 1
