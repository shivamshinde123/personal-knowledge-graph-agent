"""Native folder picker, for ``POST /api/settings/browse-folder``.

Lives in the agent layer, not ``api/``, for the same reason as every other
thin wrapper here (``agent/health.py``, ``agent/ingest_trigger.py``,
``agent/admin.py``) — the API layer depends only on the agent's public
entrypoints (see ``api/__init__.py``, ``docs/Component_Map.docx``).

A browser cannot reveal the real filesystem path of a folder picked
through its own file/directory picker — neither `<input
type="file" webkitdirectory>` nor the File System Access API's
``showDirectoryPicker()`` exposes an absolute path, by deliberate browser
sandboxing (see DECISIONS.md). Since this backend runs on the same local
machine as the user (this whole system is single-user, local-only — see
``CLAUDE.md``), the fix is to open a *native* OS folder dialog from the
backend process itself and return the real path it picks, rather than
trying to get one out of the browser.

Implemented via a PowerShell ``System.Windows.Forms.FolderBrowserDialog``
subprocess rather than ``tkinter`` — this project's dev/deploy environment
is Windows (``CLAUDE.md``'s Environment section), and a subprocess dialog
runs in its own process with its own main thread, sidestepping any
"tkinter dialogs called from a non-main thread" concern that a FastAPI
sync route (executed in uvicorn's threadpool, not the main thread) would
otherwise raise. Windows-only for now — a cross-platform version (e.g.
falling back to ``tkinter`` on macOS/Linux) is future work if this project
is ever run elsewhere. See DECISIONS.md.
"""

from __future__ import annotations

import subprocess

_DIALOG_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
    "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
    "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ Write-Output $dialog.SelectedPath }"
)


def browse_folder() -> str | None:
    """Open a native folder-picker dialog and return the selected path.

    Blocks until the user picks a folder or cancels/closes the dialog —
    that's inherent to a foreground picker, not a bug; the frontend
    disables its own "Browse…" button for the duration, the same pattern
    already used for "Reverify"/"Run ingestion now".

    Returns:
        The selected folder's absolute path, or ``None`` if the user
        cancelled.

    Raises:
        OSError: If PowerShell itself cannot be launched at all.
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _DIALOG_SCRIPT],
        capture_output=True,
        text=True,
        timeout=300,
    )
    path = result.stdout.strip()
    return path or None
