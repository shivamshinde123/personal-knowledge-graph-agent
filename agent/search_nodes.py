"""Vector Search Node and Keyword Search Node: the two hybrid-search halves.

Per ``docs/Component_Map.docx``: ``VectorSearchNode`` calls only
``ChromaStore``, ``KeywordSearchNode`` calls only ``SQLiteStore``. Both are
thin wrappers — the real work already lives in ``pipeline/embeddings.py``
(``embed_query``) and the storage layer; these functions just adapt a
natural-language question into the shape those layers expect and return a
common result type ``agent/merger.py`` can combine.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from chromadb.api.models.Collection import Collection

from config.settings import get_settings
from pipeline.embeddings import embed_query
from storage.chroma_store import query as chroma_query
from storage.sqlite_store import keyword_search

# Common, low-signal words stripped before building an FTS5 query, so a
# natural-language question ("What did I work on related to RAG
# pipelines?") searches on its meaningful terms rather than requiring
# (or being thrown off by) words that carry no topical content.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "what",
        "when",
        "where",
        "who",
        "whom",
        "which",
        "why",
        "how",
        "this",
        "that",
        "these",
        "those",
        "of",
        "on",
        "in",
        "to",
        "for",
        "with",
        "about",
        "and",
        "or",
        "i",
        "my",
        "me",
    }
)

_WORD = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One retrieved result, common to both search nodes."""

    item_id: str
    rank: int  # 1-based rank within this node's own result list


def vector_search(collection: Collection, question: str) -> list[SearchHit]:
    """Run semantic search against Chroma for a question.

    Args:
        collection: An open Chroma collection.
        question: The user's natural language question.

    Returns:
        Hits ordered by similarity (closest first), one per matching chunk
        deduplicated down to distinct items — a question can match several
        chunks of the same item, which should count as one hit, not
        several.
    """
    top_k = get_settings().config.retrieval.top_k_vector
    query_embedding = embed_query(question)
    results = chroma_query(collection, query_embedding, top_k=top_k)
    return _to_hits(result.item_id for result in results)


def keyword_search_node(conn: sqlite3.Connection, question: str) -> list[SearchHit]:
    """Run BM25 keyword search against SQLite for a question.

    Args:
        conn: An open SQLite connection.
        question: The user's natural language question.

    Returns:
        Hits ordered by relevance (best match first), deduplicated down to
        distinct items. Empty if the question has no significant terms
        left after stopword removal (rather than running an empty/invalid
        FTS5 query).
    """
    fts_query = _to_fts_query(question)
    if not fts_query:
        return []
    top_k = get_settings().config.retrieval.top_k_keyword
    results = keyword_search(conn, fts_query, top_k=top_k)
    return _to_hits(result.chunk.item_id for result in results)


def _to_fts_query(question: str) -> str:
    """Build a safe FTS5 ``MATCH`` expression from a natural-language question.

    Each significant term is double-quoted (matched as a literal string
    rather than parsed for FTS5 operators like ``AND``/``NOT``/``*``) and
    joined with ``OR``, so a question matches items containing *any* of its
    significant terms, ranked by how well they match — a natural-language
    question shouldn't require every single word to be present the way an
    implicit FTS5 ``AND`` would.
    """
    terms = [
        word.lower()
        for word in _WORD.findall(question)
        if word.lower() not in _STOPWORDS
    ]
    if not terms:
        return ""
    return " OR ".join(f'"{term}"' for term in terms)


def _to_hits(item_ids) -> list[SearchHit]:
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        hits.append(SearchHit(item_id=item_id, rank=len(hits) + 1))
    return hits
