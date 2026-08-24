# Execution Flow

A map of how the code actually executes: entry points, the order calls happen
in, and which module hands off to which. Updated in the same commit as any
change to an entry point or call chain.

> **Status**: the configuration layer (`config/settings.py`), all three
> storage backends (`storage/sqlite_store.py`, `storage/chroma_store.py`,
> `storage/neo4j_store.py`), the provider layer (`providers/`), and the
> local files extractor (`extractors/local_files.py`) are implemented and
> merged to `main`, along with `pipeline/filters.py`, `pipeline/chunking.py`,
> `pipeline/metadata.py`, and `pipeline/embeddings.py`.
> `pipeline/relationships.py` is added on this branch, completing the
> pipeline layer; the other five extractors, `scheduler/daily_batch.py`
> itself, and the agent/API/frontend layers are next. The ingestion and
> query entry points below are documented as designed in `docs/` and are
> marked _(not yet implemented)_ until their modules exist. They are kept
> here so the intended shape stays visible while it is being built.

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

## Shared: Neo4j storage (`storage/neo4j_store.py`)

Not an entry point itself — used by `pipeline/relationships.py` once it
exists. An item only gets a graph node the first time it has a confirmed
relationship (`docs/Database_Schema.docx` section 5), so there is no
standalone "create item node" call — writing the first relationship creates
both endpoint nodes.

1. `get_driver(uri=None, user=None, password=None)` — opens a driver
   (default: `settings.env.neo4j_uri`/`neo4j_user`/`neo4j_password`) and
   calls `verify_connectivity()` up front, so a misconfigured or unreachable
   server fails at startup rather than on the first query
2. `ensure_constraints(driver)` — creates the `item_id` uniqueness
   constraint and `project_name` index (idempotent, safe on every process
   start)
3. Relationship detection: `pipeline/relationships.py::detect_relationships()`
   calls `write_relationship(driver, source, target, relationship)` per
   confirmed candidate — `MERGE`s
   both `Item` nodes (upserting their properties) and `MERGE`s the
   `RELATES_TO` edge keyed on `(source, target, label)`; re-detecting the
   same labeled relationship updates `confidence` rather than duplicating
   the edge
4. Querying: `get_item()` for a single node (returns `None` for an item with
   no confirmed relationship yet), `get_related_items(driver, item_id)` for
   one-hop neighbors in both directions — this is what
   `agent/graph_traversal.py` will call
5. Deleting an item: `delete_item(driver, item_id)` — mirrors SQLite's
   cascade-on-delete; callers must call this explicitly alongside
   `storage/chroma_store.py::delete_by_item()` since neither store enforces
   foreign keys against SQLite

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
the caller (`pipeline/embeddings.py::embed_chunks()`, see below); this
module only persists and searches vectors already produced elsewhere.

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

## Shared: LLM providers (`providers/`)

Not an entry point itself — every LLM call anywhere in the system goes
through this layer (`CLAUDE.md`'s "never bypass the LLM Provider
abstraction" ground rule). Callers never import `local_provider.py` or
`openrouter_provider.py` directly, or a LangChain/Ollama/OpenAI SDK type —
only `providers/base.py`'s `get_provider()` and its return type,
`ProviderInterface`.

1. `get_provider(task)` — `task` is `"metadata"`, `"relationship"`, or
   `"answer"`; reads `settings.config.llm.provider_mode` and returns
   `create_local_provider()` (Ollama), `create_openrouter_provider()`
   (OpenRouter), or, under `mixed`, routes `"answer"` to OpenRouter and the
   two cheaper ingestion tasks to Ollama (see DECISIONS.md, 2026-08-24)
2. Both concrete providers construct a `LangChainProvider` around a
   LangChain chat model (`ChatOllama` / `ChatOpenAI`) — this is where every
   provider call's prompt building, JSON response parsing, and
   retry-with-backoff actually live, shared by both
3. `provider.generate_metadata(texts)` — called by
   `pipeline/metadata.py::generate_metadata()` for the `project_name`/`topic`
   fields (batched: one call per group of `config.yaml`'s
   `batch_metadata_group_size`, see below)
4. `provider.generate_relationship(source_text, candidate_text)` — called by
   `pipeline/relationships.py::detect_relationships()` to confirm or reject
   each vector-narrowed candidate; returns `None` (not related) or a
   judgment with a `label`/`confidence` that maps onto
   `storage/neo4j_store.py::Relationship`. `label` is constrained to a
   fixed vocabulary (`base._RELATIONSHIP_LABELS`:
   `implements`/`discussed_in`/`planned_in`/`companion_to`) — a response
   outside that set is treated as malformed and retried, same as invalid
   JSON — see DECISIONS.md, 2026-08-24, for why free-form labels were
   dropped after a real ingestion run showed them fragmenting into 16
   near-duplicate variants
5. `provider.generate_answer(question, context)` — what
   `agent/synthesizer.py::synthesize()` will call; the response cites
   `context` positionally (`[1]`, `[2]`, …), which the synthesizer resolves
   against each `ContextChunk`'s `title`/`url` before returning the final
   cited answer

---

## Shared: extractors (`extractors/`)

Not an entry point itself — invoked by `scheduler/daily_batch.py::main()`
below, once per source. Every extractor exposes one function with the same
signature and depends on nothing but `config.settings` and its own
`extractors/base.py` (never `storage/` or `agent/` — see
`docs/Component_Map.docx`); the daily batch, not the extractor, is what
calls `pipeline/filters.py` next.

- `extract_new_items(since) -> list[ExtractedItem]` — `since=None` (first
  run) returns everything; otherwise only items changed after `since`. A
  per-item failure (unreadable file, malformed record) is logged and
  skipped, never raised — see DECISIONS.md, 2026-08-24. A source-level
  failure (the whole source unreachable) raises `ExtractorError`, which
  `daily_batch.py` catches per source.
- `extractors/local_files.py` — walks `settings.env.watch_dirs`
  recursively; extracts `.txt`/`.md` directly, `.pdf` via `pypdf`, `.docx`
  via `python-docx`; filters by `st_mtime > since`. A missing watch
  directory is logged and skipped, not fatal — other configured
  directories still get scanned.

---

## Shared: pipeline filtering and chunking (`pipeline/filters.py`, `pipeline/chunking.py`)

Not entry points themselves — called by `scheduler/daily_batch.py::main()`
below, once per item, between extraction and metadata/embedding.

- `apply_noise_filter(item) -> bool` — cross-source only: drops an item if
  its extracted text is empty or shorter than
  `config.yaml`'s `filters.min_content_length`. Source-specific rules
  (browser history's domain blocklist/visit count, Gmail's excluded labels)
  are applied inside those extractors instead, before normalization — see
  DECISIONS.md, 2026-08-24, since `ExtractedItem` has no `visit_count` or
  `labels` field for a generic filter to inspect.
- `chunk_text(text) -> list[str]` — greedily packs paragraphs (split on
  blank lines) up to `chunking.target_chunk_size_tokens`; a paragraph
  already over the target on its own (no natural boundary to prefer) falls
  back to a fixed sliding window with `chunking.chunk_overlap_tokens` of
  overlap between windows. Token count is an approximate word count, not a
  real tokenizer — see DECISIONS.md, 2026-08-24, for why (keeping chunking
  fully offline under `provider_mode: fully_local`).

---

## Shared: metadata generation (`pipeline/metadata.py`)

Not an entry point itself. Operates at the *item* level, not the chunk
level — `project_name`/`topic` are columns on `items`, not `chunks`
(`docs/Database_Schema.docx`) — grouped into
`config.yaml`'s `ingestion.batch_metadata_group_size`-sized LLM calls rather
than one call per item.

- `generate_metadata(items) -> list[ItemMetadata]` — a pure function: no
  SQLite/Chroma calls of its own (see DECISIONS.md, 2026-08-24, for why that
  boundary was chosen over having this module write items itself). Each
  item's text is truncated before being sent, to keep a batched prompt a
  reasonable size. A group whose LLM call exhausts its retries
  (`ProviderError`) degrades to `ItemMetadata(None, None)` for just that
  group rather than raising and losing every other group's results.

---

## Shared: embeddings (`pipeline/embeddings.py`)

Not an entry point itself. Unlike `pipeline/metadata.py`, this stage writes
to Chroma directly rather than staying storage-free — see DECISIONS.md,
2026-08-24, for why the two pipeline stages made opposite choices there.

- `embed_chunks(collection, item_id, source_type, chunk_texts, *,
  project_name=None, topic=None, created_at=None) -> list[Chunk]` — encodes
  every chunk with the configured `sentence-transformers` model (loaded
  once, cached by model name), calls
  `storage/chroma_store.py::upsert_chunks()` itself, and returns
  SQLite-ready `Chunk` objects (each with a freshly generated
  `embedding_id` pointing at the vector just written) for the caller to
  persist via `storage/sqlite_store.py::replace_chunks()`. Takes an
  already-open `collection` rather than opening its own, since
  `get_collection()` opens a new client on every call by design — the
  caller opens one collection and reuses it across the whole ingestion run.

---

## Shared: relationship detection (`pipeline/relationships.py`)

Not an entry point itself. The last pipeline stage, run after an item's
metadata/chunks/embeddings are already persisted.

- `detect_relationships(conn, driver, collection, source_item_id) ->
  list[tuple[str, RelationshipJudgment]]` — takes the item's first chunk
  (by `chunk_index`) as representative text, embeds it
  (`pipeline/embeddings.py::embed_query()`), and queries Chroma for its
  nearest neighbors, excluding the item's own chunks
  (`where: {"item_id": {"$ne": source_item_id}}`) and narrowed to the same
  `project_name` when the item has one classified. Deduplicates matches
  down to distinct candidate items, then asks
  `get_provider("relationship").generate_relationship()` to confirm or
  reject each one. A confirmed candidate is written via
  `storage/neo4j_store.py::write_relationship()`, using SQLite lookups
  (`get_item()`) to fill in `title`/`url` on both graph nodes — data
  Chroma's own metadata doesn't carry, despite `Component_Map.docx` not
  listing `SQLiteStore` as a dependency for this stage; see DECISIONS.md,
  2026-08-24. A candidate whose provider call or graph write fails is
  logged and skipped, not raised, same resilience pattern as the rest of
  the pipeline.

---

## Entry point: `scheduler/daily_batch.py` (`main()`) — _(not yet implemented)_

1. `main()` reads `last_run_timestamp` from `ingestion_runs`
   (`storage/sqlite_store.py`)
2. For each of the six extractors (`extractors/*.py`):
   a. `extract_new_items(since=last_run_timestamp)` → list of normalized items
   b. Each item → `pipeline/filters.py::apply_noise_filter()`
   c. Surviving items → `pipeline/metadata.py::generate_metadata()` →
      `project_name`/`topic`, combined with the item's other fields and
      written via `storage/sqlite_store.py::insert_item()`
   d. Each item's text → `pipeline/chunking.py::chunk_text()`
   e. Chunks → `pipeline/embeddings.py::embed_chunks()` → vectors stored in
      Chroma, SQLite-ready `Chunk` objects returned
   f. Those `Chunk` objects → `storage/sqlite_store.py::replace_chunks()`

   **Branch point**: each extractor catches its own errors. A failing source is
   recorded in `ingestion_runs.error_log` and the remaining five continue.
3. After all sources are processed:
   `pipeline/relationships.py::detect_relationships()` runs per new item —
   queries Chroma for candidates, confirms via
   `get_provider("relationship").generate_relationship()`, writes edges via
   `storage/neo4j_store.py::write_relationship()`
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
   from SQLite, calls `get_provider("answer").generate_answer()`, returns a
   cited answer
7. Response serialized per `docs/API_Specification.docx` and returned

---

## Entry point: `api/main.py` startup — _(not yet implemented)_

1. FastAPI app created; `get_settings()` resolves configuration
2. Storage clients opened once and held for the process lifetime
   (`storage/*_store.py`)
3. Route modules from `api/routes/` registered
4. Agent graph compiled once at startup (`agent/graph.py`) rather than per
   request
