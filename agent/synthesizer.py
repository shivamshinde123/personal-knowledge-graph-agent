"""Answer Synthesizer: turns merged results into a cited natural-language answer.

Per ``docs/Component_Map.docx``: ``AnswerSynthesizer`` calls
``ProviderInterface`` and ``SQLiteStore``. Per
``docs/Technical_Design_Document.docx`` section 8.2 step 4, retrieved
chunks are pulled from SQLite by their reference ids and passed, along
with the original question, to the LLM for synthesis.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from agent.merger import MergedResult
from config.settings import get_settings
from providers.base import ContextChunk, get_provider
from storage.sqlite_store import get_chunks_for_item, get_item

_NO_RESULTS_ANSWER = "I couldn't find anything relevant to answer that."


@dataclass(frozen=True, slots=True)
class Source:
    """One cited source in a synthesized answer."""

    item_id: str
    source_type: str
    title: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class SynthesizedAnswer:
    """The final answer, plus the sources it's grounded in."""

    answer: str
    sources: list[Source]


def synthesize(
    conn: sqlite3.Connection,
    question: str,
    merged_results: list[MergedResult],
    *,
    top_k: int | None = None,
) -> SynthesizedAnswer:
    """Fetch full text for the top merged results and synthesize a cited answer.

    Args:
        conn: An open SQLite connection.
        question: The user's original natural language question.
        merged_results: Ranked results from ``agent/merger.py::merge()``.
        top_k: Maximum number of items to pull full text for and pass as
            context — caps LLM call cost/latency regardless of how many
            candidates the merger ranked. Defaults to ``config.yaml``'s
            ``retrieval.top_k_vector``, reusing the same cap already
            governing individual node result counts rather than
            introducing a new, undocumented knob (see DECISIONS.md).

    Returns:
        The synthesized answer and the ordered list of sources it was
        grounded in — index ``i`` of ``sources`` matches the ``[i + 1]``
        citation marker the provider is instructed to use. A merged result
        whose item no longer exists in SQLite, or has no chunks, is
        skipped rather than aborting synthesis. If no result yields usable
        context, a fixed "nothing found" answer is returned without
        calling the LLM at all.
    """
    if top_k is None:
        top_k = get_settings().config.retrieval.top_k_vector

    context: list[ContextChunk] = []
    sources: list[Source] = []
    for result in merged_results[:top_k]:
        item = get_item(conn, result.item_id)
        if item is None:
            continue
        chunks = get_chunks_for_item(conn, result.item_id)
        if not chunks:
            continue
        text = "\n\n".join(chunk.text for chunk in chunks)
        context.append(
            ContextChunk(
                text=text,
                source_type=item.source_type,
                title=item.title,
                url=item.url_or_path,
            )
        )
        sources.append(
            Source(
                item_id=item.id,
                source_type=item.source_type,
                title=item.title,
                url=item.url_or_path,
            )
        )

    if not context:
        return SynthesizedAnswer(answer=_NO_RESULTS_ANSWER, sources=[])

    answer = get_provider("answer").generate_answer(question, context)
    return SynthesizedAnswer(answer=answer, sources=sources)
