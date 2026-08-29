"""LangSmith tracing: enables tracing for the whole process, opt-in.

Deliberately *not* called from ``agent/graph.py::run()`` itself, or
anywhere else on the query path that runs during tests: many tests call
the real, unmonkeypatched ``config.settings.get_settings()`` (only
specific settings sub-values get monkeypatched, not the whole function),
so if enabling tracing were automatic inside ``run()``, a real, configured
``LANGSMITH_API_KEY`` would cause every test invoking the agent to start
sending real trace data to the user's real LangSmith project. Instead,
this is called explicitly, once, by the two real entry points that should
actually be traced: ``api/main.py``'s startup lifespan (production query
traffic — tests substitute a no-op lifespan, per
``tests/test_api/conftest.py``, so this never runs during a test) and
``eval/run_evaluation.py`` (eval runs). See ``DECISIONS.md``.
"""

from __future__ import annotations

import logging
import os

from config.settings import get_settings

logger = logging.getLogger(__name__)

_enabled = False


def enable_tracing() -> bool:
    """Turn on LangSmith tracing for this process, if a key is configured.

    Idempotent — safe to call more than once. Sets the environment
    variables LangChain/LangGraph's own tracing auto-instrumentation reads
    (``LANGSMITH_TRACING``, ``LANGSMITH_API_KEY``, ``LANGSMITH_PROJECT``);
    nothing in ``agent/graph.py`` itself needs to import ``langsmith`` or
    change for a traced run to appear in the configured LangSmith project
    once these are set.

    Returns:
        ``True`` if tracing is enabled (a ``langsmith_api_key`` was
        configured, now or on an earlier call), ``False`` if there was
        nothing to enable.
    """
    global _enabled
    if _enabled:
        return True
    env = get_settings().env
    if not env.langsmith_api_key:
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = env.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = env.langsmith_project
    _enabled = True
    logger.info("LangSmith tracing enabled for project %r", env.langsmith_project)
    return True
