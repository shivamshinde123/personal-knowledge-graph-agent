"""Query Router: decides which retrieval nodes a question needs.

Per ``docs/Technical_Design_Document.docx`` section 8.2 and
``docs/Component_Map.docx``, this is a pure, rule-based heuristic — not an
LLM call. ``ProviderInterface``'s ``Task`` type (``providers/base.py``) has
no "route" entry, and the component map's ``QueryRouter`` row lists only
the three search nodes under "Calls", never ``ProviderInterface``. See
``DECISIONS.md`` for the heuristic's reasoning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A quoted phrase, or a "from/by <someone>" filter, signals the user wants
# an exact lookup (a specific sender, a specific term) rather than a
# thematic question — semantic search adds noise here, so it's skipped.
_QUOTED_PHRASE = re.compile(r'"[^"]+"|\'[^\']+\'')
_SENDER_OR_AUTHOR_LOOKUP = re.compile(
    r"\b(from|by|sent by|written by|authored by)\s+\S+", re.IGNORECASE
)

# Phrasing that implies the answer depends on how items relate to each
# other, not just what any one of them individually says — this is what
# triggers graph traversal, per Technical_Design_Document.docx's own
# example ("semantic search and possibly graph traversal for a broad
# thematic question").
_RELATIONSHIP_PHRASES = (
    "related to",
    "relate to",
    "relates to",
    "connects to",
    "connected to",
    "connection between",
    "connections between",
    "linked to",
    "link between",
    "in relation to",
    "how does",
    "how do",
    "what led to",
    "led up to",
    "impact of",
    "impacts",
    "because of",
    "resulted in",
    "based on",
    "informed by",
    "led to",
)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Which retrieval nodes a question should be routed to."""

    vector_search: bool
    keyword_search: bool
    graph_traversal: bool


def route(question: str) -> RouteDecision:
    """Decide which retrieval nodes a question needs.

    Args:
        question: The user's natural language question.

    Returns:
        Which of vector search, keyword search, and graph traversal to run.
        ``keyword_search`` is always ``True`` — it's cheap and deterministic,
        so there's no real cost to always including it, per the "search is
        hybrid" architectural default (``CLAUDE.md``'s locked-in decisions).
        ``vector_search`` is skipped only for questions that look like an
        exact lookup (a quoted phrase, or a "from/by <someone>" filter).
    """
    is_specific_lookup = bool(_QUOTED_PHRASE.search(question)) or bool(
        _SENDER_OR_AUTHOR_LOOKUP.search(question)
    )
    lowered = question.lower()
    asks_about_relationships = any(
        phrase in lowered for phrase in _RELATIONSHIP_PHRASES
    )
    return RouteDecision(
        vector_search=not is_specific_lookup,
        keyword_search=True,
        graph_traversal=asks_about_relationships,
    )
