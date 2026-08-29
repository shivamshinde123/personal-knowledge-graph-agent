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
from storage.chroma_store import get_item_embeddings, query
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
    classified), deduplicates
    matches down to distinct candidate items, and asks the LLM to confirm or
    reject each one. A judgment confirmed with confidence below
    ``config.yaml``'s ``retrieval.relationship_confidence_threshold`` is
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
    source_text = chunks[0].text

    document_embedding = _mean_embedding(
        get_item_embeddings(collection, source_item_id)
    )
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
    confidence_threshold = (
        get_settings().config.retrieval.relationship_confidence_threshold
    )

    confirmed: list[tuple[str, RelationshipJudgment]] = []
    seen_item_ids: set[str] = set()
    for candidate in candidates:
        if candidate.item_id in seen_item_ids:
            continue
        seen_item_ids.add(candidate.item_id)

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


def _mean_embedding(vectors: list[list[float]]) -> list[float] | None:
    """Average a set of chunk embeddings into one whole-document vector.

    Args:
        vectors: An item's chunk embeddings, as returned by
            :func:`storage.chroma_store.get_item_embeddings`.

    Returns:
        The mean vector, or ``None`` if ``vectors`` is empty (the item has
        no chunks in Chroma yet).
    """
    if not vectors:
        return None
    return np.mean(np.array(vectors), axis=0).tolist()
