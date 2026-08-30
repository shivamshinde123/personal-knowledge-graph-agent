# Execution Flow

A map of how the code actually executes: entry points, the order calls happen
in, and which module hands off to which. Updated in the same commit as any
change to an entry point or call chain.

> **Status**: the configuration layer, all three storage backends, the
> provider layer, the entire pipeline layer (`filters`, `chunking`,
> `metadata`, `embeddings`, `relationships`), `scheduler/daily_batch.py`,
> and four extractors (local files, Notion, GitHub, browser history) are
> implemented and merged to `main`. The agent layer is complete:
> `agent/router.py`, `agent/search_nodes.py`, `agent/graph_traversal.py`,
> `agent/merger.py`, `agent/synthesizer.py`, and the LangGraph wiring in
> `agent/graph.py` (including the `condense_query` follow-up node) are all
> implemented (see below). Conversation memory is end-to-end complete,
> frontend included: session/message storage, history support in the
> provider layer, `agent/graph.py::run()` loading/persisting turns, and
> the sidebar listing/reopening real sessions are all implemented and
> verified against real, previously-recorded conversation data through the
> real API — including the follow-up-retrieval gap noted in earlier
> revisions of this document, since fixed (see DECISIONS.md, 2026-08-29).
> The API layer is complete: `api/main.py`, `GET /api/health`,
> `POST /api/query`, `GET /api/sources/status`, `GET`/`PUT /api/settings`,
> `GET /api/sessions`/`GET /api/sessions/{id}`, and
> `POST /api/ingest/trigger` are all implemented (see below), with six
> error-response handlers registered for every route. Relationship
> detection has had several real-data-verified quality passes since first
> built: bidirectional-duplicate prevention, per-candidate source chunk
> selection, a narrowing distance cutoff, boilerplate-neutralization in
> the confirmation prompt, and stale-relationship clearing on item edits
> (see DECISIONS.md, all 2026-08-29). The evaluation layer
> (`eval/test_questions.json`, `eval/evaluators.py`,
> `eval/run_evaluation.py`, `agent/tracing.py`) is implemented and
> verified against the real LangSmith account (see below). Remaining: two
> extractors not yet built (Gmail, Google Calendar).

---

## Shared: configuration loading

Every entry point resolves configuration the same way, on first use:

1. `config/settings.py::get_settings()` — cached for the process lifetime
2. → `EnvSettings()` reads `config/.env` plus the process environment
3. → `load_config()` reads and validates `config/config.yaml`
4. Returns `Settings(env=..., config=...)`

`reload_settings()` clears the cache and re-reads both sources.

`update_llm_config(*, provider_mode=None, local_model=None, cloud_model=None,
path=DEFAULT_CONFIG_PATH)` — used by `PUT /api/settings` (see below) to
apply a provider change without a restart. Loads `config.yaml` with
`ruamel.yaml`'s round-trip mode (not plain PyYAML — see DECISIONS.md,
2026-08-25, for why: comments/quotes/list formatting must survive a write),
updates only the given `llm` fields, validates via
`LLMConfig.model_validate()` *before* writing anything to disk, then writes
back and returns the freshly re-read config. Only invalidates the
process-wide `get_settings()` cache when `path` is the real default file —
a caller using a different path (tests) gets that file's own config back
without touching the global cache.

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

Not an entry point itself — used by the ingestion, agent, and API entry
points. Documented here because its call shape (upsert-then-lookup,
replace-not-append for chunks) matters to anything that calls it.

1. `connect(db_path=None)` — opens a connection (default:
   `settings.env.sqlite_db_path`, or `":memory:"` for tests) with
   `check_same_thread=False` (needed since `api/main.py` shares one
   connection across FastAPI's threadpool — see DECISIONS.md, 2026-08-25),
   enables `PRAGMA foreign_keys`, and runs the schema (idempotent — safe to
   call on every process start)
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
   calls
4. Daily batch bookkeeping: `start_ingestion_run()` →
   `complete_ingestion_run(status=...)` → `get_last_run_timestamp()` reads
   the watermark for the next run's `since=`; `get_last_ingestion_run()`
   returns the most recent run regardless of status (for display, e.g.
   `agent/sources_status.py` — see DECISIONS.md, 2026-08-25)
5. Conversation memory (tables not in `docs/Database_Schema.docx` — see
   DECISIONS.md, 2026-08-25): `record_conversation_turn(conn, session_id,
   question, answer, sources)` is the one write path — upserts the session
   (auto-generated title from the question on first turn, `updated_at`
   refreshed on later turns) and inserts both the user and agent messages.
   `list_sessions()` (most recently active first), `get_session()`, and
   `get_messages_for_session()` (oldest first) are the read side, for
   `GET /api/sessions`/`GET /api/sessions/{id}` and for loading prior
   turns before a follow-up question.

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

1. `get_provider(task)` — `task` is `"metadata"`, `"relationship"`,
   `"condense"`, `"answer"`, or `"eval"`; reads
   `settings.config.llm.provider_mode` and returns `create_local_provider()`
   (Ollama), `create_openrouter_provider()` (OpenRouter), or, under `mixed`,
   routes `"answer"` and `"eval"` to OpenRouter (judging answer quality
   deserves the same caliber of model as generating them, and eval runs
   are infrequent — only `eval/run_evaluation.py`, never the query path)
   and the three cheaper, higher-frequency tasks (`"metadata"`,
   `"relationship"`, `"condense"`) to Ollama (see DECISIONS.md, 2026-08-24
   and 2026-08-29)
2. Both concrete providers construct a `LangChainProvider` around a
   LangChain chat model (`ChatOllama` / `ChatOpenAI`) — this is where every
   provider call's prompt building, JSON response parsing, and
   retry-with-backoff actually live, shared by both. The `ChatOpenAI`
   (OpenRouter) side always passes an explicit `max_tokens`
   (`llm.cloud_max_tokens`, default `4096`) — left unset it would default
   to the routed model's own maximum (64000 for `claude-sonnet-4`), which
   can exceed the account's remaining credit balance and fail the call
   outright; verified directly — see DECISIONS.md, 2026-08-29
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
   near-duplicate variants. The prompt instructs the model to name any
   verbatim-shared boilerplate before judging and to not treat it as
   relationship evidence on its own; parsing accordingly tolerates a
   reasoning preamble before the JSON (`base._extract_json_object()`
   scans for the last brace-balanced object containing a `related` key)
   rather than requiring the whole response to be pure JSON — see
   DECISIONS.md, 2026-08-29
5. `provider.generate_answer(question, context, history=())` — called by
   `agent/synthesizer.py::synthesize()`; the response cites `context`
   positionally (`[1]`, `[2]`, …), which the synthesizer resolves against
   each `ContextChunk`'s `title`/`url` before returning the final cited
   answer. `history` (`ConversationTurn` list, oldest first) is rendered as
   its own labeled prompt section, explicitly *not* citable — only for
   resolving follow-up references like "the second one" (see DECISIONS.md,
   2026-08-25)
6. `provider.generate_search_query(question, history)` — called by
   `agent/graph.py`'s `condense_query` node, only when `history` is
   non-empty, to rewrite a follow-up into a standalone retrieval query
   (see DECISIONS.md, 2026-08-29)
7. `provider.generate_eval_judgment(criterion, question, answer, context)`
   — called only by `eval/evaluators.py`'s `evaluate_faithfulness()` /
   `evaluate_relevance()`, never on the query path. `criterion` is
   `"faithfulness"` (is everything in `answer` supported by `context`) or
   `"relevance"` (does `answer` address `question`); returns an
   `EvalJudgment(score, reasoning)`. Same preamble-tolerant parsing as
   `generate_relationship()` (see DECISIONS.md, 2026-08-29)

---

## Shared: tracing (`agent/tracing.py`)

Not an entry point itself. `enable_tracing()` sets the
`LANGSMITH_TRACING`/`LANGSMITH_API_KEY`/`LANGSMITH_PROJECT` environment
variables LangChain/LangGraph's own auto-instrumentation reads — no
`langsmith` import anywhere in `agent/graph.py`, and nothing to change
there for a traced run to show up in the configured LangSmith project.
Idempotent (safe to call more than once); a no-op returning `False` if no
`LANGSMITH_API_KEY` is configured. Deliberately **not** called from
`agent/graph.py::run()` — called explicitly, once, only by the two real
entry points that should be traced: `api/main.py`'s startup lifespan (see
below — tests substitute a no-op lifespan, so this never runs during a
test) and `eval/run_evaluation.py::main()`. See DECISIONS.md, 2026-08-29.

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
  can see via `Client.search()` (paginated), unless `NOTION_PAGE_IDS` is
  configured, in which case only those pages are fetched directly by id
  (`Client.pages.retrieve()`) instead — see DECISIONS.md, 2026-08-30.
  Filters by `last_edited_time > since`, then recursively walks each
  page's blocks via
  `Client.blocks.children.list()` (`_collect_block_text()`), converting each
  block's `rich_text` to plain text and joining blocks with blank lines so
  headings/paragraphs stay natural chunk boundaries. Title comes from the
  page's `title`-type property. Missing `NOTION_API_KEY`, or a failed
  `search()` call, raises `ExtractorError` (source-level); a single page's
  block-fetch failing is logged and skipped, not fatal — see DECISIONS.md,
  2026-08-24. Logs an INFO progress line every 25 pages scanned (a full
  scan visits every visible page and can take a long time on a large
  workspace — see DECISIONS.md, 2026-08-24).
- `extractors/github.py` — lists every repository `GITHUB_TOKEN` can access
  via `GET /user/repos` (paginated, `owner,collaborator,organization_member`
  affiliation), unless `GITHUB_REPOS` is configured, in which case only
  those `owner/repo` full names are processed instead — same convention as
  Notion's `NOTION_PAGE_IDS` — see DECISIONS.md, 2026-08-30. Per repo,
  independently: the README (its own `since` check via the most recent
  commit touching that path, not re-extracted every run — prefixed with
  the repo's description/topics), commits (`GET .../commits?since=...`,
  each with its changed file names via one extra per-commit API call, not
  raw diffs or an LLM summary), PRs (`GET .../pulls`, no native `since`
  filter — paginated newest-created-first and stopped once a page's PR
  predates `since`, with each PR's review comments folded into its text),
  and issues (`GET .../issues?since=...`, excluding entries with a
  `pull_request` key — that endpoint also returns PRs). Also extracts every
  starred repo once per run (`GET /user/starred`, `since`-filtered by
  `starred_at`) as a lightweight name/description/topics item, independent
  of the repo scope above. A category (README/commits/PRs/issues) failing
  for one repo, or a whole repo failing, is logged and skipped — the rest
  of the run continues. Missing `GITHUB_TOKEN`, or listing accessible
  repos failing outright, raises `ExtractorError` (source-level).
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
  (`storage/chroma_store.py::get_item_chunk_vectors()`, keeping each
  chunk's text alongside its embedding, → `_mean_embedding()` on the
  embeddings), not just the first chunk (see DECISIONS.md, 2026-08-24, for
  why: a shared first-chunk boilerplate block would otherwise dominate
  similarity). Queries Chroma for that vector's nearest neighbors,
  excluding the item's own chunks and any `browser_history` item (`where:
  {"$and": [{"item_id": {"$ne": source_item_id}}, {"source_type": {"$ne":
  "browser_history"}}]}` — so browser history is never a candidate either,
  see DECISIONS.md, 2026-08-25) and narrowed to the same `project_name`
  when the item has one classified. Deduplicates matches down to distinct
  candidate items, then — for each one — first checks the candidate's
  Chroma-returned `distance` against `config.yaml`'s
  `retrieval.relationship_candidate_max_distance` (default `0.6`, cosine
  distance; `null` disables the check) and skips it entirely, before any
  Neo4j or LLM call, if it's farther than that — narrowing always returns
  its top-K nearest neighbors regardless of how weak the match is, so this
  catches the "least-unrelated item available" case (see DECISIONS.md,
  2026-08-29). Then calls `storage/neo4j_store.py::has_any_relationship(
  driver, source_item_id, candidate.item_id)`, an undirected Cypher match
  that catches an existing edge in either direction regardless of label,
  and skips the candidate (no LLM call, no graph write) if one already
  exists — detection runs per newly-processed item, so without this check
  the same real-world relationship could get judged and written twice,
  once from each endpoint's own processing run (see DECISIONS.md,
  2026-08-29). Otherwise, picks representative text for the LLM prompt:
  the candidate side uses `candidate.document` (Chroma's own search result
  — already the candidate's actual matching chunk, since narrowing queried
  with the source's pooled embedding); the source side computes the
  candidate's own whole-document embedding and picks whichever of the
  source's chunks is closest to it by cosine similarity
  (`_most_similar_chunk_text()`) — a different chunk per candidate, not a
  fixed one (see DECISIONS.md, 2026-08-29). Calls
  `get_provider("relationship").generate_relationship()` to confirm or
  reject. The prompt is deliberately biased toward "unrelated" (most
  vector-narrowed candidates aren't actually related), requires a
  specific, describable connection rather than shared topic/vocabulary,
  and instructs the model to name and discount any text shared verbatim
  between the two candidates before judging, rather than treating shared
  boilerplate as relationship evidence — verified against the real
  configured default model (`claude-sonnet-4`) to fix a real false
  positive this way; parsing tolerates a reasoning preamble the model
  reliably produces as a result (`base._extract_json_object()`) — see
  DECISIONS.md, 2026-08-24 and 2026-08-29. A confirmed judgment whose
  `confidence` is below `retrieval.relationship_confidence_threshold`
  (default `0.6`) is discarded, same as an unconfirmed one (see
  DECISIONS.md, 2026-08-24). A confirmed-and-kept candidate is written via
  `storage/neo4j_store.py::write_relationship()`, using SQLite lookups
  (`get_item()`) to fill in `title`/`url` on both graph nodes — data
  Chroma's own metadata doesn't carry, despite `Component_Map.docx` not
  listing `SQLiteStore` as a dependency for this stage; see DECISIONS.md,
  2026-08-24. A candidate whose provider call or graph write fails is
  logged and skipped, not raised, same resilience pattern as the rest of
  the pipeline.

---

## Entry point: `scheduler/daily_batch.py` (`main()`)

Run via `uv run python -m scheduler.daily_batch` (module invocation — a
raw script path fails with `ModuleNotFoundError`, see DECISIONS.md,
2026-08-29), registered with cron / Task Scheduler on `config.yaml`'s
`ingestion.schedule`. `main()` is a thin wrapper: it opens the real,
settings-derived SQLite connection, Chroma
collection, and Neo4j driver, then delegates to `_run(conn, collection,
driver)` — the actual orchestration, kept separate so tests can pass in
test doubles/temp resources instead.

1. `_run()` reads the watermark via `get_last_run_timestamp(conn)` and
   starts a run record via `start_ingestion_run(conn)`
2. For each registered extractor in `_EXTRACTORS` (currently
   `local_file`, `notion`, `github`, and `browser_history` — adding a
   source means adding one entry here, per
   `docs/File_Folder_Structure.docx` section 4;
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
   c. Each `(item, metadata)` pair → `_process_item()`, returning a
      `_ProcessedItem(item_id, was_update)`:
      - `storage/sqlite_store.py::insert_item()` — combines the item's
        extracted fields with its LLM metadata. `was_update` is `True`
        when this updated a pre-existing row (a source item edited since
        its last ingestion) rather than inserting a new one — read off
        `insert_item()`'s return value versus the freshly generated id
        passed in, no extra query needed (see DECISIONS.md, 2026-08-29)
      - `pipeline/chunking.py::chunk_text()` → `pipeline/embeddings.py::
        embed_chunks()` → vectors stored in Chroma, SQLite-ready `Chunk`s
        returned → `storage/sqlite_store.py::replace_chunks()`
      - if chunking/embedding/`replace_chunks()` raises, the just-inserted
        item row is deleted before the error propagates — no orphaned item
        with zero chunks (see DECISIONS.md, 2026-08-24)

      **Branch point**: a single item's processing failure is caught,
      logged, and recorded in `errors`; the rest of that source's items
      still get processed.
3. After every source is processed, before relationship detection: for
   every item that was an *update* (not a new insert), calls
   `storage/neo4j_store.py::delete_relationships_for_item()` — clears its
   existing edges (node kept) so relationship detection starts clean
   rather than `has_any_relationship()` skipping a candidate whose edge
   was judged against content that's since changed (see DECISIONS.md,
   2026-08-29). A failure here is caught, logged, and recorded in `errors`
   per item, same resilience pattern as the rest of the batch.
4. Then, for every successfully-processed item's id (new or updated),
   `pipeline/relationships.py::detect_relationships()` runs (its own
   internal per-candidate resilience is backed by a top-level catch here
   too, recorded in `errors` on failure)
5. `status` is computed from `errors`/`items_processed`
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

- `synthesize(conn, question, merged_results, *, top_k=None, history=()) ->
  SynthesizedAnswer` — takes the top `top_k` (default `config.yaml`'s
  `retrieval.top_k_vector` — see DECISIONS.md, 2026-08-25) merged results,
  fetches each item's full chunk text via `get_item()`/
  `get_chunks_for_item()`, and builds one `ContextChunk` per item (multiple
  chunks joined into one text block). A merged result whose item no longer
  exists, or has no chunks, is skipped, not fatal. If no result yields
  usable context, returns a fixed "nothing found" answer without calling
  the LLM at all. Otherwise calls
  `get_provider("answer").generate_answer(question, context, history)` and
  returns the answer alongside an ordered `sources` list — index `i`
  matches the `[i + 1]` citation marker the provider is instructed to use.
  `history` (prior `ConversationTurn`s) is passed straight through to the
  provider, not used for retrieval itself — see DECISIONS.md, 2026-08-25.

---

## Entry point: `agent/graph.py::run(conn, collection, driver, question, session_id=None)`

Reachable over HTTP via `POST /api/query` (see below). Also callable
directly (see `tests/test_agent/test_graph.py`).

1. Resolves `session_id` — the given one, or a fresh `uuid4` for a new
   session — and loads that session's prior turns via
   `storage/sqlite_store.py::get_messages_for_session()` → `ConversationTurn`
   list (`_load_history()`; empty for a new session). See DECISIONS.md,
   2026-08-25.
2. Builds a fresh LangGraph `StateGraph` per call (`_build_graph()`,
   closures over this call's `conn`/`collection`/`driver` — see
   DECISIONS.md, 2026-08-25) and invokes it, with `history` (and
   `search_query` initialized to the raw `question`) in the initial state,
   as one fixed, linear sequence of nodes:

   a. `condense_query` — if `history` is non-empty, calls
      `get_provider("condense").generate_search_query(question, history)`
      and overwrites `search_query` in state with the rewrite; a no-op
      (state unchanged) for a fresh session, or if the call raises
      `ProviderError` (logged and swallowed — see DECISIONS.md, 2026-08-29)
   b. `router` — `agent/router.py::route(search_query)` → the
      `RouteDecision` stored in state for every downstream node to check

      **Branch point**: this is the agent's main branch. A quoted phrase
      or a "from"/"by" filter skips vector search; relationship-implying
      phrasing (e.g. "how does X relate to Y") enables graph traversal;
      keyword search always runs. Runs on `search_query`, not the raw
      `question`, so a condensed follow-up routes the same way an
      equivalent first-turn question would.
   c. `vector_search` — `agent/search_nodes.py::vector_search()` against
      `search_query` if `decision.vector_search`, else contributes `[]`
   d. `keyword_search` — `agent/search_nodes.py::keyword_search_node()`
      against `search_query` if `decision.keyword_search` (always,
      currently), else `[]`
   e. `graph_traversal` — `agent/graph_traversal.py::graph_traversal()`,
      seeded from the combined vector + keyword hits, if
      `decision.graph_traversal`, else `[]`
   f. `merge` — `agent/merger.py::merge()` — Reciprocal Rank Fusion across
      whichever of the three hit lists are non-empty
   g. `synthesize` — `agent/synthesizer.py::synthesize()` — fetches full
      text for the top results from SQLite, calls
      `get_provider("answer").generate_answer(question, context, history)`
      using the original `question` (not `search_query`), returns a cited
      answer
3. After the graph returns, calls
   `storage/sqlite_store.py::record_conversation_turn()` with the resolved
   session id, the question, the final answer, and its sources — this
   persists *before* `run()` returns, so a client's very next call with the
   same `session_id` already sees this turn as history (step 1)
4. Returns a `QueryResult` (`session_id`, `answer`, `sources`,
   `retrieval_methods_used`)

---

## Entry point: `api/main.py` startup (`create_app()`)

Run via `uv run uvicorn api.main:app`. `create_app(*, lifespan_fn=lifespan)`
builds the FastAPI app and registers every route module from
`api/routes/`; the module-level `app = create_app()` is what `uvicorn`
actually serves.

1. On startup, the `lifespan` context manager first calls
   `agent/tracing.py::enable_tracing()` (a no-op if no `LANGSMITH_API_KEY`
   is configured — see "Shared: tracing" above), then opens one SQLite
   connection (`storage/sqlite_store.py::connect()`), one Chroma collection
   (`storage/chroma_store.py::get_collection()`), and one Neo4j driver
   (`storage/neo4j_store.py::get_driver()`), and stores them on
   `app.state.conn`/`collection`/`driver` for the process's lifetime —
   route handlers read them from `request.app.state` rather than via
   `Depends()` (see DECISIONS.md, 2026-08-25)
2. On shutdown, the driver and connection are closed
3. Tests substitute a no-op `lifespan_fn` and set `app.state` directly to
   test doubles (`tests/test_api/conftest.py`), so a test run never opens
   the real, settings-derived connections

Per `docs/File_Folder_Structure.docx` section 4, adding a new API endpoint
means adding a route module under `api/routes/`, registering it in
`create_app()`, and defining its request/response shape in
`api/schemas.py`.

`create_app()` also registers five exception handlers
(`_register_exception_handlers()`), applying to every route: validation
errors → 422, `ProviderError` → 502, `VectorStoreError`/`GraphStoreError`/
`StorageError` → 500, `ConfigError` → 500, anything else → 500 — all shaped
as `{"error": str, "detail": str}` per `docs/API_Specification.docx`
section 2. Route handlers don't need their own try/except for these — see
DECISIONS.md, 2026-08-25.

`create_app()` also adds `CORSMiddleware`, allowing any `localhost`/
`127.0.0.1` origin on any port — needed for `frontend/`'s Vite dev server
(a different port/origin) to call this API at all. See DECISIONS.md,
2026-08-25.

---

## Entry point: `GET /api/health` (`api/routes/health.py`)

1. Reads `conn`/`collection`/`driver` from `request.app.state`
2. `agent/health.py::check_health()` probes each — see DECISIONS.md,
   2026-08-25, for why this logic lives in `agent/` rather than the route
   module itself (the API layer never imports `storage`/`providers`
   directly)
3. Returns `{"status": "ok" | "degraded", "services": {...}}` per
   `docs/API_Specification.docx` section 3.2

---

## Entry point: `POST /api/query` (`api/routes/query.py`)

1. Request body validated against `api/schemas.py::QueryRequest`
   (`question`, optional `session_id`) — a missing `question` is a 422 via
   the shared validation-error handler above
2. `agent/graph.py::run()` invoked, reading `conn`/`collection`/`driver`
   from `request.app.state` (see above for `run()`'s own internal steps),
   timed with `time.monotonic()` for the response's `latency_ms`
3. `QueryResult` reshaped into `api/schemas.py::QueryResponse` and returned
   — a `ProviderError`/`VectorStoreError`/`GraphStoreError`/`StorageError`/
   any other exception `run()` raises is caught by `api/main.py`'s shared
   handlers above, not by this route itself

---

## Entry point: `GET /api/sources/status` (`api/routes/sources.py`)

1. Reads `conn` from `request.app.state`
2. `agent/sources_status.py::get_sources_status()` — see DECISIONS.md,
   2026-08-25, for how per-source `items_processed`/`status` are derived
   (not stored) from `storage/sqlite_store.py::get_last_ingestion_run()`
   plus a per-`source_type` `items` count within that run's window.
   `total_items` (a plain `COUNT(*)` per `source_type`, independent of any
   run window) is also included — `items_processed` alone is legitimately
   `0` whenever the latest run finds nothing new, which reads as "nothing
   has ever been ingested" without `total_items` alongside it (see
   DECISIONS.md, 2026-08-30)
3. Returns `{"last_run": {...} | null, "sources": [...]}` per
   `docs/API_Specification.docx` section 3.3 — `last_run` is `null` and
   every source reports 0 items/`"ok"` if the batch has never run

---

## Entry point: `GET`/`POST /api/sources/connections` (`api/routes/sources.py`)

Extension beyond `docs/API_Specification.docx` (same pattern as
`POST /api/ingest/trigger`) — live per-source connectivity/configuration,
distinct from `GET /api/sources/status`'s "last batch run outcome" above.
See DECISIONS.md, 2026-08-29.

1. `GET /sources/connections` → `agent/connection_check.py::
   get_connection_status()` — returns the in-process cache if younger than
   5 minutes, else runs `check_all_connections()` fresh and caches the
   result (`_cache`/`_cache_time`, both module-level)
2. `POST /sources/connections/verify` → same, with `force_refresh=True` —
   always runs fresh regardless of cache age; what the Settings screen's
   "Reverify" button calls
3. `check_all_connections()` checks all six sources, always in the same
   order: `local_file`/`browser_history` — a real filesystem check
   (configured path/dir exists and is reachable); `notion` — a real,
   cheap Notion API call (`Client(auth=...).users.me()`) if
   `NOTION_API_KEY` is set; `github` — a real, cheap GitHub API call
   (`GET /user` with `GITHUB_TOKEN`) if configured — see DECISIONS.md,
   2026-08-30; `gmail`/`calendar` — always `"not_configured"` with a "not
   yet built" detail, since no extractor exists for these yet — never
   silently reported as `"ok"`
4. Returns `{"connections": [{"source_type", "status", "detail",
   "checked_at"}, ...]}`, `status` one of `"ok"` / `"error"` /
   `"not_configured"`

---

## Entry point: `GET /api/settings` (`api/routes/settings.py`)

1. `config/settings.py::get_settings().config.llm` read directly — no
   `agent/` intermediary, since `config` isn't `storage`/`providers` (see
   DECISIONS.md, 2026-08-25)
2. Returns `{"provider_mode", "local_model", "cloud_model"}` per
   `docs/API_Specification.docx` section 3.6

---

## Entry point: `PUT /api/settings` (`api/routes/settings.py`)

1. Request body validated against `api/schemas.py::SettingsUpdateRequest`
   (all fields optional — a partial update) — an invalid `provider_mode`
   value is a 422 via the shared validation-error handler
2. `config/settings.py::update_llm_config()` called with whichever fields
   were given (see "Shared: configuration loading" above for its own
   steps) — a `ConfigError` (bad resulting config, or a file I/O failure)
   is caught by `api/main.py`'s shared handler, mapped to 500
3. Returns `{"status": "updated", "provider_mode", "local_model",
   "cloud_model"}` per `docs/API_Specification.docx` section 3.7, reflecting
   the freshly written-and-reread configuration

---

## Entry point: `GET`/`PUT /api/settings/sources` (`api/routes/settings.py`)

Extension beyond `docs/API_Specification.docx` — configurable ingestion
scope (local watch folders, Notion page/database ids), not just LLM
provider config. See DECISIONS.md, 2026-08-30.

1. `GET /settings/sources` → `config/settings.py::get_settings().env`'s
   `watch_dirs`/`notion_page_ids_list` computed properties, read directly
   (same "config isn't storage/providers" reasoning as `GET /settings`
   above)
2. `PUT /settings/sources` → `config/settings.py::update_source_config()`
   — writes `LOCAL_FILES_WATCH_DIRS`/`NOTION_PAGE_IDS` to `config/.env` via
   `python-dotenv`'s `set_key()` (rewrites just those lines in place,
   leaving every other line/comment untouched), only the given fields (a
   partial update); a `ConfigError` (file I/O failure) maps to 500 via the
   shared handler
3. Both return `{"local_files_watch_dirs": [...], "notion_page_ids": [...]}`
4. `extractors/notion.py::extract_new_items()` reads
   `notion_page_ids_list` on its next run: non-empty means fetch exactly
   those pages by id (`client.pages.retrieve()`) instead of the
   workspace-wide `client.search()`; empty keeps the original "every page
   the integration can see" behavior

---

## Entry point: `GET /api/sessions` (`api/routes/sessions.py`)

1. `storage/sqlite_store.py::list_sessions()` — most recently active first
2. Returns `{"sessions": [{"session_id", "title", "updated_at"}, ...]}` per
   `docs/API_Specification.docx` section 3.4

---

## Entry point: `GET /api/sessions/{session_id}` (`api/routes/sessions.py`)

1. `storage/sqlite_store.py::get_session()` — a `None` result returns a
   404 directly as `JSONResponse({"error": "not_found", ...})`, not via
   `HTTPException` (see DECISIONS.md, 2026-08-25, for why: `HTTPException`
   would bypass this project's `{"error", "detail"}` shape)
2. `storage/sqlite_store.py::get_messages_for_session()` — oldest first
3. Returns `{"session_id", "messages": [{"role", "text", "timestamp",
   "sources"}, ...]}` per `docs/API_Specification.docx` section 3.5

---

## Entry point: `POST /api/ingest/trigger` (`api/routes/ingest.py`)

Per `docs/API_Specification.docx` section 3.8 — a manual override for
development/testing, outside the daily schedule.

1. `agent/ingest_trigger.py::trigger_ingestion()` — generates a
   human-readable run label (`run_manual_<UTC timestamp>`) and spawns
   `scheduler/daily_batch.py` via `subprocess.Popen([sys.executable, "-m",
   "scheduler.daily_batch"], cwd=PROJECT_ROOT)`: an independent OS
   process, not an in-process call, so the Interface/Agent layers never
   import `scheduler` directly (per `docs/Component_Map.docx`'s dependency
   rules) and the spawned run gets its own fresh SQLite/Chroma/Neo4j
   connections rather than sharing the API process's long-lived
   `app.state` ones. Module invocation (`-m`), not a raw script path — see
   DECISIONS.md, 2026-08-29, for why the latter fails with
   `ModuleNotFoundError`
2. Returns immediately (no waiting for the subprocess): `202 Accepted`,
   `{"status": "started", "run_id": <label>}`. The label is independent of
   the UUID `storage/sqlite_store.py::start_ingestion_run()` assigns
   internally once the spawned process actually starts — there's no
   endpoint to look a run up by this label; outcome is checked afterward
   via `GET /api/sources/status`, same as a scheduled run

---

## Entry point: `frontend/` (`npm run dev`, or `index.html` in production)

A Vite + React app — see `frontend/README.md` for setup/dev commands.
`frontend/src/index.jsx` mounts `App.jsx` into `#root`.

1. `App.jsx` holds `view` (`"chat"` | `"settings"`), `sessionId`, and the
   fetched `sessions` list, and renders `Sidebar.jsx` plus either
   `ChatWindow.jsx` or `SettingsPanel.jsx` — no router, since the
   wireframe only needs a "Settings link," not deep-linking (see
   DECISIONS.md, 2026-08-25). Fetches `getSessions()` on mount and again
   after every completed turn (`onTurnCompleted`), so the sidebar's list
   and ordering stay current
2. `Sidebar.jsx` — "New chat" and clicking a real session both bump
   `App.jsx`'s `chatKey`, forcing `ChatWindow` to remount (see DECISIONS.md,
   2026-08-25, for why a remount rather than watching `sessionId` for
   changes). Renders the real session list from `App.jsx`, or "No past
   conversations yet" if it's empty
3. `ChatWindow.jsx` — on mount, if given an existing `sessionId`, calls
   `api/client.js::getSessionHistory()` once and populates messages from
   it (see DECISIONS.md, 2026-08-25). On submit, appends the user message
   and a "Thinking…" placeholder immediately, calls
   `api/client.js::postQuery(question, sessionId)`, then fills the
   placeholder in with the real answer and `SourceChip.jsx`-rendered
   sources (or an error state) once it resolves, and reports the (possibly
   new) `session_id` up to `App.jsx` via `onTurnCompleted`
4. `SettingsPanel.jsx` — on mount, calls `api/client.js::getSettings()`,
   `getSourcesStatus()`, `getSourceConnections()`, and `getSourceConfig()`
   in parallel; "Save Changes" calls `putSettings()` (provider config) and
   `putSourceConfig()` (watch folders/Notion scope, parsed from a
   one-per-line textarea via `linesToList()`) together, updating local
   state from the responses. Each connected-source card shows the live
   connection status (`getSourceConnections()`'s cache-or-fresh result)
   rather than the batch-run status alone; "Reverify" calls
   `verifySourceConnections()` (force-refreshes, bypassing the cache) and
   replaces the connections state with the fresh result — see
   DECISIONS.md, 2026-08-29 and 2026-08-30
5. `api/client.js` is the only module making network calls (per
   `docs/Coding_Conventions.docx` section 3) — every function maps
   directly to one `docs/API_Specification.docx` endpoint, talking to
   `http://127.0.0.1:8080/api` (not `localhost` — see DECISIONS.md,
   2026-08-25) with CORS enabled on the backend side for the dev server's
   own origin (see DECISIONS.md, 2026-08-25)

---

## Entry point: `eval/run_evaluation.py` (`main()`)

Run via `uv run python -m eval.run_evaluation` (module invocation — see
DECISIONS.md, 2026-08-29, for why `python eval/run_evaluation.py` would
fail the same way `scheduler/daily_batch.py` did). Per
`docs/Technical_Design_Document.docx` section 13.6's "realistic evaluation
workflow" — run after any meaningful architecture change.

1. `agent/tracing.py::enable_tracing()` — logs a warning (doesn't abort)
   if no `LANGSMITH_API_KEY` is configured, since dataset upload will
   still fail later without one
2. Opens the real, settings-derived SQLite connection, Chroma collection,
   and Neo4j driver (same as `scheduler/daily_batch.py::main()`)
3. `load_test_questions()` — reads `eval/test_questions.json`'s 26
   hand-written questions (real, built against the currently-ingested
   corpus — project design docs and Notion recipe pages; see
   DECISIONS.md, 2026-08-29)
4. `_ensure_dataset(client, questions)` — reuses an existing LangSmith
   Dataset named `pkg-agent-eval-questions` if one exists, else creates it
   and uploads every question as an example (`inputs: {question}`,
   `outputs: {expected_item_ids, expected_answer_summary}`)
5. `get_provider("eval")` — the LLM-as-judge provider (cloud in `mixed`
   mode; see "Shared: LLM providers" above)
6. `langsmith.evaluate()` runs `_make_target()`'s function — which calls
   the real `agent/graph.py::run()` for each question, then reconstructs
   the exact context text `agent/synthesizer.py::synthesize()` used (all
   of each cited source's chunks, `get_chunks_for_item()` +
   `"\n\n".join()`) — against three evaluators from `_make_evaluators()`:
   `recall_at_k` (pure, `eval/evaluators.py`), `evaluate_faithfulness()`,
   and `evaluate_relevance()` (both LLM-as-judge via the `"eval"`
   provider). Results land as a real, browsable experiment in the
   configured LangSmith project

---
