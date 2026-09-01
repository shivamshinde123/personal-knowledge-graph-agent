"""Tests for scheduler/loop.py, the Docker scheduler sidecar entrypoint."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scheduler.loop import _should_run, run_forever


class TestShouldRun:
    def test_true_once_the_next_fire_time_has_passed(self):
        last_check = datetime(2026, 9, 1, 22, 59, tzinfo=UTC)
        now = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)

        assert _should_run("0 23 * * *", last_check, now) is True

    def test_false_before_the_next_fire_time(self):
        last_check = datetime(2026, 9, 1, 22, 0, tzinfo=UTC)
        now = datetime(2026, 9, 1, 22, 30, tzinfo=UTC)

        assert _should_run("0 23 * * *", last_check, now) is False

    def test_does_not_refire_for_a_fire_time_already_passed_last_check(self):
        # last_check is already past the 23:00 fire time -- the next one
        # croniter finds is tomorrow's, not today's already-handled one.
        last_check = datetime(2026, 9, 1, 23, 0, 1, tzinfo=UTC)
        now = datetime(2026, 9, 1, 23, 30, tzinfo=UTC)

        assert _should_run("0 23 * * *", last_check, now) is False


class _StopLoopError(Exception):
    """Raised by a fake sleep() to break run_forever()'s infinite loop."""


def _fake_clock(start: datetime, step: timedelta):
    """A now_fn() that advances by `step` on every call, deterministically.

    A mocked sleep() doesn't actually pass time, so run_forever()'s real
    wall clock would otherwise show near-zero elapsed time between polls —
    this simulates real elapsed time instead.
    """
    state = {"now": start}

    def now_fn() -> datetime:
        state["now"] += step
        return state["now"]

    return now_fn


class TestRunForever:
    def test_runs_the_batch_once_the_schedule_comes_due(self, monkeypatch):
        monkeypatch.setattr(
            "scheduler.loop.reload_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(ingestion=SimpleNamespace(schedule="* * * * *"))
            ),
        )
        calls = []

        def fake_sleep(_seconds):
            calls.append("slept")
            if len(calls) > 1:
                raise _StopLoopError

        with pytest.raises(_StopLoopError):
            run_forever(
                sleep=fake_sleep,
                now_fn=_fake_clock(
                    datetime(2026, 9, 1, tzinfo=UTC), timedelta(minutes=1)
                ),
                run_batch=lambda: calls.append("ran"),
            )

        assert "ran" in calls

    def test_a_failed_batch_does_not_end_the_loop(self, monkeypatch):
        monkeypatch.setattr(
            "scheduler.loop.reload_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(ingestion=SimpleNamespace(schedule="* * * * *"))
            ),
        )
        calls = []
        sleep_count = {"n": 0}

        def fake_sleep(_seconds):
            sleep_count["n"] += 1
            calls.append("slept")
            if sleep_count["n"] > 2:
                raise _StopLoopError

        def failing_batch():
            calls.append("ran")
            raise RuntimeError("boom")

        with pytest.raises(_StopLoopError):
            run_forever(
                sleep=fake_sleep,
                now_fn=_fake_clock(
                    datetime(2026, 9, 1, tzinfo=UTC), timedelta(minutes=1)
                ),
                run_batch=failing_batch,
            )

        # Ran (and failed) more than once -- the exception never broke the
        # polling loop itself.
        assert calls.count("ran") >= 2

    def test_does_not_run_when_the_schedule_is_not_due(self, monkeypatch):
        monkeypatch.setattr(
            "scheduler.loop.reload_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(ingestion=SimpleNamespace(schedule="0 23 * * *"))
            ),
        )
        calls = []

        def fake_sleep(_seconds):
            calls.append("slept")
            if len(calls) > 2:
                raise _StopLoopError

        with pytest.raises(_StopLoopError):
            run_forever(
                sleep=fake_sleep,
                # Advances only 1 second per tick -- nowhere near the next
                # 23:00 fire time within 3 ticks.
                now_fn=_fake_clock(
                    datetime(2026, 9, 1, 12, 0, tzinfo=UTC), timedelta(seconds=1)
                ),
                run_batch=lambda: calls.append("ran"),
            )

        assert "ran" not in calls

    def test_reloads_the_schedule_on_every_poll(self, monkeypatch):
        """A schedule edited from Settings takes effect without a restart."""
        schedules = ["0 0 1 1 *", "* * * * *"]  # never due, then always due

        def fake_reload_settings():
            return SimpleNamespace(
                config=SimpleNamespace(ingestion=SimpleNamespace(schedule=schedules[0]))
            )

        monkeypatch.setattr("scheduler.loop.reload_settings", fake_reload_settings)
        calls = []

        def fake_sleep(_seconds):
            calls.append("slept")
            if len(calls) == 1:
                schedules[0] = schedules[1]  # simulate an edit mid-run
            if len(calls) > 2:
                raise _StopLoopError

        with pytest.raises(_StopLoopError):
            run_forever(
                sleep=fake_sleep,
                now_fn=_fake_clock(
                    datetime(2026, 9, 1, tzinfo=UTC), timedelta(minutes=1)
                ),
                run_batch=lambda: calls.append("ran"),
            )

        assert "ran" in calls
