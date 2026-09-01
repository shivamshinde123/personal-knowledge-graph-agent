"""Chroma storage: the single embedded vector collection.

This module owns the vector store defined in ``docs/Database_Schema.docx``.
It belongs to the storage layer: it imports only ``config.settings``, and per
``docs/Component_Map.docx`` and ``docs/Coding_Conventions.docx`` it is the
only module allowed to hold a live Chroma client. It does not itself cache
or reuse a client across calls — ``get_collection()`` opens a new one every
time it's called, so callers that want a single client for a process's
lifetime (as ``api/main.py`` startup does) must call it once and hold onto
the returned collection themselves.

Embeddings are computed elsewhere (``pipeline/embeddings.py``, via
``providers/base.py::get_provider("embedding")`` — always the configured
cloud model, see DECISIONS.md) and passed in already-computed; this module
never invokes an embedding model itself.

A Chroma collection is locked to whatever vector dimensionality its first
write used — a later write of a different size (e.g. after
``llm.cloud_embedding_model`` changes to a model with a different native
output width) fails outright, for every item, not just the changed ones.
``upsert_chunks()`` below detects this specific failure and raises a
``VectorStoreError`` naming the real fix (reset all data, then
re-ingest) rather than a generic "could not upsert" message — verified
directly against a real, systemic occurrence of this exact failure. See
``DECISIONS.md``.

Typical use::

    from storage.chroma_store import get_collection, upsert_chunks

    collection = get_collection()  # call once; hold onto the result
    upsert_chunks(collection, [vector_chunk])
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api.models.Collection import Collection

from config.settings import get_settings

COLLECTION_NAME = "chunks"


class VectorStoreError(Exception):
    """Raised when a Chroma storage operation fails."""


@dataclass(slots=True)
class VectorChunk:
    """A chunk's embedding and metadata, ready to write to Chroma.

    ``id`` matches ``chunks.embedding_id`` in SQLite; ``item_id`` matches
    ``items.id``, per the cross-store consistency rules in
    ``docs/Database_Schema.docx`` section 5.
    """

    id: str
    embedding: list[float]
    document: str
    item_id: str
    source_type: str
    project_name: str | None = None
    topic: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class VectorSearchResult:
    """A chunk returned by a similarity query, with its distance."""

    id: str
    document: str
    item_id: str
    source_type: str
    project_name: str | None
    topic: str | None
    created_at: datetime | None
    distance: float


def _to_metadata(chunk: VectorChunk) -> dict[str, Any]:
    """Build the Chroma metadata dict for a chunk, omitting unset fields.

    Chroma metadata values must be non-null, so optional fields are only
    included when present rather than written as ``None``.
    """
    metadata: dict[str, Any] = {
        "item_id": chunk.item_id,
        "source_type": chunk.source_type,
    }
    if chunk.project_name is not None:
        metadata["project_name"] = chunk.project_name
    if chunk.topic is not None:
        metadata["topic"] = chunk.topic
    if chunk.created_at is not None:
        metadata["created_at"] = chunk.created_at.isoformat()
    return metadata


def _to_search_result(
    id_: str, document: str, metadata: dict[str, Any], distance: float
) -> VectorSearchResult:
    created_at = metadata.get("created_at")
    return VectorSearchResult(
        id=id_,
        document=document,
        item_id=metadata["item_id"],
        source_type=metadata["source_type"],
        project_name=metadata.get("project_name"),
        topic=metadata.get("topic"),
        created_at=None if created_at is None else datetime.fromisoformat(created_at),
        distance=distance,
    )


def get_collection(persist_dir: Path | str | None = None) -> Collection:
    """Open the single embedded Chroma collection, creating it if needed.

    Configures cosine similarity, matching the normalized embeddings
    (Ollama or OpenAI-compatible, depending on ``provider_mode`` — see
    ``providers/``) ``pipeline/embeddings.py`` will write here. No
    embedding function is attached: every write in this module supplies
    its own precomputed vector, so Chroma is never asked to embed anything
    itself.

    Args:
        persist_dir: Directory Chroma persists to. Defaults to
            ``settings.env.chroma_persist_dir``.

    Returns:
        The ``chunks`` collection.

    Raises:
        VectorStoreError: If the client or collection cannot be opened.
    """
    target: Path | str = (
        get_settings().env.chroma_persist_dir if persist_dir is None else persist_dir
    )
    try:
        Path(target).mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(target))
        collection = client.get_or_create_collection(
            COLLECTION_NAME,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as exc:
        raise VectorStoreError(
            f"Could not open Chroma collection at {target!r}: {exc}"
        ) from exc
    return collection


def upsert_chunks(collection: Collection, chunks: Iterable[VectorChunk]) -> None:
    """Insert or update chunks in the collection.

    Args:
        collection: An open collection from :func:`get_collection`.
        chunks: The chunks to write. A chunk whose id already exists is
            overwritten in place.

    Raises:
        VectorStoreError: If the write fails. A dimension mismatch (the
            collection was created with a different-sized embedding model
            than the one currently configured) gets its own clearer
            message — see the module docstring.
    """
    chunks = list(chunks)
    if not chunks:
        return
    try:
        collection.upsert(
            ids=[c.id for c in chunks],
            embeddings=[c.embedding for c in chunks],
            documents=[c.document for c in chunks],
            metadatas=[_to_metadata(c) for c in chunks],
        )
    except Exception as exc:
        if "expecting embedding with dimension" in str(exc):
            raise VectorStoreError(
                f"Could not upsert {len(chunks)} chunk(s): the vector store "
                "was created with a different-sized embedding model than "
                "the one currently configured (llm.cloud_embedding_model). "
                "Use Settings > Danger Zone > 'Reset all data', then "
                f"re-run ingestion. Original error: {exc}"
            ) from exc
        raise VectorStoreError(
            f"Could not upsert {len(chunks)} chunk(s): {exc}"
        ) from exc


def delete_by_item(collection: Collection, item_id: str) -> None:
    """Delete every chunk belonging to an item.

    Chroma does not enforce foreign keys against SQLite, so per
    ``docs/Database_Schema.docx`` section 5, callers deleting an item from
    SQLite must call this explicitly to keep the two stores consistent.

    Args:
        collection: An open collection from :func:`get_collection`.
        item_id: The item whose chunks should be removed.

    Raises:
        VectorStoreError: If the delete fails.
    """
    try:
        collection.delete(where={"item_id": item_id})
    except Exception as exc:
        raise VectorStoreError(
            f"Could not delete chunks for item {item_id!r}: {exc}"
        ) from exc


def reset_all(persist_dir: Path | str | None = None) -> Collection:
    """Delete and recreate the collection, clearing any embedding-dimension lock.

    An earlier version of this function only deleted every document
    inside the collection (via ``collection.get()`` + ``collection.delete()``).
    That leaves the collection's underlying HNSW index — and the vector
    dimension it locked in on its first-ever write — untouched, so a
    later ingest at a different dimension kept failing with "expecting
    embedding with dimension of X, got Y" even right after a "reset".
    Verified directly: a real reset left the collection's on-disk index
    directory unchanged, and the very next ingestion run failed on its
    first item with that exact error. Deleting the whole collection and
    recreating it (matching how :func:`get_collection` creates it in the
    first place) clears the lock too. See ``DECISIONS.md``.

    Args:
        persist_dir: Directory Chroma persists to. Defaults to
            ``settings.env.chroma_persist_dir``.

    Returns:
        The freshly created, empty collection. Callers holding a
        long-lived reference to the old collection (``api/main.py``'s
        ``app.state.collection``) must replace it with this return
        value — the old object is no longer valid once its underlying
        collection has been deleted.

    Raises:
        VectorStoreError: If the delete or recreate fails.
    """
    target: Path | str = (
        get_settings().env.chroma_persist_dir if persist_dir is None else persist_dir
    )
    try:
        client = chromadb.PersistentClient(path=str(target))
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass  # nothing to delete yet -- get_or_create below still works
        return client.get_or_create_collection(
            COLLECTION_NAME,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as exc:
        raise VectorStoreError(f"Could not reset the vector store: {exc}") from exc


def get_item_embeddings(collection: Collection, item_id: str) -> list[list[float]]:
    """Fetch every stored embedding for an item's chunks.

    Used to build a whole-document embedding (e.g. by averaging) from
    already-computed chunk vectors, rather than re-embedding text — see
    ``pipeline/relationships.py``.

    Args:
        collection: An open collection from :func:`get_collection`.
        item_id: The item whose chunk embeddings to fetch.

    Returns:
        The item's chunk embeddings, in no particular order. Empty if the
        item has no chunks in the collection.

    Raises:
        VectorStoreError: If the fetch fails.
    """
    try:
        result = collection.get(where={"item_id": item_id}, include=["embeddings"])
    except Exception as exc:
        raise VectorStoreError(
            f"Could not fetch embeddings for item {item_id!r}: {exc}"
        ) from exc
    return [list(vector) for vector in result["embeddings"]]


@dataclass(slots=True)
class ItemChunkVector:
    """One chunk's text and embedding, for per-chunk similarity comparisons."""

    document: str
    embedding: list[float]


def get_item_chunk_vectors(
    collection: Collection, item_id: str
) -> list[ItemChunkVector]:
    """Fetch every stored chunk's text *and* embedding for an item.

    Unlike :func:`get_item_embeddings` (vectors only, for building a
    whole-document embedding by averaging), this keeps each chunk's text
    paired with its own vector — needed to pick out *which* chunk's text to
    use once a per-chunk similarity comparison identifies the closest one.
    See ``pipeline/relationships.py`` and ``DECISIONS.md``.

    Args:
        collection: An open collection from :func:`get_collection`.
        item_id: The item whose chunks to fetch.

    Returns:
        The item's chunks, in no particular order. Empty if the item has no
        chunks in the collection.

    Raises:
        VectorStoreError: If the fetch fails.
    """
    try:
        result = collection.get(
            where={"item_id": item_id}, include=["documents", "embeddings"]
        )
    except Exception as exc:
        raise VectorStoreError(
            f"Could not fetch chunk vectors for item {item_id!r}: {exc}"
        ) from exc
    return [
        ItemChunkVector(document=document, embedding=list(embedding))
        for document, embedding in zip(
            result["documents"], result["embeddings"], strict=True
        )
    ]


def query(
    collection: Collection,
    query_embedding: list[float],
    top_k: int = 8,
    where: dict[str, Any] | None = None,
) -> list[VectorSearchResult]:
    """Run a similarity search over chunk embeddings.

    Args:
        collection: An open collection from :func:`get_collection`.
        query_embedding: The query vector, produced by the same embedding
            model used to write the collection.
        top_k: Maximum number of results to return.
        where: An optional Chroma metadata filter (e.g.
            ``{"source_type": "notion"}`` or ``{"project_name": "pkg-agent"}``)
            for source-type or project-scoped search and relationship
            candidate narrowing.

    Returns:
        Matching chunks ordered by similarity (closest first).

    Raises:
        VectorStoreError: If the query fails.
    """
    try:
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        raise VectorStoreError(f"Vector search failed: {exc}") from exc
    ids = result["ids"][0]
    documents = result["documents"][0]
    metadatas = result["metadatas"][0]
    distances = result["distances"][0]
    return [
        _to_search_result(id_, document, metadata, distance)
        for id_, document, metadata, distance in zip(
            ids, documents, metadatas, distances, strict=True
        )
    ]
