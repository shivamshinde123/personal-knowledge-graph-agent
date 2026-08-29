"""Manual ingestion trigger: spawns the daily batch as a separate process.

Lives in the agent layer, not ``api/``, for the same reason as
``agent/health.py`` and ``agent/sources_status.py`` — the API layer
depends only on the agent's public entrypoints and never reaches into
other layers directly (see ``api/__init__.py``, ``docs/Component_Map.docx``).

Per ``docs/Component_Map.docx``'s dependency rules, the Agent layer
depends on Storage and Providers only, never on Extractors or the
Pipeline layer — and the Scheduler sits above those. Rather than
importing and calling ``scheduler/daily_batch.py`` in-process (which would
cross that boundary, and would share the API process's long-lived
``app.state`` SQLite connection concurrently with normal request
handling — ``storage/sqlite_store.py::connect()``'s own docstring warns
concurrent multi-threaded callers must serialize writes themselves), this
spawns it as an independent OS process, exactly as cron/Task Scheduler
would. See ``DECISIONS.md``.

Spawned via ``-m scheduler.daily_batch`` (module invocation), not a raw
script path (``python scheduler/daily_batch.py``) — the latter only adds
the *script's own directory* to ``sys.path``, not the project root, so
``scheduler/daily_batch.py``'s own absolute imports (``from
config.settings import ...``) fail with ``ModuleNotFoundError`` when run
that way. Verified directly: the project's own previously-documented
invocation command failed with exactly this error; ``-m`` (which adds the
current working directory instead) does not. See ``DECISIONS.md``.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

from config.settings import PROJECT_ROOT


def trigger_ingestion() -> str:
    """Spawn a daily batch run in the background; return a display run label.

    Returns immediately, without waiting for the run to finish. The label
    returned is for the API response only (per
    ``docs/API_Specification.docx`` section 3.8's example,
    ``"run_manual_20260814_1530"``) — it's independent of the UUID
    ``storage/sqlite_store.py::start_ingestion_run()`` assigns internally
    to the actual ``ingestion_runs`` row once the spawned process starts;
    there's no endpoint to look a run up by this label. Progress and
    outcome are checked afterward via ``GET /api/sources/status``, same as
    a scheduled run.

    Returns:
        A human-readable run label for the response body.

    Raises:
        OSError: If the process cannot be spawned at all (e.g. the
            interpreter or script path doesn't exist).
    """
    run_id = f"run_manual_{datetime.now(UTC):%Y%m%d_%H%M}"
    # Fixed, hardcoded argv — no shell, no user-controlled input. "-m" adds
    # PROJECT_ROOT (cwd) to sys.path, unlike a raw script path — see module
    # docstring.
    subprocess.Popen([sys.executable, "-m", "scheduler.daily_batch"], cwd=PROJECT_ROOT)
    return run_id
