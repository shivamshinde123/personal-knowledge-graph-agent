"""Tests for agent/ingest_trigger.py.

``subprocess.Popen`` is faked — a real run would spawn the actual daily
batch, hitting real extractors/embeddings/LLM calls, far too heavy for a
unit test and exactly what the other test suites already cover directly.
"""

import sys

import pytest

from agent.ingest_trigger import cancel_ingestion, trigger_ingestion
from config.settings import PROJECT_ROOT
from storage.sqlite_store import connect, is_cancellation_requested, start_ingestion_run


class FakePopen:
    """Records the args/kwargs it was constructed with."""

    calls: list[tuple[list[str], dict]] = []

    def __init__(self, argv, **kwargs):
        """Record the argv/kwargs this would have been spawned with."""
        FakePopen.calls.append((argv, kwargs))


class TestTriggerIngestion:
    def test_spawns_the_daily_batch_module_with_the_current_interpreter(
        self, monkeypatch
    ):
        # Module invocation ("-m scheduler.daily_batch"), not a raw script
        # path — see agent/ingest_trigger.py's module docstring for why a
        # raw path fails with ModuleNotFoundError.
        FakePopen.calls = []
        monkeypatch.setattr("agent.ingest_trigger.subprocess.Popen", FakePopen)

        trigger_ingestion()

        assert len(FakePopen.calls) == 1
        argv, kwargs = FakePopen.calls[0]
        assert argv == [sys.executable, "-m", "scheduler.daily_batch"]
        assert kwargs["cwd"] == PROJECT_ROOT

    def test_returns_a_run_manual_labeled_id(self, monkeypatch):
        monkeypatch.setattr("agent.ingest_trigger.subprocess.Popen", FakePopen)

        run_id = trigger_ingestion()

        assert run_id.startswith("run_manual_")


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


class TestCancelIngestion:
    def test_requests_cancellation_for_the_given_run(self, conn):
        run_id = start_ingestion_run(conn)

        cancel_ingestion(conn, run_id)

        assert is_cancellation_requested(conn, run_id) is True
