"""Result Merger: combines whichever retrieval nodes ran via Reciprocal Rank Fusion.

Per ``docs/Component_Map.docx``: ``ResultMerger`` is a pure function with no
external calls, combining and ranking results from vector search, keyword
search, and (optionally) graph traversal using Reciprocal Rank Fusion — the
method named in ``docs/Technical_Design_Document.docx`` section 6.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.search_nodes import SearchHit

# The standard smoothing constant from the original Reciprocal Rank Fusion
# paper (Cormack, Clarke & Buettcher, 2009): score += 1 / (k + rank). Not
# specified in /docs, so this uses the widely-adopted default rather than
# inventing a project-specific value — see DECISIONS.md.
_RRF_K = 60


@dataclass(frozen=True, slots=True)
class MergedResult:
    """One item's combined rank across whichever retrieval nodes ran."""

    item_id: str
    score: float


def merge(*hit_lists: list[SearchHit]) -> list[MergedResult]:
    """Combine one or more ranked hit lists into a single ranked list.

    Args:
        *hit_lists: Each retrieval node's own hits (vector search, keyword
            search, graph traversal), in that node's own rank order. A node
            that didn't run per ``agent/router.py::route()`` simply
            contributes an empty list (or is omitted entirely).

    Returns:
        Every distinct item across all lists, ranked by combined
        Reciprocal Rank Fusion score (highest first) — an item appearing in
        more lists, or ranked higher within a list, scores higher.
    """
    scores: dict[str, float] = {}
    for hits in hit_lists:
        for hit in hits:
            scores[hit.item_id] = scores.get(hit.item_id, 0.0) + 1.0 / (
                _RRF_K + hit.rank
            )
    ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return [MergedResult(item_id=item_id, score=score) for item_id, score in ordered]
