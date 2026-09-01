"""Tests for agent/ingest_trigger.py.

``subprocess.Popen`` is faked — a real run would spawn the actual daily
batch, hitting real extractors/embeddings/LLM calls, far too heavy for a
unit test and exactly what the other test suites already cover directly.

``os.kill`` is *always* faked in every ``cancel_ingestion()`` test below —
``start_ingestion_run()`` records ``os.getpid()`` as the run's own pid, and
since these tests never actually spawn a real subprocess, that pid is the
test runner's own process. Calling the real ``os.kill`` would send
``SIGTERM`` to pytest itself.
"""

import signal
import sys

import pytest

from agent.ingest_trigger import cancel_ingestion, trigger_ingestion
from config.settings import PROJECT_ROOT
from storage.sqlite_store import (
    complete_ingestion_run,
    connect,
    get_ingestion_run,
    is_cancellation_requested,
    start_ingestion_run,
)


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
    def test_requests_cancellation_for_the_given_run(self, conn, monkeypatch):
        monkeypatch.setattr("agent.ingest_trigger.os.kill", lambda pid, sig: None)
        run_id = start_ingestion_run(conn)

        cancel_ingestion(conn, run_id)

        assert is_cancellation_requested(conn, run_id) is True

    def test_force_kills_the_recorded_pid(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "agent.ingest_trigger.os.kill",
            lambda pid, sig: calls.append((pid, sig)),
        )
        run_id = start_ingestion_run(conn)
        run = get_ingestion_run(conn, run_id)

        cancel_ingestion(conn, run_id)

        assert len(calls) == 1
        pid, sig = calls[0]
        assert pid == run.pid
        assert sig == signal.SIGTERM

    def test_finalizes_the_run_as_cancelled(self, conn, monkeypatch):
        monkeypatch.setattr("agent.ingest_trigger.os.kill", lambda pid, sig: None)
        run_id = start_ingestion_run(conn)

        cancel_ingestion(conn, run_id)

        run = get_ingestion_run(conn, run_id)
        assert run.status == "cancelled"
        assert run.error_log == "Force-stopped by user"

    def test_a_missing_pid_does_not_finalize_the_run(self, conn, monkeypatch):
        """A missing pid means the kill can never be confirmed.

        Finalizing as "cancelled" anyway would misrepresent reality (the
        process could still be running) and could let a second run start
        concurrently. Only the cooperative flag is set; status is left
        alone for the batch to finalize itself. See DECISIONS.md.
        """
        calls = []
        monkeypatch.setattr(
            "agent.ingest_trigger.os.kill", lambda pid, sig: calls.append(pid)
        )
        run_id = start_ingestion_run(conn)
        conn.execute("UPDATE ingestion_runs SET pid = NULL WHERE id = ?", (run_id,))
        conn.commit()

        cancel_ingestion(conn, run_id)

        assert calls == []  # nothing to kill
        run = get_ingestion_run(conn, run_id)
        assert run.status == "running"
        assert is_cancellation_requested(conn, run_id) is True

    def test_a_failed_kill_does_not_finalize_the_run(self, conn, monkeypatch):
        """An OSError other than ProcessLookupError means the kill is unconfirmed.

        E.g. a permissions error — the process could still be alive, so
        this must not claim it stopped. Same reasoning as a missing pid.
        """

        def raise_permission_error(pid, sig):
            raise PermissionError("Access is denied")

        monkeypatch.setattr("agent.ingest_trigger.os.kill", raise_permission_error)
        run_id = start_ingestion_run(conn)

        cancel_ingestion(conn, run_id)  # must not raise

        run = get_ingestion_run(conn, run_id)
        assert run.status == "running"
        assert is_cancellation_requested(conn, run_id) is True

    def test_an_already_exited_pid_does_not_raise(self, conn, monkeypatch):
        def raise_not_found(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr("agent.ingest_trigger.os.kill", raise_not_found)
        run_id = start_ingestion_run(conn)

        cancel_ingestion(conn, run_id)  # must not raise

        assert get_ingestion_run(conn, run_id).status == "cancelled"

    def test_a_run_that_already_completed_is_left_alone(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "agent.ingest_trigger.os.kill", lambda pid, sig: calls.append(pid)
        )
        run_id = start_ingestion_run(conn)
        complete_ingestion_run(conn, run_id, status="success", items_processed=5)

        cancel_ingestion(conn, run_id)

        assert calls == []  # not "running" anymore -- nothing to kill
        run = get_ingestion_run(conn, run_id)
        assert run.status == "success"  # not clobbered to "cancelled"
        assert run.items_processed == 5

    def test_an_unknown_run_id_does_nothing(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "agent.ingest_trigger.os.kill", lambda pid, sig: calls.append(pid)
        )

        cancel_ingestion(conn, "no-such-run")  # must not raise

        assert calls == []
