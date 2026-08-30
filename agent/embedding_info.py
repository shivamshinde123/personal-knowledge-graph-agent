"""Read-only access to the frozen embedding model name, for display only.

Lives in the agent layer, not ``api/``, for the same reason as every other
thin wrapper here — the API layer depends only on the agent's public
entrypoints, never on ``providers`` directly (see ``api/__init__.py``,
``docs/Component_Map.docx``). ``GET``/``PUT /api/settings`` need this name
purely to display it (it's a frozen constant, not settable — see
``config/settings.py::LLMConfig``'s docstring, ``DECISIONS.md``), so this
module exists only to cross that layer boundary correctly rather than
reaching into ``providers/openrouter_provider.py`` from the route module.

Only one model, not a local/cloud pair — embedding always goes through
OpenRouter regardless of ``provider_mode`` (there is no local embedding
path any more — removed, see ``providers/base.py::get_provider()``,
``DECISIONS.md``).
"""

from __future__ import annotations

from providers.openrouter_provider import EMBEDDING_MODEL as _EMBEDDING_MODEL


def get_embedding_model_name() -> str:
    """Return the frozen embedding model name."""
    return _EMBEDDING_MODEL
