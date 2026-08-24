# Execution Flow

A map of how the code actually executes: entry points, the order calls happen
in, and which module hands off to which. Updated in the same commit as any
change to an entry point or call chain.

> **Status**: the configuration layer (`config/settings.py`), the SQLite and
> Neo4j storage modules (`storage/sqlite_store.py`, `storage/neo4j_store.py`),
> and the provider layer (`providers/`) are implemented. `chroma_store.py` is
> implemented on a separate, not-yet-merged branch (PR #3). The ingestion and
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
3. Relationship detection: for each candidate pair,
   `write_relationship(driver, source, target, relationship)` — `MERGE`s
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
3. `provider.generate_metadata(texts)` — what
   `pipeline/metadata.py::generate_metadata()` will call for the
   `project_name`/`topic` fields (batched: one call per group of
   `config.yaml`'s `batch_metadata_group_size`)
4. `provider.generate_relationship(source_text, candidate_text)` — what
   `pipeline/relationships.py::detect_relationships()` will call to confirm
   or reject each vector-narrowed candidate pair; returns `None` (not
   related) or a judgment with a `label`/`confidence` that maps onto
   `storage/neo4j_store.py::Relationship`
5. `provider.generate_answer(question, context)` — what
   `agent/synthesizer.py::synthesize()` will call; the response cites
   `context` positionally (`[1]`, `[2]`, …), which the synthesizer resolves
   against each `ContextChunk`'s `title`/`url` before returning the final
   cited answer

---

## Entry point: `scheduler/daily_batch.py` (`main()`) — _(not yet implemented)_

1. `main()` reads `last_run_timestamp` from `ingestion_runs`
   (`storage/sqlite_store.py`)
2. For each of the six extractors (`extractors/*.py`):
   a. `extract_new_items(since=last_run_timestamp)` → list of normalized items
   b. Each item → `pipeline/filters.py::apply_noise_filter()`
   c. Surviving items → `pipeline/chunking.py::chunk_text()`
   d. Each chunk → `pipeline/metadata.py::generate_metadata()` (rule-based
      fields direct; `project_name`/`topic` via
      `get_provider("metadata").generate_metadata()`)
   e. Each chunk → `pipeline/embeddings.py::embed()` → stored in Chroma
   f. Raw text + metadata → `storage/sqlite_store.py::insert_item()` /
      `insert_chunk()`

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
