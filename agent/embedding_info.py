"""Read-only access to the frozen embedding model names, for display only.

Lives in the agent layer, not ``api/``, for the same reason as every other
thin wrapper here — the API layer depends only on the agent's public
entrypoints, never on ``providers`` directly (see ``api/__init__.py``,
``docs/Component_Map.docx``). ``GET``/``PUT /api/settings`` need these two
names purely to display them (they're frozen constants, not settable — see
``config/settings.py::LLMConfig``'s docstring, ``DECISIONS.md``), so this
module exists only to cross that layer boundary correctly rather than
reaching into ``providers/local_provider.py``/``openrouter_provider.py``
from the route module.
"""

from __future__ import annotations

from providers.local_provider import EMBEDDING_MODEL as _LOCAL_EMBEDDING_MODEL
from providers.openrouter_provider import EMBEDDING_MODEL as _CLOUD_EMBEDDING_MODEL


def get_embedding_model_names() -> tuple[str, str]:
    """Return the frozen ``(local, cloud)`` embedding model names."""
    return _LOCAL_EMBEDDING_MODEL, _CLOUD_EMBEDDING_MODEL
