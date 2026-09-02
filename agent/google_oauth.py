"""Thin wrapper around ``extractors/google_oauth.py`` for the API layer.

Lives in the agent layer, not ``api/``, for the same reason every other
thin wrapper here does (``agent/health.py``, ``agent/browse.py``,
``agent/ingest_trigger.py``, ``agent/admin.py``) — the API layer depends
only on the agent's public entrypoints, never reaching into another layer
directly (see ``api/__init__.py``, ``docs/Component_Map.docx``).

The real OAuth mechanics live in ``extractors/google_oauth.py`` — a peer
to ``extractors/gmail.py``/``extractors/calendar.py`` within the
extractors layer, since ``agent/`` may never depend on ``extractors/`` for
real business logic (see that module's own docstring for the full
reasoning). This module exists purely so ``api/routes/setup.py`` has an
agent-layer entrypoint to call instead of importing ``extractors/``
directly, matching every other route's dependency shape.
"""

from __future__ import annotations

from extractors.google_oauth import (
    GoogleOAuthError,
    complete_authorization,
    is_connected,
    load_credentials,
    start_authorization,
    token_path,
)

__all__ = [
    "GoogleOAuthError",
    "complete_authorization",
    "is_connected",
    "load_credentials",
    "start_authorization",
    "token_path",
]
