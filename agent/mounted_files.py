"""Lists files/folders reachable under this app's fixed Docker mounts.

Lives in the agent layer, not ``api/``, for the same reason every other
thin wrapper here does (``agent/browse.py``, ``agent/google_oauth.py``) —
the API layer depends only on the agent's public entrypoints (see
``api/__init__.py``, ``docs/Component_Map.docx``).

**Why this exists**: under Docker, ``BROWSER_HISTORY_PATH`` (and
Gmail/Calendar's older, file-based credential paths — see
``extractors/google_oauth.py``) and ``LOCAL_FILES_WATCH_DIRS`` must all
point at in-container paths, not host ones — the running app can't reach
an arbitrary host path directly, only whatever was mounted in at
container-start via ``docker-compose.yml``'s ``HOST_DATA_DIR``/
``HOST_WATCH_DIR`` (read-only bind mounts at ``/host-data``/
``/data/watched``). Typing an in-container path blind, with no way to see
what's actually reachable there, is exactly the kind of manual step issue
#92 asks to eliminate — this lets the guided setup wizard (and Settings)
show a real pick list instead. See DECISIONS.md.

Outside Docker (a manual dev setup, where neither mount exists at all),
both listing functions always return an empty list — local files already
has its own native folder picker (``agent/browse.py``) in that case, and
the wizard falls back to a plain text input for browser history's real
host path.
"""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath

# Fixed to match docker-compose.yml's own
# `${HOST_DATA_DIR:-./docker/empty}:/host-data:ro` and
# `${HOST_WATCH_DIR:-./docker/empty}:/data/watched:ro` mounts exactly --
# not derived from settings, since these paths are a Docker Compose
# decision, not an application one (see DECISIONS.md, issues #52, #92).
_HOST_DATA_ROOT = Path("/host-data")
_WATCHED_ROOT = Path("/data/watched")
_WATCHED_ROOT_POSIX = PurePosixPath("/data/watched")

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


def list_watched_directories() -> list[str]:
    """List immediate subdirectories under the ``/data/watched`` mount.

    Full in-container paths (e.g. ``"/data/watched/project-a"``), not
    relative ones — the wizard/Settings picker uses these directly as
    ``local_files_watch_dirs`` entries, no path reconstruction needed on
    the frontend side. One level deep only (a folder-level pick, matching
    how ``local_files_watch_dirs`` is used — each entry is a directory
    ``extractors/local_files.py`` recurses within, not a single file).

    Returns:
        Sorted subdirectory paths, capped at :data:`_MAX_FILES`. Always
        empty outside Docker, where ``/data/watched`` doesn't exist.
    """
    if not _WATCHED_ROOT.is_dir():
        return []
    dirs = sorted(str(p) for p in _WATCHED_ROOT.iterdir() if p.is_dir())
    return dirs[:_MAX_FILES]


def is_within_watched_root(path: str) -> bool:
    """Is ``path`` the ``/data/watched`` mount itself, or somewhere under it?

    A pure path-component check — never touches the filesystem, so it
    works the same whether or not ``path`` actually exists yet, and in
    tests run outside a real container. Used to guard
    ``PUT /api/settings/sources``'s ``local_files_watch_dirs`` under
    Docker: a value here can be narrowed to any subset of what's already
    mounted, but can never escape it — the running app still can't mount
    a *new* host folder into itself, only Docker Compose can, at
    container-start. See DECISIONS.md, issue #92.

    ``posixpath.normpath()`` resolves ``.``/``..`` segments *lexically*
    (string manipulation, no filesystem access) before the containment
    check — verified directly this matters: ``PurePosixPath.parents``
    alone treats ``".."`` as an opaque path component rather than
    resolving it, so ``"/data/watched/../etc"`` would otherwise lexically
    (and wrongly) appear to have ``/data/watched`` as one of its parents,
    even though it actually names a path *outside* the mount entirely.

    Args:
        path: A candidate ``local_files_watch_dirs`` entry.

    Returns:
        ``True`` if ``path``, once ``..``/``.``-resolved, is
        ``/data/watched`` itself or a descendant of it (evaluated as a
        POSIX path regardless of host OS, since the container is always
        Linux) — ``False`` otherwise, including for any path that isn't
        even absolute.
    """
    candidate = PurePosixPath(posixpath.normpath(path))
    return candidate == _WATCHED_ROOT_POSIX or _WATCHED_ROOT_POSIX in candidate.parents
