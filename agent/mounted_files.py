"""Lists files reachable under the Docker ``/host-data`` mount.

Lives in the agent layer, not ``api/``, for the same reason every other
thin wrapper here does (``agent/browse.py``, ``agent/google_oauth.py``) —
the API layer depends only on the agent's public entrypoints (see
``api/__init__.py``, ``docs/Component_Map.docx``).

**Why this exists**: under Docker, ``BROWSER_HISTORY_PATH`` (and
Gmail/Calendar's older, file-based credential paths — see
``extractors/google_oauth.py``) must point at an in-container path, not a
host one — the running app can't reach an arbitrary host path directly,
only whatever was mounted in at container-start via
``docker-compose.yml``'s ``HOST_DATA_DIR`` (a read-only bind mount at
``/host-data``). Typing an in-container path blind, with no way to see
what's actually reachable there, is exactly the kind of manual step issue
#92 asks to eliminate — this lets the guided setup wizard show a real pick
list instead. See DECISIONS.md.

Outside Docker (a manual dev setup, where ``/host-data`` doesn't exist at
all), :func:`list_host_data_files` always returns an empty list — the
wizard falls back to a plain text input for the real host path in that
case, the same as typing any other local setting.
"""

from __future__ import annotations

from pathlib import Path

# Fixed to match docker-compose.yml's own
# `${HOST_DATA_DIR:-./docker/empty}:/host-data:ro` mount exactly -- not
# derived from settings, since this path is a Docker Compose decision,
# not an application one (see DECISIONS.md, issue #52).
_HOST_DATA_ROOT = Path("/host-data")

# A generous but finite cap -- protects against a user pointing HOST_DATA_DIR
# at something enormous (an entire home directory) and the wizard trying to
# render tens of thousands of rows.
_MAX_FILES = 500


def list_host_data_files() -> list[str]:
    """List every file under the ``/host-data`` mount, relative paths.

    Returns:
        Sorted, ``/host-data``-relative file paths (using ``/`` as the
        separator regardless of host OS, so the frontend can display and
        round-trip them consistently) — capped at :data:`_MAX_FILES`.
        Always empty outside Docker, where ``/host-data`` doesn't exist.
    """
    if not _HOST_DATA_ROOT.is_dir():
        return []
    files = sorted(
        p.relative_to(_HOST_DATA_ROOT).as_posix()
        for p in _HOST_DATA_ROOT.rglob("*")
        if p.is_file()
    )
    return files[:_MAX_FILES]
