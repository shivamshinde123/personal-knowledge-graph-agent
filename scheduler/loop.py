"""Docker scheduler sidecar entrypoint: runs the daily batch on a schedule.

Run via ``uv run python -m scheduler.loop`` (module invocation — same
``ModuleNotFoundError`` reasoning as ``scheduler/daily_batch.py``'s own
module docstring: a raw script path only adds this file's own directory to
``sys.path``, not the project root).

This is the **Docker-only** entrypoint — ``docker-compose.yml`` runs it as
its own long-lived ``scheduler`` service, replacing "set up your own
cron/Task Scheduler entry by hand" entirely for a containerized deployment
(issue #52). A manual developer setup still registers
``scheduler/daily_batch.py`` with the OS's own real cron/Task Scheduler, as
documented in the README — this loop doesn't replace that, it only gives
Docker Compose something to run as a supervised, restart-policy-managed
service, since there's no host-level cron/Task Scheduler available inside
a container to register with instead.

Polls every :data:`_POLL_INTERVAL_SECONDS` rather than sleeping a single
long stretch until the next computed fire time — this means
``config.yaml``'s ``ingestion.schedule`` is re-read on every poll, so
editing it from Settings takes effect within one poll interval without
restarting this container, rather than only on the next restart.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from croniter import croniter

from config.settings import reload_settings
from scheduler.daily_batch import main as run_daily_batch

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 30.0


def _should_run(schedule: str, last_check: datetime, now: datetime) -> bool:
    """Has ``schedule``'s next fire time after ``last_check`` already passed?

    Args:
        schedule: A standard 5-field cron expression
            (``config.yaml``'s ``ingestion.schedule``).
        last_check: The last time this was checked — the search starts
            strictly after this instant, so the same fire time is never
            matched twice in a row.
        now: The current time.

    Returns:
        ``True`` if a scheduled run is due.
    """
    next_fire = croniter(schedule, last_check).get_next(datetime)
    return next_fire <= now


def run_forever(
    *,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_batch: Callable[[], None] = run_daily_batch,
) -> None:
    """Poll forever, running one batch each time the schedule comes due.

    A failed batch is logged and never ends the loop — the same "one bad
    run doesn't end the schedule" reasoning as
    ``scheduler/daily_batch.py``'s own per-source/per-item error handling.

    Args:
        poll_interval_seconds: How often to check whether a run is due.
            Defaults to :data:`_POLL_INTERVAL_SECONDS`.
        sleep: Injected for testing — a test substitutes something that
            raises after a fixed number of calls, since this function
            otherwise never returns.
        now_fn: Injected for testing — defaults to the real wall clock. A
            test substitutes a fake clock that advances deterministically
            per call, since real elapsed time between a mocked ``sleep``'s
            calls would otherwise be near zero.
        run_batch: Injected for testing — defaults to
            ``scheduler.daily_batch.main``.
    """
    last_check = now_fn()
    while True:
        sleep(poll_interval_seconds)
        now = now_fn()
        schedule = reload_settings().config.ingestion.schedule
        if _should_run(schedule, last_check, now):
            logger.info("Running scheduled ingestion batch (schedule=%r)", schedule)
            try:
                run_batch()
            except Exception:
                logger.exception("Scheduled ingestion run failed")
        last_check = now


if __name__ == "__main__":
    from config.settings import get_settings

    logging.basicConfig(level=get_settings().env.log_level)
    logger.info(
        "Scheduler sidecar starting — polling every %.0fs", _POLL_INTERVAL_SECONDS
    )
    run_forever()
