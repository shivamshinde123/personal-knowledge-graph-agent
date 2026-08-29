# Execution Flow

A map of how the code actually executes: entry points, the order calls happen
in, and which module hands off to which. Updated in the same commit as any
change to an entry point or call chain.

> **Status**: the configuration layer, all three storage backends, the
> provider layer, the entire pipeline layer (`filters`, `chunking`,
> `metadata`, `embeddings`, `relationships`), `scheduler/daily_batch.py`,
> and three extractors (local files, Notion, browser history) are
> implemented and merged to `main`. The agent layer is complete:
> `agent/router.py`, `agent/search_nodes.py`, `agent/graph_traversal.py`,
> `agent/merger.py`, `agent/synthesizer.py`, and the LangGraph wiring in
> `agent/graph.py` are all implemented (see below). `agent/graph.py::run()`
> does not yet implement multi-turn conversation memory — see DECISIONS.md,
> 2026-08-25. Remaining: the three extractors (Gmail, GitHub, Google
> Calendar) and the API/frontend layers — `POST /api/query` below is still
> documented as designed in `docs/` and marked _(not yet implemented)_
> until `api/` itself exists to call `agent/graph.py::run()`.

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
5. `get_item_embeddings(collection, item_id)` — fetches every chunk vector
   already stored for an item, for building a whole-document embedding by
   averaging rather than re-embedding text; used by
   `pipeline/relationships.py`'s candidate narrowing (see DECISIONS.md,
   2026-08-24)

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
- `extractors/notion.py` — lists every page the integration (`NOTION_API_KEY`)
  can see via `Client.search()` (paginated), filters by
  `last_edited_time > since`, then recursively walks each page's blocks via
  `Client.blocks.children.list()` (`_collect_block_text()`), converting each
  block's `rich_text` to plain text and joining blocks with blank lines so
  headings/paragraphs stay natural chunk boundaries. Title comes from the
  page's `title`-type property. Missing `NOTION_API_KEY`, or a failed
  `search()` call, raises `ExtractorError` (source-level); a single page's
  block-fetch failing is logged and skipped, not fatal — see DECISIONS.md,
  2026-08-24. Logs an INFO progress line every 25 pages scanned (a full
  scan visits every visible page and can take a long time on a large
  workspace — see DECISIONS.md, 2026-08-24).
- `extractors/browser_history.py` — copies `settings.env.browser_history_path`
  to a temp file before opening it (the browser holds the original locked
  while running — see DECISIONS.md, 2026-08-24), then reads Chrome's `urls`
  table (`url`, `title`, `visit_count`, `last_visit_time`), one
  `ExtractedItem` per URL. Filters, in order: no/blank title dropped;
  `visit_count < filters.browser_history.min_visit_count` dropped;
  `domain_blocklist` substring match dropped (see DECISIONS.md, 2026-08-24);
  `last_visit_time <= since` dropped. `raw_text` is the title only — no page
  content is fetched, per `docs/Data_Extraction_Specification.docx` section
  8.2. Missing/unreadable path raises `ExtractorError` (source-level).

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
  list[tuple[str, RelationshipJudgment]]` — returns `[]` immediately, before
  any Chroma query or LLM call, if the item's `source_type` is
  `browser_history` (`_NO_RELATIONSHIP_DETECTION_SOURCE`): that source is
  intentionally excluded from relationship detection entirely, per
  `CLAUDE.md`'s locked-in decisions — see DECISIONS.md, 2026-08-25.
  Otherwise, builds a whole-document query vector by averaging every chunk
  embedding the item already has in Chroma
  (`storage/chroma_store.py::get_item_embeddings()` →
  `_mean_embedding()`), not just the first chunk (see DECISIONS.md,
  2026-08-24, for why: a shared first-chunk boilerplate block would
  otherwise dominate similarity). Queries Chroma for that vector's nearest
  neighbors, excluding the item's own chunks and any `browser_history` item
  (`where: {"$and": [{"item_id": {"$ne": source_item_id}}, {"source_type":
  {"$ne": "browser_history"}}]}` — so browser history is never a candidate
  either, see DECISIONS.md, 2026-08-25) and narrowed to the same
  `project_name` when the item has one classified. Deduplicates matches
  down to distinct candidate items, then — using the item's first chunk as
  representative text for the LLM prompt itself, unchanged — asks
  `get_provider("relationship").generate_relationship()` to confirm or
  reject each one. The prompt is deliberately biased toward "unrelated"
  (most vector-narrowed candidates aren't actually related) and requires a
  specific, describable connection rather than shared topic/vocabulary — see
  DECISIONS.md, 2026-08-24. A confirmed judgment whose `confidence` is below
  `config.yaml`'s `retrieval.relationship_confidence_threshold` (default
  `0.6`) is discarded, same as an unconfirmed one (see DECISIONS.md,
  2026-08-24 — this is a partial fix: verified against real data to catch
  some but not all false positives, since a cheap model can still be
  confidently wrong on cases with literal shared boilerplate text). A
  confirmed-and-kept candidate is written via
  `storage/neo4j_store.py::write_relationship()`, using SQLite lookups
  (`get_item()`) to fill in `title`/`url` on both graph nodes — data
  Chroma's own metadata doesn't carry, despite `Component_Map.docx` not
  listing `SQLiteStore` as a dependency for this stage; see DECISIONS.md,
  2026-08-24. A candidate whose provider call or graph write fails is
  logged and skipped, not raised, same resilience pattern as the rest of
  the pipeline.

---

## Entry point: `scheduler/daily_batch.py` (`main()`)

Run via `uv run python scheduler/daily_batch.py`, registered with cron /
Task Scheduler on `config.yaml`'s `ingestion.schedule`. `main()` is a thin
wrapper: it opens the real, settings-derived SQLite connection, Chroma
collection, and Neo4j driver, then delegates to `_run(conn, collection,
driver)` — the actual orchestration, kept separate so tests can pass in
test doubles/temp resources instead.

1. `_run()` reads the watermark via `get_last_run_timestamp(conn)` and
   starts a run record via `start_ingestion_run(conn)`
2. For each registered extractor in `_EXTRACTORS` (currently
   `("local_file", local_files.extract_new_items)` and
   `("notion", notion.extract_new_items)` — adding a source means adding
   one entry here, per `docs/File_Folder_Structure.docx` section 4;
   `tests/test_scheduler/test_daily_batch.py`'s integration tests pin
   `_EXTRACTORS` to `local_file` only via an autouse fixture, so a new
   entry here never makes those tests real-network-dependent — see
   DECISIONS.md, 2026-08-24):
   a. `extract(since)` → list of `ExtractedItem`. An `ExtractorError` here
      is caught, logged, and recorded in `errors`; the loop moves on to the
      next source

      **Branch point**: an extractor failure doesn't stop the batch — the
      remaining sources still run.
   b. Surviving `apply_noise_filter()` items are collected for the *whole
      source* and passed to `generate_metadata()` **once**, not per item —
      this is what actually uses `config.yaml`'s
      `ingestion.batch_metadata_group_size` grouping (see DECISIONS.md,
      2026-08-24)
   c. Each `(item, metadata)` pair → `_process_item()`:
      - `storage/sqlite_store.py::insert_item()` — combines the item's
        extracted fields with its LLM metadata
      - `pipeline/chunking.py::chunk_text()` → `pipeline/embeddings.py::
        embed_chunks()` → vectors stored in Chroma, SQLite-ready `Chunk`s
        returned → `storage/sqlite_store.py::replace_chunks()`
      - if chunking/embedding/`replace_chunks()` raises, the just-inserted
        item row is deleted before the error propagates — no orphaned item
        with zero chunks (see DECISIONS.md, 2026-08-24)

      **Branch point**: a single item's processing failure is caught,
      logged, and recorded in `errors`; the rest of that source's items
      still get processed.
3. After every source is processed: for each successfully-processed item's
   id, `pipeline/relationships.py::detect_relationships()` runs (its own
   internal per-candidate resilience is backed by a top-level catch here
   too, recorded in `errors` on failure)
4. `status` is computed from `errors`/`items_processed`
   (`"success"`/`"partial_failure"`/`"failed"` — see DECISIONS.md,
   2026-08-24) and written via `complete_ingestion_run()`

   **Branch point**: only `status = "success"` advances the watermark
   (`storage/sqlite_store.py::get_last_run_timestamp()`'s own behavior).
   `"partial_failure"` and `"failed"` both leave it unchanged, so every
   source is retried on the next run — `"partial_failure"` just means some
   items got through this time too.

---

## Shared: query router (`agent/router.py`)

Not an entry point itself — will be called by `agent/graph.py::run()` once
it exists (see below). A pure, rule-based heuristic, not an LLM call — see
DECISIONS.md, 2026-08-25.

- `route(question) -> RouteDecision` — `RouteDecision` has three booleans:
  `vector_search` (`True` unless the question looks like an exact lookup —
  a quoted phrase, or a `"from"`/`"by"` sender/author filter),
  `keyword_search` (always `True`), and `graph_traversal` (`True` only when
  the question's phrasing implies relationships between items — a fixed
  list of phrases like "how does X relate to Y", "what led to X").

---

## Shared: search nodes (`agent/search_nodes.py`)

Not entry points themselves — will be called by `agent/graph.py::run()`
once it exists, for whichever nodes `agent/router.py::route()` selects.
Both return `list[SearchHit]` (`item_id`, 1-based `rank` within that
node's own results), deduplicated so multiple matching chunks of the same
item count as one hit.

- `vector_search(collection, question)` — embeds the question via
  `pipeline/embeddings.py::embed_query()`, then
  `storage/chroma_store.py::query()` for the nearest
  `config.yaml`'s `retrieval.top_k_vector` chunks.
- `keyword_search_node(conn, question)` — converts the question into a
  safe FTS5 `MATCH` expression (`_to_fts_query()`: strip stopwords,
  double-quote each remaining term, join with `OR` — see DECISIONS.md,
  2026-08-25) and calls `storage/sqlite_store.py::keyword_search()` for
  the top `config.yaml`'s `retrieval.top_k_keyword` chunks. A question with
  no significant terms returns `[]` without querying at all.

---

## Shared: graph traversal node (`agent/graph_traversal.py`)

Not an entry point itself — will be called by `agent/graph.py::run()` once
it exists, only when `agent/router.py::route()` sets `graph_traversal =
True`. Runs alongside, never instead of, the search nodes above — it has
no independent starting point into the graph (see DECISIONS.md,
2026-08-25).

- `graph_traversal(driver, seed_hits) -> list[SearchHit]` — for each seed
  hit (from vector/keyword search), fetches its one-hop neighbors via
  `storage/neo4j_store.py::get_related_items()`, excludes anything already
  in the seed set, and ranks the rest by how many distinct seeds connect to
  them (ties broken by the best seed rank that reached them). A seed whose
  lookup fails is logged and skipped, not fatal.

---

## Shared: result merger (`agent/merger.py`)

Not an entry point itself — will be called by `agent/graph.py::run()` once
it exists, on whichever nodes' hit lists actually ran. A pure function, no
external calls, per `Component_Map.docx`.

- `merge(*hit_lists) -> list[MergedResult]` — Reciprocal Rank Fusion:
  `score += 1 / (60 + rank)` for each list an item appears in (`k=60`, the
  standard constant — see DECISIONS.md, 2026-08-25), summed across lists,
  then sorted highest score first. An item appearing in more lists, or
  ranked higher within a list, scores higher. A node that didn't run simply
  contributes an empty list (or is omitted).

---

## Shared: answer synthesizer (`agent/synthesizer.py`)

Not an entry point itself — will be called by `agent/graph.py::run()` once
it exists, as the final step before returning a response. Calls
`ProviderInterface` and `SQLiteStore`, per `Component_Map.docx`.

- `synthesize(conn, question, merged_results, *, top_k=None) ->
  SynthesizedAnswer` — takes the top `top_k` (default `config.yaml`'s
  `retrieval.top_k_vector` — see DECISIONS.md, 2026-08-25) merged results,
  fetches each item's full chunk text via `get_item()`/
  `get_chunks_for_item()`, and builds one `ContextChunk` per item (multiple
  chunks joined into one text block). A merged result whose item no longer
  exists, or has no chunks, is skipped, not fatal. If no result yields
  usable context, returns a fixed "nothing found" answer without calling
  the LLM at all. Otherwise calls
  `get_provider("answer").generate_answer(question, context)` and returns
  the answer alongside an ordered `sources` list — index `i` matches the
  `[i + 1]` citation marker the provider is instructed to use.

---

## Entry point: `agent/graph.py::run(conn, collection, driver, question, session_id=None)`

Not yet reachable over HTTP — `api/` doesn't exist. Callable directly today
(see `tests/test_agent/test_graph.py`). Builds a fresh LangGraph
`StateGraph` per call (`_build_graph()`, closures over this call's
`conn`/`collection`/`driver` — see DECISIONS.md, 2026-08-25) and invokes it
as one fixed, linear sequence of nodes:

1. `router` — `agent/router.py::route(question)` → the `RouteDecision`
   stored in state for every downstream node to check

   **Branch point**: this is the agent's main branch. A quoted phrase or a
   "from"/"by" filter skips vector search; relationship-implying phrasing
   (e.g. "how does X relate to Y") enables graph traversal; keyword search
   always runs.
2. `vector_search` — `agent/search_nodes.py::vector_search()` if
   `decision.vector_search`, else contributes `[]`
3. `keyword_search` — `agent/search_nodes.py::keyword_search_node()` if
   `decision.keyword_search` (always, currently), else `[]`
4. `graph_traversal` — `agent/graph_traversal.py::graph_traversal()`,
   seeded from the combined vector + keyword hits, if
   `decision.graph_traversal`, else `[]`
5. `merge` — `agent/merger.py::merge()` — Reciprocal Rank Fusion across
   whichever of the three hit lists are non-empty
6. `synthesize` — `agent/synthesizer.py::synthesize()` — fetches full text
   for the top results from SQLite, calls
   `get_provider("answer").generate_answer()`, returns a cited answer
7. `run()` returns a `QueryResult` (`session_id`, `answer`, `sources`,
   `retrieval_methods_used`) — `session_id` is echoed back if given, else a
   fresh one is generated; no conversation memory yet (see DECISIONS.md,
   2026-08-25).

---

## Entry point: `POST /api/query` (`api/routes/query.py`) — _(not yet implemented)_

1. Request validated against `api/schemas.py`
2. `agent/graph.py::run()` invoked (see above for its own internal steps)
3. Response serialized per `docs/API_Specification.docx` and returned

---

## Entry point: `api/main.py` startup — _(not yet implemented)_

1. FastAPI app created; `get_settings()` resolves configuration
2. Storage clients opened once and held for the process lifetime
   (`storage/*_store.py`)
3. Route modules from `api/routes/` registered
4. Agent graph compiled once at startup (`agent/graph.py`) rather than per
   request
