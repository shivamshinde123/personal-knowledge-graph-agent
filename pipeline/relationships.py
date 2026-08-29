"""Relationship detection: candidate narrowing via Chroma, then LLM confirmation.

Per ``docs/Technical_Design_Document.docx``, relationships are found by
vector-similarity candidate narrowing, never by an LLM comparison against
the full dataset — this module queries Chroma for the item's nearest
neighbors and only asks the LLM to judge those. The narrowing query uses a
whole-document embedding (the mean of the item's own chunk embeddings, all
already computed and stored by ``pipeline/embeddings.py``), not just the
first chunk — the first chunk alone is often boilerplate (a title block, a
"prepared for" line) shared near-verbatim across unrelated items, which
would otherwise dominate the similarity signal. See ``DECISIONS.md``.

The text shown to the LLM for confirmation is chosen the same way, per
pair: the candidate side already gets its actual matching chunk for free
(Chroma's own search result *is* the chunk that caused the match), but the
source side picks whichever of its own chunks is closest, by cosine
similarity, to the candidate's whole-document embedding — not always a
fixed chunk regardless of which candidate is being judged. See
``DECISIONS.md``, 2026-08-29.

Full item details (title, url) for the graph nodes this writes aren't
available from Chroma's metadata alone, so — unlike
``docs/Component_Map.docx``'s dependency list for ``RelationshipDetector``,
which names only ``ChromaStore``, ``ProviderInterface``, and ``Neo4jStore``
— this module also reads from SQLite. See ``DECISIONS.md``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

import neo4j
import numpy as np
from chromadb.api.models.Collection import Collection

from config.settings import get_settings
from providers.base import ProviderError, RelationshipJudgment, get_provider
from storage.chroma_store import (
    ItemChunkVector,
    get_item_chunk_vectors,
    get_item_embeddings,
    query,
)
from storage.neo4j_store import (
    GraphStoreError,
    ItemNode,
    Relationship,
    has_any_relationship,
    write_relationship,
)
from storage.sqlite_store import Item, get_chunks_for_item, get_item

logger = logging.getLogger(__name__)

# Per CLAUDE.md's locked-in decisions: "Browser History is intentionally
# the lightest-weight source ... no relationship detection." Excluded from
# candidate narrowing here (not just skipped as a detection source in
# scheduler/daily_batch.py) so it never enters the graph in either
# direction — see DECISIONS.md, 2026-08-25.
_NO_RELATIONSHIP_DETECTION_SOURCE = "browser_history"


def detect_relationships(
    conn: sqlite3.Connection,
    driver: neo4j.Driver,
    collection: Collection,
    source_item_id: str,
) -> list[tuple[str, RelationshipJudgment]]:
    """Find and write confirmed relationships for one recently-ingested item.

    Returns immediately, without any LLM or graph work, if the item's
    source is excluded from relationship detection entirely (currently just
    ``browser_history`` — see the module-level constant and DECISIONS.md,
    2026-08-25). Otherwise, queries Chroma for the item's nearest-neighbor
    chunks (narrowed to the same ``project_name`` when the item has one
    classified), deduplicates matches down to distinct candidate items, and
    asks the LLM to confirm or reject each one. A candidate farther than
    ``config.yaml``'s ``retrieval.relationship_candidate_max_distance``
    (cosine distance; ``null`` disables this) never reaches the LLM at all —
    narrowing always returns its top-K nearest neighbors regardless of how
    weak the match actually is, so without this filter, a document with no
    genuine matches anywhere in the corpus still gets K arbitrary candidates
    judged (see DECISIONS.md, 2026-08-29). A judgment confirmed with
    confidence below ``retrieval.relationship_confidence_threshold`` is
    discarded like an unconfirmed one — vector-narrowed candidates are only
    ever topically adjacent, not necessarily related, so a low-confidence
    "yes" from the LLM is treated as noise rather than written to the graph
    (see DECISIONS.md). A candidate already connected to the source by any
    existing edge, in either direction, is skipped before the LLM is even
    called — detection runs per newly-processed item, so without this check
    the same real-world relationship could get independently judged (and
    written as a second, opposite-direction edge) once when each endpoint
    is processed (see DECISIONS.md). A candidate whose LLM call or graph
    write fails is logged and skipped rather than aborting the rest — one
    bad candidate shouldn't cost the item every other relationship it might
    have.

    Args:
        conn: An open SQLite connection, for looking up full item details.
        driver: An open Neo4j driver.
        collection: An open Chroma collection.
        source_item_id: The effective SQLite id of the item to find
            relationships for (typically one just ingested).

    Returns:
        ``(candidate_item_id, judgment)`` pairs for every relationship
        confirmed and written.
    """
    source = get_item(conn, source_item_id)
    if source is None:
        logger.warning(
            "detect_relationships called for unknown item %r", source_item_id
        )
        return []
    if source.source_type == _NO_RELATIONSHIP_DETECTION_SOURCE:
        return []

    chunks = get_chunks_for_item(conn, source_item_id)
    if not chunks:
        return []

    source_chunk_vectors = get_item_chunk_vectors(collection, source_item_id)
    document_embedding = _mean_embedding([cv.embedding for cv in source_chunk_vectors])
    if document_embedding is None:
        return []

    top_k = get_settings().config.retrieval.relationship_candidate_count
    where: dict = {
        "$and": [
            {"item_id": {"$ne": source_item_id}},
            {"source_type": {"$ne": _NO_RELATIONSHIP_DETECTION_SOURCE}},
        ]
    }
    if source.project_name is not None:
        where = {"$and": [*where["$and"], {"project_name": source.project_name}]}
    candidates = query(collection, document_embedding, top_k=top_k, where=where)

    provider = get_provider("relationship")
    source_node = _to_item_node(source)
    retrieval_config = get_settings().config.retrieval
    confidence_threshold = retrieval_config.relationship_confidence_threshold
    max_distance = retrieval_config.relationship_candidate_max_distance

    confirmed: list[tuple[str, RelationshipJudgment]] = []
    seen_item_ids: set[str] = set()
    for candidate in candidates:
        if candidate.item_id in seen_item_ids:
            continue
        seen_item_ids.add(candidate.item_id)

        if max_distance is not None and candidate.distance > max_distance:
            # Narrowing always returns its top-K nearest neighbors, even
            # when none of them are genuinely close — this is the "least
            # unrelated item available" case, filtered out before it ever
            # costs an LLM call (see DECISIONS.md).
            continue

        candidate_item = get_item(conn, candidate.item_id)
        if candidate_item is None:
            continue

        try:
            if has_any_relationship(driver, source_item_id, candidate.item_id):
                # Already related, in either direction — from this item's
                # own earlier run, or from the candidate's. Re-judging
                # would risk writing a second, opposite-direction edge for
                # what's really the same discovered relationship (see
                # DECISIONS.md).
                continue
            candidate_embedding = _mean_embedding(
                get_item_embeddings(collection, candidate.item_id)
            )
            source_text = _most_similar_chunk_text(
                source_chunk_vectors, candidate_embedding
            )
            judgment = provider.generate_relationship(source_text, candidate.document)
            if judgment is None:
                continue
            if (
                judgment.confidence is not None
                and judgment.confidence < confidence_threshold
            ):
                logger.debug(
                    "Discarding low-confidence relationship between %r and %r: "
                    "%s (%.2f < %.2f)",
                    source_item_id,
                    candidate.item_id,
                    judgment.label,
                    judgment.confidence,
                    confidence_threshold,
                )
                continue
            write_relationship(
                driver,
                source_node,
                _to_item_node(candidate_item),
                Relationship(
                    label=judgment.label,
                    confidence=judgment.confidence,
                    created_at=datetime.now(UTC),
                ),
            )
        except (ProviderError, GraphStoreError) as exc:
            logger.warning(
                "Relationship check between %r and %r failed, skipping: %s",
                source_item_id,
                candidate.item_id,
                exc,
            )
            continue

        confirmed.append((candidate.item_id, judgment))

    return confirmed


def _to_item_node(item: Item) -> ItemNode:
    return ItemNode(
        id=item.id,
        source_type=item.source_type,
        title=item.title,
        project_name=item.project_name,
        topic=item.topic,
        created_at=item.created_at,
        url=item.url_or_path,
    )


def _most_similar_chunk_text(
    chunk_vectors: list[ItemChunkVector], target_embedding: list[float] | None
) -> str:
    """Pick whichever chunk's text is closest, by cosine similarity, to a target.

    Used to choose which of the *source* item's own chunks to show the LLM
    for one specific candidate — the chunk most relevant to that candidate
    in particular, not a fixed chunk reused regardless of which candidate is
    being judged (the candidate side doesn't need this: Chroma's own search
    result already *is* the candidate's most relevant chunk). See
    ``DECISIONS.md``, 2026-08-29.

    Args:
        chunk_vectors: The source item's chunks, text paired with embedding.
            Never empty when called — the caller already confirmed at least
            one chunk exists before reaching this point.
        target_embedding: The candidate's whole-document embedding, or
            ``None`` if it has no chunks in Chroma (an inconsistent but
            possible state) — falls back to the first chunk's text.

    Returns:
        The text of the closest (or, on a ``None`` target, first) chunk.
    """
    if target_embedding is None:
        return chunk_vectors[0].document
    best = max(
        chunk_vectors,
        key=lambda cv: _cosine_similarity(cv.embedding, target_embedding),
    )
    return best.document


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors, 0.0 if either is a zero vector."""
    a_arr, b_arr = np.array(a), np.array(b)
    denominator = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if denominator == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / denominator)


def _mean_embedding(vectors: list[list[float]]) -> list[float] | None:
    """Average a set of chunk embeddings into one whole-document vector.

    Args:
        vectors: An item's chunk embeddings.

    Returns:
        The mean vector, or ``None`` if ``vectors`` is empty (the item has
        no chunks in Chroma yet).
    """
    if not vectors:
        return None
    return np.mean(np.array(vectors), axis=0).tolist()
