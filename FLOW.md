# Execution Flow

A map of how the code actually executes: entry points, the order calls happen
in, and which module hands off to which. Updated in the same commit as any
change to an entry point or call chain.

> **Status**: the configuration layer (`config/settings.py`) and both current
> storage modules (`storage/sqlite_store.py` and `storage/chroma_store.py`) are
> implemented; `storage/neo4j_store.py` is next. The ingestion and query entry
> points below are documented as designed in `docs/` and are marked _(not yet
> implemented)_ until their modules exist. They are kept here so the
> intended shape stays visible while it is being built.

---

## Shared: configuration loading

Every entry point resolves configuration the same way, on first use:

1. `config/settings.py::get_settings()` — cached for the process lifetime
2. → `EnvSettings()` reads `config/.env` plus the process environment
3. → `load_config()` reads and validates `config/config.yaml`
4. Returns `Settings(env=..., config=...)`

`reload_settings()` clears the cache and re-reads both sources; it is the
mechanism `PUT /api/settings` uses to apply a provider change without a restart.

---

## Shared: SQLite storage (`storage/sqlite_store.py`)

Not an entry point itself — used by the ingestion and query entry points
below once they're implemented. Documented here because its call shape
(upsert-then-lookup, replace-not-append for chunks) matters to anything that
calls it.

1. `connect(db_path=None)` — opens a connection (default:
   `settings.env.sqlite_db_path`, or `":memory:"` for tests), enables
   `PRAGMA foreign_keys`, and runs the schema (idempotent — safe to call on
   every process start)
2. Ingesting an item:
   a. `insert_item(conn, item)` → upserts on `(source_type, source_ref_id)`
      and returns the *effective* item id — which may differ from
      `item.id` if this source ref was already ingested (see DECISIONS.md,
      2026-08-24)
   b. `replace_chunks(conn, effective_item_id, chunks)` → deletes any
      existing chunks for that item and inserts the new set in one
      transaction, keeping `chunks_fts` in sync via triggers
3. Querying: `get_item()` / `get_chunks_for_item()` for direct lookups,
   `keyword_search(conn, query, top_k)` for BM25-ranked full-text search —
   this is the keyword half of hybrid search that `agent/search_nodes.py`
   will call
4. Daily batch bookkeeping: `start_ingestion_run()` →
   `complete_ingestion_run(status=...)` → `get_last_run_timestamp()` reads the
   watermark for the next run's `since=`

---

## Shared: Chroma storage (`storage/chroma_store.py`)

Not an entry point itself — used alongside `sqlite_store.py` by the
ingestion and query entry points below. Embeddings are always computed by
the caller (`pipeline/embeddings.py`); this module only persists and
searches vectors already produced elsewhere.

1. `get_collection(persist_dir=None)` — opens the single `chunks` collection
   (default: `settings.env.chroma_persist_dir`), configured for cosine
   similarity with no embedding function attached (see DECISIONS.md,
   2026-08-24)
2. Ingesting a chunk: `upsert_chunks(collection, [vector_chunk, ...])` —
   `vector_chunk.id` must equal the corresponding
   `storage/sqlite_store.py::Chunk.embedding_id`, and `vector_chunk.item_id`
   must equal the SQLite item's effective id from `insert_item()`
3. Querying: `query(collection, query_embedding, top_k, where=...)` — the
   vector half of hybrid search that `agent/search_nodes.py` will call; an
   optional `where` filter narrows by `source_type`/`project_name`/`topic`,
   used both for filtered search and for relationship candidate narrowing in
   `pipeline/relationships.py`
4. Deleting an item: `delete_by_item(collection, item_id)` — SQLite's
   `delete_item()` cascades to `chunks` on its own, but callers must call
   this too since Chroma isn't a foreign-key participant

---

## Entry point: `scheduler/daily_batch.py` (`main()`) — _(not yet implemented)_

1. `main()` reads `last_run_timestamp` from `ingestion_runs`
   (`storage/sqlite_store.py`)
2. For each of the six extractors (`extractors/*.py`):
   a. `extract_new_items(since=last_run_timestamp)` → list of normalized items
   b. Each item → `pipeline/filters.py::apply_noise_filter()`
   c. Surviving items → `pipeline/chunking.py::chunk_text()`
   d. Each chunk → `pipeline/metadata.py::generate_metadata()` (rule-based
      fields direct; `project_name`/`topic` via `providers.generate_metadata()`)
   e. Each chunk → `pipeline/embeddings.py::embed()` → stored in Chroma
   f. Raw text + metadata → `storage/sqlite_store.py::insert_item()` /
      `insert_chunk()`

   **Branch point**: each extractor catches its own errors. A failing source is
   recorded in `ingestion_runs.error_log` and the remaining five continue.
3. After all sources are processed:
   `pipeline/relationships.py::detect_relationships()` runs per new item —
   queries Chroma for candidates, confirms via
   `providers.generate_relationship()`, writes edges via
   `storage/neo4j_store.py::write_edge()`
4. **Branch point**: only when every source succeeded (`status = success`) does
   `storage/sqlite_store.py::update_last_run_timestamp()` advance the watermark.
   A `partial_failure` leaves it unchanged so the failed source is retried
   tomorrow.

---

## Entry point: `POST /api/query` (`api/routes/query.py`) — _(not yet implemented)_

1. Request validated against `api/schemas.py`
2. `agent/graph.py::run(question, session_id)` invoked
3. `agent/router.py::route()` inspects the question and decides which of
   `VectorSearchNode` / `KeywordSearchNode` / `GraphTraversalNode` to invoke

   **Branch point**: this is the agent's main branch. Questions naming a
   specific identifier favour keyword search; open-ended questions favour vector
   search; questions about how things connect pull in graph traversal.
4. Selected nodes run (`agent/search_nodes.py`, `agent/graph_traversal.py`)
5. `agent/merger.py::merge()` — Reciprocal Rank Fusion across whichever nodes
   actually ran
6. `agent/synthesizer.py::synthesize()` — fetches full text for the top results
   from SQLite, calls `providers.generate_answer()`, returns a cited answer
7. Response serialized per `docs/API_Specification.docx` and returned

---

## Entry point: `api/main.py` startup — _(not yet implemented)_

1. FastAPI app created; `get_settings()` resolves configuration
2. Storage clients opened once and held for the process lifetime
   (`storage/*_store.py`)
3. Route modules from `api/routes/` registered
4. Agent graph compiled once at startup (`agent/graph.py`) rather than per
   request
