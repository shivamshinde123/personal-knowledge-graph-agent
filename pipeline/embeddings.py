"""Embeddings: converts an item's chunk text into vectors and persists them.

Per ``docs/Component_Map.docx``, ``EmbeddingGenerator`` is the pipeline
stage that writes to Chroma directly (unlike ``pipeline/metadata.py``,
which stays storage-free) — the caller holds one Chroma collection open for
the whole ingestion run and passes it in here, rather than this module
reopening one per call (see ``storage/chroma_store.py``'s own docstring on
why it doesn't cache a client itself).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from functools import cache
from uuid import uuid4

from chromadb.api.models.Collection import Collection
from sentence_transformers import SentenceTransformer

from config.settings import get_settings
from storage.chroma_store import VectorChunk, upsert_chunks
from storage.sqlite_store import Chunk


@cache
def _model(name: str) -> SentenceTransformer:
    """Load (and cache) a sentence-transformers model by name."""
    return SentenceTransformer(name)


def embed_chunks(
    collection: Collection,
    item_id: str,
    source_type: str,
    chunk_texts: Sequence[str],
    *,
    project_name: str | None = None,
    topic: str | None = None,
    created_at: datetime | None = None,
) -> list[Chunk]:
    """Embed an item's chunks and persist the vectors to Chroma.

    Args:
        collection: An open Chroma collection from
            :func:`storage.chroma_store.get_collection`, held by the caller
            for the whole ingestion run.
        item_id: The chunks' owning item's effective SQLite id.
        source_type: The item's source type, written as Chroma metadata for
            source-filtered search.
        chunk_texts: The item's chunk texts, in order, from
            :func:`pipeline.chunking.chunk_text`.
        project_name: LLM-derived project name, if classified.
        topic: LLM-derived topic, if classified.
        created_at: The item's creation time, for time-based filtering.

    Returns:
        One SQLite-ready :class:`Chunk` per input text, in the same order,
        each carrying a freshly generated ``embedding_id`` pointing at the
        Chroma vector just written. Callers persist these via
        :func:`storage.sqlite_store.replace_chunks`.
    """
    if not chunk_texts:
        return []
    model = _model(get_settings().config.embedding.model)
    vectors = model.encode(list(chunk_texts), convert_to_numpy=True)

    sqlite_chunks: list[Chunk] = []
    vector_chunks: list[VectorChunk] = []
    for index, (text, vector) in enumerate(zip(chunk_texts, vectors, strict=True)):
        embedding_id = str(uuid4())
        sqlite_chunks.append(
            Chunk(
                id=str(uuid4()),
                item_id=item_id,
                chunk_index=index,
                text=text,
                embedding_id=embedding_id,
                token_count=len(text.split()),
            )
        )
        vector_chunks.append(
            VectorChunk(
                id=embedding_id,
                embedding=vector.tolist(),
                document=text,
                item_id=item_id,
                source_type=source_type,
                project_name=project_name,
                topic=topic,
                created_at=created_at,
            )
        )

    upsert_chunks(collection, vector_chunks)
    return sqlite_chunks


def embed_query(text: str) -> list[float]:
    """Embed a single piece of text for a similarity search.

    Used for query-time vector search and for
    :func:`pipeline.relationships.detect_relationships`'s candidate search,
    so both share this module's cached model loading instead of loading
    their own copy.

    Args:
        text: The text to embed.

    Returns:
        The embedding vector.
    """
    model = _model(get_settings().config.embedding.model)
    return model.encode([text], convert_to_numpy=True)[0].tolist()
