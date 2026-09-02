# Execution Flow

A map of how the code actually executes: entry points, the order calls happen
in, and which module hands off to which. Updated in the same commit as any
change to an entry point or call chain.

> **Status**: the configuration layer, all three storage backends, the
> provider layer, the entire pipeline layer (`filters`, `chunking`,
> `metadata`, `embeddings`, `relationships`), `scheduler/daily_batch.py`,
> and all six extractors (local files, Notion, Gmail, GitHub, Google
> Calendar, browser history) are implemented and merged to `main`. The
> agent layer is complete:
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
> verified against the real LangSmith account (see below). No extractors
> remain unbuilt.

---

## Shared: configuration loading

Every entry point resolves configuration the same way, on first use:

1. `config/settings.py::get_settings()` — cached for the process lifetime
2. → `EnvSettings()` reads `config/.env` plus the process environment
3. → `load_config()` reads and validates `config/config.yaml`
4. Returns `Settings(env=..., config=...)`

`reload_settings()` clears the cache and re-reads both sources.

`update_llm_config(*, provider_mode=None, local_generation_model=None,
cloud_generation_model=None, cloud_embedding_model=None,
path=DEFAULT_CONFIG_PATH)` — used by `PUT /api/settings` (see below) to
apply a provider/model change without a restart. Loads `config.yaml` with
`ruamel.yaml`'s round-trip mode (not plain PyYAML — see DECISIONS.md,
2026-08-25, for why: comments/quotes/list formatting must survive a write),
updates only the given `llm` fields, validates via
`LLMConfig.model_validate()` *before* writing anything to disk, then writes
back (through `_retry_on_transient_permission_error()` — see below) and
returns the freshly re-read config. Only invalidates the process-wide
`get_settings()` cache when `path` is the real default file — a caller
using a different path (tests) gets that file's own config back without
touching the global cache.

`update_source_config(*, local_files_watch_dirs=None, notion_page_ids=None,
path=DEFAULT_ENV_PATH)` — used by `PUT /api/settings/sources`. `.env` is
flat `KEY=VALUE` lines, so `python-dotenv`'s own `set_key()` (also routed
through `_retry_on_transient_permission_error()`) rewrites just the given
line(s) in place without a custom round-trip parser the way `config.yaml`
needs. Every watch-folder path has a trailing `\`/`/` stripped before
writing — a path ending in a bare backslash breaks `python-dotenv`'s own
write-then-read round trip (its writer only escapes literal quote
characters, not backslashes, so a trailing `\` lands right before the
closing quote and its *reader* treats that as an escaped quote instead of
the string ending — corrupting every line after it in the file too,
verified directly against a real occurrence that silently dropped
`OPENROUTER_API_KEY`). See DECISIONS.md, 2026-08-30.

`_retry_on_transient_permission_error(call)` — both writers above go
through this rather than calling their underlying write directly. Windows
can transiently deny the rename/replace either write does (a real-time
antivirus scanner, the search indexer, an editor's file-watcher briefly
holding the target open) with a `PermissionError` (WinError 5) that
clears itself within milliseconds — verified directly against a real
user-reported failure, where the identical write succeeded immediately on
manual retry. Retries up to 5 times with a short linear backoff before
letting the error propagate to the caller's own `except OSError` →
`ConfigError` handling unchanged; a non-`PermissionError` `OSError` (a
genuinely bad path, real permissions) is never retried. See DECISIONS.md,
2026-08-30.

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
5. Whole-graph fetch: `get_full_graph(driver) -> GraphSnapshot(nodes, edges)`
   — one `MATCH (i:Item) RETURN i` plus one
   `MATCH (a:Item)-[r:RELATES_TO]->(b:Item) RETURN ...`, no pagination or
   filtering (the graph stays small by construction — unconnected items
   never get a node, see point 1's `docs/Database_Schema.docx` section 5
   note). What `GET /api/graph` (see below) calls, via
   `agent/graph_view.py::get_graph_snapshot()` — see DECISIONS.md,
   2026-08-30.

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
   (`update_ingestion_run_progress(run_id, items_processed,
   current_item=...)`, called once per item as `_run()` processes each
   one — not just at the very end, so `items_processed` updates live
   while `status` is still `"running"`, for a real live-progress readout
   rather than a start/stop signal — see DECISIONS.md, 2026-08-30;
   `current_item` names what was just processed, and
   `update_ingestion_run_current_item(run_id, ...)` separately announces
   which source is currently being *extracted*, before any of its items
   are countable — see DECISIONS.md, 2026-08-31) →
   `complete_ingestion_run(status=...)` → `get_last_run_timestamp()`
   reads the watermark for the next run's `since=`;
   `get_last_ingestion_run()` returns the most recent run regardless of
   status (for display, e.g. `agent/sources_status.py` — see
   DECISIONS.md, 2026-08-25)
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
   must equal the SQLite item's effective id from `insert_item()`. A
   collection is locked to whatever vector dimensionality its first write
   used — a later write of a different size (the embedding model's output
   width changed) fails for every item, not just new ones; this function
   detects that specific Chroma error and raises a `VectorStoreError`
   naming the real fix ("Reset all data", then re-ingest) instead of the
   generic message — verified directly against a real, systemic
   occurrence of this failure. See DECISIONS.md, 2026-08-30.
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
6. `reset_all(persist_dir=None)` — **deletes and recreates the whole
   collection** (`client.delete_collection()` +
   `client.get_or_create_collection()`), not just its documents; deleting
   documents alone leaves the dimension lock from step 2 in place, which
   defeats the entire point of resetting before switching embedding
   models. Returns the freshly created `Collection` — callers holding a
   long-lived reference (`api/main.py`'s `app.state.collection`) must
   replace it with the return value, since the old object is invalid once
   its underlying collection is deleted. See DECISIONS.md, 2026-08-30
   ("`reset_all()`'s Chroma reset never actually cleared the dimension
   lock").

---

## Shared: LLM providers (`providers/`)

Not an entry point itself — every LLM call anywhere in the system goes
through this layer (`CLAUDE.md`'s "never bypass the LLM Provider
abstraction" ground rule). Callers never import `local_provider.py` or
`openrouter_provider.py` directly, or a LangChain/Ollama/OpenAI SDK type —
only `providers/base.py`'s `get_provider()` and its return type,
`ProviderInterface`.

1. `get_provider(task)` — `task` is `"metadata"`, `"relationship"`,
   `"condense"`, `"answer"`, `"eval"`, or `"embedding"`. Every task,
   including `"embedding"`, reads `settings.config.llm.provider_mode` and
   returns `create_local_provider()` (Ollama) for `fully_local`, or
   `create_openrouter_provider()` (OpenRouter) for `fully_cloud` — there is
   no `mixed` mode (removed — see DECISIONS.md, 2026-08-30). Local
   embedding was brought back via Ollama (see DECISIONS.md, 2026-09-01,
   "Local embedding via Ollama restored"), reversing an earlier design
   where `"embedding"` always resolved to OpenRouter regardless of mode —
   `fully_local` is once again zero-cost and zero-network for both
   generation and embedding.
2. Both concrete providers construct a `LangChainProvider` around a
   LangChain chat model (`ChatOllama` / `ChatOpenAI`) for their prompt
   building, JSON response parsing, and retry-with-backoff — shared by
   both. Both also build an `embed_fn`: `create_local_provider()` wraps a
   LangChain `OllamaEmbeddings` (`llm.local_embedding_model`, default
   `nomic-embed-text`); `create_openrouter_provider()` wraps a LangChain
   `OpenAIEmbeddings` pointed at OpenRouter's base URL
   (`llm.cloud_embedding_model`). Neither forces a shared output
   dimensionality any more — switching `provider_mode`, or editing either
   embedding model, can leave Chroma holding vectors from an incompatible
   space; the frontend is responsible for treating a `provider_mode`
   switch as destructive (double-confirm, then an automatic full reset)
   rather than anything enforced here. The `ChatOpenAI`
   (OpenRouter) side always passes an explicit
   `max_tokens` (`llm.cloud_max_tokens`, default `4096`) — left unset it
   would default to the routed model's own maximum (64000 for
   `claude-sonnet-4`), which can exceed the account's remaining credit
   balance and fail the call outright; verified directly — see
   DECISIONS.md, 2026-08-29
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
8. `provider.generate_embeddings(texts)` — called by
   `pipeline/embeddings.py::embed_chunks()`/`embed_query()` (see below);
   returns one vector per input text. Follows `provider_mode` like every
   other task (see point 1 above) — Ollama's `embed_fn` under
   `fully_local`, OpenRouter's under `fully_cloud`.

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
  directories still get scanned. `watch_dirs` is `[]` when
  `LOCAL_FILES_WATCH_DIRS` is unset — watches nothing until the user
  explicitly configures a folder; see DECISIONS.md, 2026-09-01 (this
  reverts an earlier auto-created-default-folder behavior from
  2026-08-31).
- `extractors/notion.py` — lists every page the integration (`NOTION_API_KEY`)
  can see via `Client.search()` (paginated), unless `NOTION_PAGE_IDS` is
  configured, in which case only those pages are fetched directly by id
  (`Client.pages.retrieve()`) instead — see DECISIONS.md, 2026-08-30. Each
  root page goes through `_extract_page_and_subpages()`, which recursively
  walks its blocks via `Client.blocks.children.list()`
  (`_collect_block_text()`), converting each block's `rich_text` to plain
  text and joining blocks with blank lines so headings/paragraphs stay
  natural chunk boundaries. Title comes from the page's `title`-type
  property. A `child_page` block found while walking is *not* folded into
  the parent's text — it's fetched via `Client.pages.retrieve()` and
  recursed into as its own independent extraction (own `since` check, own
  item, own further subpages), since a subpage's edits don't bump its
  parent's `last_edited_time` in Notion — a page's blocks are walked to
  discover subpages even when the page itself fails its own `since` check,
  otherwise a subpage nested under an unchanged parent could never be
  found at all once scoped to specific page ids. A `seen_page_ids` set
  dedupes a page reachable both directly (`search()`, unscoped mode) and
  as a subpage of another page — see DECISIONS.md, 2026-08-31. Missing
  `NOTION_API_KEY`, or a failed `search()` call, raises `ExtractorError`
  (source-level); a single page's block-fetch or subpage-fetch failing is
  logged and skipped, not fatal — see DECISIONS.md, 2026-08-24. Logs an
  INFO progress line every 25 *root* pages scanned (a full scan visits
  every visible page and can take a long time on a large workspace — see
  DECISIONS.md, 2026-08-24).
- `extractors/gmail.py` — one item per **thread**, not per message
  (`docs/Data_Extraction_Specification.docx` section 5 lists thread ID as
  metadata, but `ExtractedItem` has no generic metadata field to hang it
  off, so the whole thread — every message, oldest first — is
  concatenated into one item's `raw_text` instead; a new reply
  re-extracts and re-embeds the whole conversation, since `source_ref_id`
  is the thread id — see DECISIONS.md, 2026-08-30). Auth: loads a cached
  OAuth token (`data/gmail_token.json`), silently refreshing it if
  expired — never triggers the interactive browser consent flow itself.
  If no cached token exists yet, raises `ExtractorError` naming the
  one-time setup command (`setup_auth()`, run via
  `uv run python -m extractors.gmail --setup-auth`) instead of hanging an
  unattended run waiting on a consent screen. Lists message ids via
  `messages().list(q="after:<since>")` (Gmail search syntax), resolves
  each to its thread id (free — included in the list response), then
  fetches each distinct thread in full. Per message: labels matching
  `config.yaml`'s `filters.gmail.excluded_labels` (e.g.
  `CATEGORY_PROMOTIONS`) are dropped; body text prefers `text/plain`,
  falling back to a crude HTML-tag-stripped `text/html`; PDF/DOCX
  attachments get their text extracted via the same `pypdf`/`python-docx`
  libraries `extractors/local_files.py` uses. A thread where every
  message was filtered out produces no item. `_effective_window()`
  combines the incremental `since` cursor with
  `settings.env.effective_gmail_date_range_start`/`_end` — defaulting to
  the last 15 days when `GMAIL_DATE_RANGE_START`/`_END` are unset, never
  unbounded (see DECISIONS.md, 2026-09-01): the start date floors `since`
  (via `max()`, never moving a later cursor backward) and the end date
  adds an inclusive `before:` to the Gmail search query on every run —
  see DECISIONS.md, 2026-08-30.
- `extractors/github.py` — per repo (scoped to `settings.env.github_repos_list`
  if set, else every accessible repo — see DECISIONS.md), extracts the
  README (re-checked via its own last-commit date, not every run),
  commits (message + changed file names, no diffs), PRs (title/body +
  review comments), issues (excluding PR-shaped entries via the
  `pull_request` key), and starred repos (module-level, not per-repo).
  `_effective_window()` (same shape as `extractors/gmail.py`'s) combines
  `since` with `GITHUB_DATE_RANGE_START`/`GITHUB_DATE_RANGE_END`: the
  start date floors `since`; the end date (`until`) is passed as GitHub's
  own `until` query param for commits, and filtered client-side for PRs/
  issues/starred repos (those endpoints have no server-side upper bound)
  — see DECISIONS.md, 2026-08-30.
- `extractors/calendar.py` — lists the primary calendar's events via
  `events().list(singleEvents=False, updatedMin=<since>, timeMin=<range
  start>, timeMax=<range end + 1 day>)` — the `singleEvents=False` is what
  keeps a recurring series as one event (carrying a `recurrence` rule)
  instead of expanding it into one entry per occurrence, satisfying
  "recurrence pattern stored once" for free, server-side — see
  DECISIONS.md, 2026-08-30. `timeMin`/`timeMax` are a window on each
  event's own start/end time — defaulting to today through 30 days out
  when `CALENDAR_DATE_RANGE_START`/`_END` are unset (`settings.env
  .effective_calendar_date_range_start`/`_end`) — an independent axis
  from `updatedMin`'s "changed recently" that Google's API ANDs together
  with it, same combined shape as Gmail's own range+incremental check.
  See DECISIONS.md, 2026-09-01. Auth: same cached-OAuth-
  token pattern as `extractors/gmail.py` (own credentials/token file,
  `GOOGLE_CALENDAR_CREDENTIALS_PATH` / `data/calendar_token.json`), same
  one-time `setup_auth()` step
  (`uv run python -m extractors.calendar --setup-auth`). Per event: a
  `status: "cancelled"` event is skipped; a recurring event with no
  description is dropped when `filters.calendar.skip_recurring_without_description`
  is set (default `true`) — a standing reminder, not a real meeting; a
  timed event's `start`/`end` carries `dateTime` directly, an all-day
  event's bare `date` is anchored to UTC midnight. `raw_text` is title +
  time + attendees + description together (not description-only — most
  personal events have no description at all, which would otherwise
  leave nothing to embed); location and color/category tag are dropped
  entirely (no metadata field to put them in, same limitation as Gmail's
  thread id).
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
Embedding itself is not done here directly any more — both functions
below call `providers/base.py::get_provider("embedding").
generate_embeddings()`, which always resolves to OpenRouter regardless of
`provider_mode` (there is no local embedding path — see DECISIONS.md,
2026-08-30, latest entry), so an ingestion run needs `OPENROUTER_API_KEY`
configured even under `fully_local`.

- `embed_chunks(collection, item_id, source_type, chunk_texts, *,
  project_name=None, topic=None, created_at=None) -> list[Chunk]` — calls
  `get_provider("embedding").generate_embeddings(chunk_texts)`, then
  `storage/chroma_store.py::upsert_chunks()` itself, and returns
  SQLite-ready `Chunk` objects (each with a freshly generated
  `embedding_id` pointing at the vector just written) for the caller to
  persist via `storage/sqlite_store.py::replace_chunks()`. Takes an
  already-open `collection` rather than opening its own, since
  `get_collection()` opens a new client on every call by design — the
  caller opens one collection and reuses it across the whole ingestion run.
- `embed_query(text) -> list[float]` — the single-text form, used for
  query-time vector search and by `pipeline/relationships.py`'s candidate
  search.

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
   starts a run record via `start_ingestion_run(conn)` — which also
   records `os.getpid()` on the new row, so `POST /api/ingest/cancel` can
   force-kill this exact process later regardless of which process
   spawned it (see that entry point, DECISIONS.md, 2026-09-01)
2. For each registered extractor in `_EXTRACTORS` (currently
   `local_file`, `notion`, `gmail`, `github`, `calendar`, and
   `browser_history` — adding a source means adding one entry here, per
   `docs/File_Folder_Structure.docx` section 4;
   `tests/test_scheduler/test_daily_batch.py`'s integration tests pin
   `_EXTRACTORS` to `local_file` only via an autouse fixture, so a new
   entry here never makes those tests real-network-dependent — see
   DECISIONS.md, 2026-08-24).

   **Branch point**: `is_cancellation_requested(conn, run_id)` is checked
   before each source runs — if a Stop request landed since the last
   source finished, the loop breaks here with `cancelled = True` (see
   DECISIONS.md, 2026-08-31). This is the only cancellation check
   `gmail`/`calendar`/`browser_history` ever get, since they don't call
   `on_progress` (issue #68).

   Before `extract()` runs, `current_item` is announced
   (`update_ingestion_run_current_item(run_id, "Extracting
   {source_name}…")`) — the only feedback available during this phase,
   which can run for many minutes with nothing yet countable.
   a. `extract(since, on_progress)` → list of `ExtractedItem`. `on_progress`
      is a closure that writes a live `"{source_name}: {label}
      ({current}/{total})"` (or `"({current} scanned)"` when `total` is
      `None`) via `update_ingestion_run_current_item()`, then returns
      `not is_cancellation_requested(conn, run_id)`. Only `github.py`
      (per repo), `local_files.py` (per matching file), and `notion.py`
      (per root page) actually call it — see DECISIONS.md, 2026-08-31.
      An `ExtractorError` here is caught, logged, and recorded in
      `errors`; the loop moves on to the next source

      **Branch point**: an extractor failure doesn't stop the batch — the
      remaining sources still run. A `False` return from `on_progress`
      stops *that extractor's own loop* early (its items already gathered
      are kept) and is re-checked via `is_cancellation_requested()`
      immediately after `extract()` returns, setting `cancelled = True`.
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
      still get processed. If `cancelled` is set once this source's items
      are done, the outer source loop breaks — items from later sources
      are never reached, but everything processed so far is kept.
3. After every source is processed (or the loop broke early on
   cancellation), before relationship detection: for
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
5. `status` is computed from `cancelled`/`errors`/`items_processed`
   (`"cancelled"` takes priority over the rest, else
   `"success"`/`"partial_failure"`/`"failed"` — see DECISIONS.md,
   2026-08-24 and 2026-08-31) and written via `complete_ingestion_run()`

   **Branch point**: only `status = "success"` advances the watermark
   (`storage/sqlite_store.py::get_last_run_timestamp()`'s own behavior).
   `"partial_failure"`, `"failed"`, and `"cancelled"` all leave it
   unchanged, so every source is retried on the next run — `"cancelled"`
   and `"partial_failure"` just mean some items got through this time too.

---

## Entry point: `scheduler/loop.py` (`run_forever()`)

Run via `uv run python -m scheduler.loop` — the Docker-only counterpart to
`scheduler/daily_batch.py` above. `docker-compose.yml` runs this as its own
long-lived `scheduler` service, replacing "register a cron/Task Scheduler
entry by hand" entirely for a containerized deployment, since there's no
host-level cron/Task Scheduler inside a container to register with instead.
A manual (non-Docker) developer setup still uses real cron/Task Scheduler
against `scheduler/daily_batch.py` directly, as documented in the README —
this loop doesn't replace that path. See DECISIONS.md (issue #52).

1. `run_forever()` reads `now_fn()` once at startup as `last_check`, then
   loops forever: `sleep(poll_interval_seconds)` (default 30s), reads
   `now_fn()` again, and calls `config.settings.reload_settings()` fresh
   on every iteration — not just once at startup — so editing
   `config.yaml`'s `ingestion.schedule` from Settings takes effect within
   one poll interval, without restarting this container
2. `_should_run(schedule, last_check, now)` — `croniter(schedule,
   last_check).get_next(datetime) <= now`. Searching strictly after
   `last_check` (not `now`) means the same fire time is never matched
   twice across consecutive polls
3. **Branch point**: if due, calls `scheduler.daily_batch.main()` (the
   exact same entrypoint a manual cron/Task Scheduler registration would
   call) — a raised exception here is logged (`logger.exception()`) and
   swallowed, never ending the loop, the same "one bad run doesn't end the
   schedule" reasoning `daily_batch.py`'s own per-source/per-item error
   handling already has
4. `last_check` is updated to `now` regardless of whether a run happened,
   so the next iteration's search window starts from here

`sleep`, `now_fn`, and `run_batch` are all injectable keyword arguments,
defaulting to the real `time.sleep`/wall clock/`daily_batch.main` — tests
substitute a `sleep` that raises after a fixed number of calls (the only
way to terminate an intentionally infinite loop deterministically) and a
`now_fn` that advances a fake clock per call, since real elapsed time
between two calls to a mocked `sleep` is otherwise near zero.

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

Run via `uv run python -m api.main` (module invocation — picks a stable
port with fallback, see below; `uv run uvicorn api.main:app --port ...`
still works but always binds to exactly the port you pass it, no
fallback). `create_app(*, lifespan_fn=lifespan)` builds the FastAPI app
and registers every route module from `api/routes/`; the module-level
`app = create_app()` is what `uvicorn` actually serves either way.

0. The `if __name__ == "__main__":` block (module-invocation path only):
   `_select_port()` tries `settings.env.fastapi_port` then
   `_PORT_FALLBACKS = (8080, 8090, 8091)` in order, binding the first free
   one (`_is_port_free()`, a plain `socket` bind-and-release check), and
   writes it to `data/backend_port.txt` — `frontend/vite.config.js` reads
   that file at config-load time and injects it as
   `import.meta.env.VITE_API_BASE_URL`, which `client.js`'s `API_BASE_URL`
   reads, so the two processes can never drift out of sync on which port
   the backend actually ended up on. See DECISIONS.md, 2026-08-31.
   `_select_bind_host()` picks the interface `uvicorn.run()` binds:
   `"127.0.0.1"` on bare metal (this backend's normal "local-only,
   single-user" posture), or `"0.0.0.0"` when `settings.env.running_in_docker`
   is set — `127.0.0.1` inside a container is invisible to Docker's own
   port-forwarding, which connects to the container's *external* interface,
   not its loopback; verified directly against a real running stack (see
   DECISIONS.md, 2026-09-02, issue #52)

1. On startup, the `lifespan` context manager first calls
   `agent/tracing.py::enable_tracing()` (a no-op if no `LANGSMITH_API_KEY`
   is configured — see "Shared: tracing" above), then opens one SQLite
   connection (`storage/sqlite_store.py::connect()`), one Chroma collection
   (`storage/chroma_store.py::get_collection()`), and one Neo4j driver
   (`storage/neo4j_store.py::get_driver()`), and stores them on
   `app.state.conn`/`collection`/`driver` for the process's lifetime —
   route handlers read them from `request.app.state` rather than via
   `Depends()` (see DECISIONS.md, 2026-08-25). Also stores
   `app.state.chroma_persist_dir` (the same directory `get_collection()`
   just opened) so `POST /api/admin/reset` can later reset Chroma against
   the exact directory this process is using, rather than re-deriving it
   from `get_settings()` directly — the latter would break test isolation,
   since tests point Chroma at an isolated `tmp_path` (see DECISIONS.md,
   2026-08-30)
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

## Entry point: `docker compose up -d --build` (issue #52)

Not a code entry point itself — a deployment topology wrapping the real
entry points above/below it. See DECISIONS.md, 2026-09-02.

1. `neo4j` starts first; `backend`/`scheduler` both `depends_on:
   condition: service_healthy` on it, so neither starts until Neo4j's own
   healthcheck (`wget` against its HTTP port) passes
2. `backend` and `scheduler` are the **same built image**
   (`docker/backend.Dockerfile`) with different `command`s —
   `python -m api.main` vs. `python -m scheduler.loop` — sharing one
   `app_data` named volume (`/app/data`, holding both the SQLite db and
   Chroma's persist dir) so either process's writes are visible to the
   other, the same "share the filesystem" relationship a manual dev
   setup's foreground backend process and spawned `daily_batch`
   subprocess already have
3. Both mount the real `config/.env` read-only via `env_file:` unchanged —
   every credential works exactly as it does outside Docker — while a
   compose-level `environment:` block overrides only
   `SQLITE_DB_PATH`/`CHROMA_PERSIST_DIR`/`NEO4J_URI`/`OLLAMA_HOST`/
   `LOCAL_FILES_WATCH_DIRS`/`RUNNING_IN_DOCKER`, since those must point
   in-container/in-network rather than at the host; real env vars set
   this way win over the mounted file's own values
4. `ollama` only starts when the `local-llm` Compose profile is active
   (`--profile local-llm`, or `COMPOSE_PROFILES=local-llm` in the
   root-level `.env`) — irrelevant, and skipped, under
   `provider_mode: fully_cloud`
5. `frontend` (`docker/frontend.Dockerfile`, multi-stage: `npm run build`
   → served by bare `nginx`) is built with `VITE_API_BASE_URL` baked in
   at image-build time from `HOST_BACKEND_PORT` — the *published* host
   port, since the browser making requests runs on the host, not inside
   the Docker network (see `frontend/vite.config.js`'s own comment,
   `api/main.py`'s `_select_bind_host()` above)
6. Two separate `.env` files are in play, deliberately: the root-level one
   (`.env.docker.example` → `.env`) is read only by `docker compose`
   itself to fill in `${VARS}` in `docker-compose.yml` (`NEO4J_PASSWORD`,
   `HOST_WATCH_DIR`, `HOST_DATA_DIR`, published ports); `config/.env` is
   the application's own configuration, identical in shape to a manual
   dev setup's

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
3. Returns `{"last_run": {..., "items_processed"} | null, "sources": [...]}`
   per `docs/API_Specification.docx` section 3.3 — `last_run` is `null` and
   every source reports 0 items/`"ok"` if the batch has never run.
   `last_run.items_processed` (extends the documented shape — see
   DECISIONS.md, 2026-08-30) updates live while `status` is still
   `"running"`, not just once the run completes — this is what
   `frontend/src/App.jsx`'s ingestion-completion polling reads to show
   real, live progress rather than only a start/stop signal

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
   cheap Notion API call (`notion_key_works()` → `Client(auth=...).users.me()`)
   if `NOTION_API_KEY` is set; `gmail` — a real, cheap Gmail API call
   (`users().getProfile()`) using the cached OAuth token if either
   `GMAIL_CREDENTIALS_PATH` is set **or** the guided setup wizard's shared
   Google token exists (`agent/google_oauth.py::is_connected()` — see
   DECISIONS.md, 2026-09-02, issue #92); `github` — a real, cheap GitHub
   API call (`github_token_works()` → `GET /user`) if `GITHUB_TOKEN` is
   set; `calendar` — a real, cheap Calendar API call
   (`calendarList().get(calendarId="primary")`) using the cached OAuth
   token, same either-path "configured" check as Gmail
   (`GOOGLE_CALENDAR_CREDENTIALS_PATH` or the shared wizard token).
   `notion_key_works()`/`github_token_works()` are standalone, parameterized
   functions (not inlined in `_check_notion()`/`_check_github()` any
   more) — the guided setup wizard's `POST /api/setup/validate` calls the
   same two functions directly, against a pasted-but-not-yet-saved value,
   before persisting it (see the `/api/setup/*` entry point below,
   DECISIONS.md, 2026-09-02).
   For the two OAuth sources (Gmail, Calendar), "not yet authorized" (no
   token via either path yet) reports `"not_configured"`, not an error,
   since nothing is actually broken; any other failure (revoked token,
   unreachable API) reports `"error"` — see DECISIONS.md, 2026-08-30
4. Returns `{"connections": [{"source_type", "status", "detail",
   "checked_at"}, ...]}`, `status` one of `"ok"` / `"error"` /
   `"not_configured"`

---

## Entry point: `GET /api/settings` (`api/routes/settings.py`)

1. `config/settings.py::get_settings().config.llm` read directly for all
   four model fields (both generation models, both embedding models) — no
   `agent/` intermediary, since `config` isn't `storage`/`providers` (see
   DECISIONS.md, 2026-08-25).
2. Returns `{"provider_mode", "local_generation_model",
   "local_embedding_model", "cloud_generation_model",
   "cloud_embedding_model"}` — `local_embedding_model` restored (see
   DECISIONS.md, 2026-09-01, "Local embedding via Ollama restored") after
   an earlier iteration removed it entirely; extends
   `docs/API_Specification.docx` section 3.6's original two-field shape

---

## Entry point: `PUT /api/settings` (`api/routes/settings.py`)

1. Request body validated against `api/schemas.py::SettingsUpdateRequest`
   (`provider_mode`, `local_generation_model`, `local_embedding_model`,
   `cloud_generation_model`, `cloud_embedding_model` — all optional, a
   partial update) — an invalid `provider_mode` value is a 422 via the
   shared validation-error handler
2. `config/settings.py::update_llm_config()` called with whichever fields
   were given (see "Shared: configuration loading" above for its own
   steps) — a `ConfigError` (bad resulting config, or a file I/O failure)
   is caught by `api/main.py`'s shared handler, mapped to 500. This route
   itself has no special handling for a `provider_mode` switch or either
   embedding-model change — the "confirm, then reset (+ re-ingest for
   `cloud_embedding_model` only)" behavior is entirely
   `SettingsPanel.jsx`'s responsibility (see below), not enforced here
3. Returns `{"status": "updated", "provider_mode",
   "local_generation_model", "local_embedding_model",
   "cloud_generation_model", "cloud_embedding_model"}`, reflecting the
   freshly written-and-reread configuration

---

## Entry point: `POST /api/settings/browse-folder` (`api/routes/settings.py`)

Extension beyond `docs/API_Specification.docx` — see `agent/browse.py`,
DECISIONS.md, 2026-08-30.

1. **Branch point**: if `settings.env.running_in_docker` is set, returns a
   `422 {"error": "not_available_in_docker", ...}` directly, without
   attempting the dialog at all — neither a GUI nor PowerShell itself
   exists inside a Linux container (see DECISIONS.md, 2026-09-02, "Local
   files watch dirs become a fixed, read-only Docker volume mount")
2. Otherwise: `agent/browse.py::browse_folder()` — shells out to a
   PowerShell `System.Windows.Forms.FolderBrowserDialog` script, blocking
   until the dialog is closed
3. Returns `{"path": <string> | null}` — `null` if the user cancelled
   rather than picking a folder

---

## Entry point: `GET`/`PUT /api/settings/sources` (`api/routes/settings.py`)

Extension beyond `docs/API_Specification.docx` — configurable ingestion
scope (local watch folders, Notion page/database ids), not just LLM
provider config. See DECISIONS.md, 2026-08-30.

1. `GET /settings/sources` → `config/settings.py::get_settings().env`'s
   `watch_dirs`/`notion_page_ids_list` computed properties, read directly
   (same "config isn't storage/providers" reasoning as `GET /settings`
   above). Gmail's/Calendar's date-range fields come from
   `effective_gmail_date_range_start`/`_end` and
   `effective_calendar_date_range_start`/`_end` — always a real window,
   defaulted when unset (see DECISIONS.md, 2026-09-01) — while GitHub's
   own date range is read raw (`github_date_range_start`/`_end`, still
   `None`-able)
2. `PUT /settings/sources` — **branch point**: if the payload includes
   `local_files_watch_dirs` (non-`None`) while
   `settings.env.running_in_docker` is set, returns `422
   {"error": "not_available_in_docker", ...}` immediately, without calling
   `update_source_config()` at all — that field is a fixed,
   volume-mount-backed container path in Docker, not something the running
   app can change on its own (see DECISIONS.md, 2026-09-02). Every other
   field in the same payload is unaffected by this check. Otherwise:
   `config/settings.py::update_source_config()` — writes
   `LOCAL_FILES_WATCH_DIRS`/`NOTION_PAGE_IDS`/
   `CALENDAR_DATE_RANGE_START`/`_END`/etc. to `config/.env` via
   `python-dotenv`'s `set_key()` (rewrites just those lines in place,
   leaving every other line/comment untouched), only the given fields (a
   partial update); a `ConfigError` (file I/O failure) maps to 500 via the
   shared handler
3. Both return `{"local_files_watch_dirs": [...], "notion_page_ids": [...],
   "github_repos": [...], "gmail_date_range_start"/"_end",
   "github_date_range_start"/"_end", "calendar_date_range_start"/"_end",
   "running_in_docker": bool}` — the last field tells the frontend which
   Local Files UI to render (editable + Browse vs. read-only list, see
   `SettingsPanel.jsx` below)
4. `extractors/notion.py::extract_new_items()` reads
   `notion_page_ids_list` on its next run: non-empty means fetch exactly
   those pages by id (`client.pages.retrieve()`) instead of the
   workspace-wide `client.search()`; empty keeps the original "every page
   the integration can see" behavior

---

## Entry point: `POST /api/admin/reset` (`api/routes/admin.py`)

Extension beyond `docs/API_Specification.docx` — a destructive, hard-to-
reverse full-database wipe. See DECISIONS.md, 2026-08-30.

1. Request body validated against `api/schemas.py::AdminResetRequest`
   (`confirm: bool`) — `confirm: false` (or omitted) returns a 422
   directly as `JSONResponse({"error": "confirmation_required", ...})`,
   same "shaped like every other error" reasoning as `GET
   /api/sessions/{id}`'s 404, not via `HTTPException`
2. `agent/admin.py::reset_all_data()`, reading `conn`/`driver` and
   `chroma_persist_dir` (not `collection` — see step 3) from
   `request.app.state` — wipes SQLite, Chroma, and Neo4j (see "Shared"
   sections above for each store's own `reset_all()`)

   **Branch point**: a partial failure (`AdminError`) is caught here and
   returned as a 500 `JSONResponse({"error": "admin_error", "detail":
   ...})` naming exactly which store(s) failed, rather than propagating to
   `api/main.py`'s generic handler
3. `storage/chroma_store.py::reset_all()` deletes and recreates the whole
   Chroma collection (not just its documents — see DECISIONS.md,
   2026-08-30, "`reset_all()`'s Chroma reset never actually cleared the
   dimension lock") and returns the fresh `Collection`; this route
   replaces `request.app.state.collection` with it, since the old object
   is no longer valid once its underlying collection is deleted — every
   later request in this process (query, ingestion) reads
   `app.state.collection` fresh each time, so this swap is what keeps
   them from hitting a stale/deleted collection
4. Returns `{"status": "reset"}` on full success

---

## Entry point: `/api/setup/*` (`api/routes/setup.py`)

Extension beyond `docs/API_Specification.docx` — the guided first-run
setup wizard, issue #92. Google OAuth (phase 1) and credential
validate/save (phase 2) are both backend-complete; the frontend wizard
shell itself, and local files/browser history, are not built yet. See
DECISIONS.md, 2026-09-02 (both entries).

1. `POST /setup/google/oauth/start` — request body validated against
   `api/schemas.py::GoogleOAuthStartRequest` (`client_id`, `client_secret`).
   Builds `redirect_uri` from the *inbound request itself*
   (`request.url_for("google_oauth_callback_route")`) — correct for
   whatever host/port this request actually arrived on, dev or Docker,
   with nothing to configure separately. Calls
   `agent/google_oauth.py::start_authorization()` (thin wrapper over
   `extractors/google_oauth.py`, the real implementation — see that
   module's own docstring for why the real logic can't live in `agent/`):
   saves the credentials to `config/.env`
   (`config/settings.py::update_google_oauth_config()`), builds the
   Google consent URL via `google_auth_oauthlib.flow.Flow` (requesting
   both Gmail-readonly and Calendar-readonly scopes in one screen — see
   DECISIONS.md), and tracks the pending flow in-memory keyed by its
   `state`. Returns `{"authorization_url": str}` — the wizard opens this
   in a new tab/popup, it doesn't navigate there itself.

   **Branch point**: empty/malformed credentials or a Google-side
   rejection raise `GoogleOAuthError`, mapped by `api/main.py`'s
   dedicated handler to `400` (bad input, never this server's fault) —
   distinct from every other unregistered exception's `500`.

2. `GET /setup/google/oauth/callback` (registered as
   `name="google_oauth_callback_route"`, so `request.url_for()` above can
   resolve it) — Google's own redirect target, hit by the user's browser
   navigating there directly, never by the frontend's `fetch()` calls.
   Always returns HTML (`_callback_page()`, a tiny inline template), not
   JSON, regardless of outcome, since there's no JSON-consuming caller on
   this request. **Branch points**, in order: a `?error=...` query param
   (user denied consent) → failure page immediately; a missing `state` →
   failure page; otherwise calls
   `agent/google_oauth.py::complete_authorization(state, authorization_response=str(request.url))`
   — looks up the pending flow by `state` (a stale/forged/already-used
   callback raises `GoogleOAuthError`, rendered as a failure page, not a
   500, since `GoogleOAuthError` is caught directly in this route rather
   than left to propagate), exchanges the code via
   `Flow.fetch_token()`, and writes the resulting credentials to
   `extractors/google_oauth.py::token_path()`
   (`data/google_oauth_token.json` — a Docker-volume-backed path, see
   DECISIONS.md issue #52). Success renders a plain "connected, close
   this tab" page.

3. `GET /setup/google/oauth/status` → `agent/google_oauth.py::is_connected()`
   (a cheap on-disk check — does the shared token file exist) → `{"connected": bool}`.
   Polled by the wizard's own tab while the popup from step 1 is open,
   since the actual callback in step 2 lands in that separate tab/window,
   not the one the wizard itself is running in.

4. `extractors/gmail.py`'s/`extractors/calendar.py`'s own
   `_get_credentials()` read this same shared token
   (`extractors/google_oauth.py::load_credentials()`) as their first
   choice on every later ingestion run, falling back to each extractor's
   own older, separate per-service token file if the guided flow was
   never completed — see the `scheduler/daily_batch.py` entry point above
   and DECISIONS.md, 2026-09-02.

5. `POST /setup/validate` — request body validated against
   `api/schemas.py::SetupValidateRequest` (`source`: one of
   `"openrouter"`/`"notion"`/`"github"`, `value`: the raw pasted
   credential). Dispatches to `agent/connection_check.py`'s
   `openrouter_key_works()`/`notion_key_works()`/`github_token_works()` —
   each makes one real, cheap API call with the *given* value directly,
   never reading from or writing to `config/.env` at all. Returns
   `{"status": "ok"|"error", "detail": str}`. See DECISIONS.md, 2026-09-02
   ("Validate-before-save credentials..."), including the dispatch-dict
   bug found and fixed there (must resolve the validator function by
   plain name at call time, not through a dict built once at import
   time, or a test's monkeypatch on that name silently has no effect).

6. `POST /setup/credentials` — request body validated against
   `api/schemas.py::SetupCredentialsRequest` (`notion_api_key`,
   `github_token`, `openrouter_api_key`, all optional). Calls
   `config/settings.py::update_credentials_config()` — the first update
   endpoint these three credentials have ever had; previously only
   editable by hand in `config/.env`. Does no validation of its own — the
   wizard always calls step 5 first and only reaches this on a confirmed
   `"ok"` result. Returns `{"status": "updated"}`; a `ConfigError` maps to
   `500` via the shared handler.

---

## Entry point: `GET /api/graph` (`api/routes/graph.py`)

Extension beyond `docs/API_Specification.docx` — the relationship graph
wasn't in the original spec. Requested directly for the frontend graph
view (issue #57). See DECISIONS.md, 2026-08-30.

1. Reads `driver` from `request.app.state`
2. `agent/graph_view.py::get_graph_snapshot()` — a thin pass-through to
   `storage/neo4j_store.py::get_full_graph()` (see "Shared: Neo4j storage"
   above)
3. `GraphSnapshot` reshaped into `api/schemas.py::GraphResponse`
   (`nodes: [{id, title, source_type, url}]`, `edges: [{source_id,
   target_id, label, confidence}]`) and returned — an empty graph (no
   relationships confirmed yet) returns `{"nodes": [], "edges": []}`, not
   an error

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

## Entry point: `POST /api/ingest/cancel` (`api/routes/ingest.py`)

Extension beyond `docs/API_Specification.docx` — see DECISIONS.md,
2026-08-31 (issues #72, #73) and 2026-09-01 (the force-kill entry — the
cooperative-flag-only design below turned out to have a real gap once a
source's extraction phase finishes; see that entry for why).

1. Request body `{"run_id": <ingestion_runs.id>}` — the real UUID from
   `GET /api/sources/status`'s `last_run.run_id`, **not**
   `POST /api/ingest/trigger`'s display-label return value; the two are
   different strings for the same run
2. `agent/ingest_trigger.py::cancel_ingestion(request.app.state.conn, run_id)`:
   a. Still sets `ingestion_runs.cancel_requested = 1`
      (`storage/sqlite_store.py::request_ingestion_cancellation()`) — cheap,
      harmless to keep, and still useful if the process happens to hit a
      check point first
   b. `storage/sqlite_store.py::get_ingestion_run(conn, run_id)` — reads the
      run's recorded `pid` (set by the batch process itself, via
      `start_ingestion_run()`, at row-creation time — see that entry
      point's step 1 below). If the run isn't found, or isn't
      `status == "running"` anymore, stops here — nothing to kill,
      nothing to finalize
   b'. If `pid` is `None` (a legacy run predating that column), stops here
      too — with no way to confirm the process has actually stopped,
      finalizing as `"cancelled"` would misrepresent reality; only the
      cooperative flag from step 2a is left in place. See DECISIONS.md,
      2026-09-01 ("cancel_ingestion() no longer finalizes... on an
      unconfirmed kill")
   c. `os.kill(pid, signal.SIGTERM)` — on Windows this maps to
      `TerminateProcess()`, an immediate forceful kill, not a catchable
      signal. A `ProcessLookupError` (already exited) is caught and
      treated as a confirmed stop; any other `OSError` is caught, logged,
      and treated as an *unconfirmed* kill — stops here without
      finalizing, same reasoning as step b'
   d. Only on a confirmed kill: re-reads the run, and only if it's
      *still* `"running"` (guards a race against the batch legitimately
      finishing in the gap between b and c) calls
      `complete_ingestion_run(status="cancelled", ...)` directly — a
      force-killed process can never call this itself on its way out
3. Returns `200 {"status": "cancel_requested"}` regardless of whether the
   kill was confirmed — by the time this response is sent, a confirmed
   kill has already finalized the row (steps 2b-2d all happen
   synchronously before the response) but an unconfirmed one has not, and
   the caller finds out which by polling `GET /api/sources/status`
   afterward, unlike the earlier purely-cooperative design where this
   response meant "the flag is set, actual stopping happens whenever the
   process gets around to checking it"

---

## Entry point: `frontend/` (`npm run dev`, or `index.html` in production)

A Vite + React app — see `frontend/README.md` for setup/dev commands.
`frontend/src/index.jsx` mounts `App.jsx` into `#root`.

1. `App.jsx` holds `view` (`"chat"` | `"settings"` | `"graph"`), `sessionId`,
   the fetched `sessions` list, and a `toasts` list, and renders `Toasts.jsx`
   plus `Sidebar.jsx` plus one of `ChatWindow.jsx`, `SettingsPanel.jsx`, or
   `GraphView.jsx` — no router, since the wireframe only needs a "Settings
   link," not deep-linking (see DECISIONS.md, 2026-08-25; the graph view
   follows the same no-router pattern, see DECISIONS.md, 2026-08-30).
   Fetches `getSessions()` on mount and again after every completed turn
   (`onTurnCompleted`), so the sidebar's list and ordering stay current.
   Also owns `handleTriggerIngestion()`/`handleResetAll()` — passed down to
   `SettingsPanel.jsx` as props rather than living there, since both need
   to act (toast a result, in the ingestion case only after polling for
   completion; clear/refresh chat session state, in the reset case) even
   if the user has since navigated away from Settings — see DECISIONS.md,
   2026-08-30. Also holds `ingestionStatus`
   (`{startedAt, runId, dbRunId, itemsProcessed, status}` — `null` before
   anything's been triggered this session), set on trigger and updated on
   every poll inside `pollIngestionCompletion()`, then passed down to
   `SettingsPanel.jsx` for a persistent "started at HH:MM:SS" + live
   progress display that survives both the toast's 7s auto-dismiss and
   navigating away from Settings and back — see DECISIONS.md, 2026-08-30
   (the live-progress entry). `dbRunId` (the real `ingestion_runs.id`, as
   opposed to `runId`'s human-readable display label) is only known once
   the first poll's `getSourcesStatus()` result finds the matching row —
   `null` until then — and is what `handleCancelIngestion(dbRunId)` (also
   owned here, alongside `handleTriggerIngestion()`, same reasoning) passes
   to `postIngestCancel()`. A `status === "cancelled"` result inside
   `pollIngestionCompletion()` toasts a neutral (not error-styled)
   "Ingestion stopped — N item(s) processed" message. See DECISIONS.md,
   2026-08-31.
2. `Sidebar.jsx` — "New chat" and clicking a real session both bump
   `App.jsx`'s `chatKey`, forcing `ChatWindow` to remount (see DECISIONS.md,
   2026-08-25, for why a remount rather than watching `sessionId` for
   changes). Renders the real session list from `App.jsx`, or "No past
   conversations yet" if it's empty. "Chat", "Graph", and "Settings" sit
   together in a `.sidebar-footer` at the bottom, sharing one `.nav-link`
   style — "Chat" (added with the Figma redesign, see DECISIONS.md,
   2026-08-30) calls a new `onOpenChat` prop that only switches
   `App.jsx`'s `view`, unlike "New chat"/a session click, which also touch
   `sessionId`/`chatKey`. Each nav icon is `components/icons/NavIcons.jsx`
   (`ChatIcon`/`GraphIcon`/`SettingsIcon`) — the exact exported Figma
   asset's path geometry, inlined as SVG with `fill="currentColor"`, so
   the same shape renders in either the muted or `--accent-active` color
   depending on which tab is current, without needing two
   differently-colored exports per icon. An earlier version used `<img>`
   + CSS `mask-image` for the same effect directly from the exported SVG
   files; switched to inlining after the mask-image version didn't
   render — see DECISIONS.md, 2026-08-30
3. `ChatWindow.jsx` — on mount, if given an existing `sessionId`, calls
   `api/client.js::getSessionHistory()` once and populates messages from
   it (see DECISIONS.md, 2026-08-25). On submit, appends the user message
   and a "Thinking…" placeholder immediately, calls
   `api/client.js::postQuery(question, sessionId)`, then fills the
   placeholder in with the real answer and `SourceChip.jsx`-rendered
   sources (or an error state) once it resolves, and reports the (possibly
   new) `session_id` up to `App.jsx` via `onTurnCompleted`. The message
   list auto-scrolls to the newest message whenever `messages` changes,
   but only if the reader was already at the bottom (tracked by a ref the
   scroll handler updates continuously, not a fresh measurement taken
   after the new message is already in the DOM — see DECISIONS.md,
   2026-08-30); a floating "↓ Latest" button appears once they've
   scrolled away from the bottom, for jumping back without hand-scrolling.
   The empty state (no messages yet) renders the Figma redesign's icon +
   heading + suggestion chips instead of a plain sentence — clicking a
   chip fills the input with a related prompt rather than sending it
   immediately. Also fetches `getSourceConnections()` once on mount for
   the "Data Source Indicators" pills above the input box: one pill per
   source whose live status is `"ok"`, not a static hardcoded list — see
   DECISIONS.md, 2026-08-30
4. `SettingsPanel.jsx` — on mount, calls `api/client.js::getSettings()`,
   `getSourcesStatus()`, `getSourceConnections()`, and `getSourceConfig()`
   in parallel, snapshotting the loaded values into `lastSavedRef` — the
   baseline every later field compares its own value against before
   deciding to save. There is no "Save Changes" step: every field saves
   itself the moment it's committed — see DECISIONS.md, 2026-09-01. The
   Models section renders only the generation + embedding pair matching
   `settings.provider_mode` (`isLocal ? local_* : cloud_*`) — not all four
   fields at once — but both fields of the inactive pair are still kept in
   `settings` (round-tripped through every `GET`/`PUT /api/settings` call)
   so switching modes and back doesn't lose an edited-but-inactive value.
   See DECISIONS.md, 2026-09-01, "Local embedding via Ollama restored
   (frontend)". The two generation-model text inputs and the four
   source-scope textareas/date fields call their respective save helper
   (`saveLlmField()`/`saveScopeTextarea()`/`saveDateField()`) from
   `onBlur`, each first checking `lastSavedRef.current[key] !== value` so
   an unchanged blur (click in, click away) is a no-op. Every save helper
   follows the same shape: `putSettings()`/`putSourceConfig()` with just
   the one changed field (both endpoints are partial-update), then syncs
   `lastSavedRef` from the response so it always reflects the server's own
   truth rather than the client's assumption. Either embedding model
   field's `onBlur` calls `saveEmbeddingModel(apiKey, value)` instead of
   the generic `saveLlmField()` — it still can't be silent, since changing
   it strands every existing embedding, so it opens the same
   `useConfirm()` (`hooks/useConfirm.jsx`, rendering `ConfirmDialog.jsx` —
   this project's own centered modal, not the native `window.confirm()`)
   dialog as before with a danger-styled warning; declining reverts the
   input to `lastSavedRef.current`'s value (there's no Save button left to
   abandon an edit via otherwise). Only on confirm does it call
   `putSettings({[apiKey]: value})`, then `onResetAll()` followed by
   `onTriggerIngestion()` (the same `App.jsx`-owned flows the Danger Zone
   button and "Run ingestion now" button call directly elsewhere) and
   re-fetches `getSourcesStatus()`/`getSourceConnections()` — see
   DECISIONS.md, 2026-08-30 (the un-freeze/confirm-dialog entry) and
   2026-09-01.

   The `provider_mode` radio's own `onChange` calls `saveProviderMode(value)`
   instead — a switch between Local and Cloud is treated as the single
   most consequential action in Settings, since it changes which embedding
   model is active and the two modes' embedding spaces are never
   compatible. `saveProviderMode()` awaits **two** consecutive
   `useConfirm()` prompts in sequence (declining either aborts and reverts
   the radio pick), both passed `critical: true` — a new dialog variant
   (`ConfirmDialog.jsx`'s `critical` prop, `.confirm-dialog.critical` /
   `.confirm-backdrop.critical` in `index.css`) that renders the entire
   dialog on a solid red background, more alarming than the plain
   `danger`-styled button variant every other confirm in this file uses.
   Only after both confirms does it call `putSettings({provider_mode})`,
   then `onResetAll()` — but, unlike the embedding-model flow above,
   deliberately stops there: no `onTriggerIngestion()` call, so a mode
   switch never starts a run on its own. See DECISIONS.md, 2026-09-01,
   "Local embedding via Ollama restored (frontend)". Each
   connected-source card shows the live connection status
   (`getSourceConnections()`'s cache-or-fresh result) rather than the
   batch-run status alone; "Reverify" calls `verifySourceConnections()`
   (force-refreshes, bypassing the cache) and replaces the connections
   state with the fresh result — see DECISIONS.md, 2026-08-29 and
   2026-08-30. The Danger Zone's own "Reset all data" button and "Run
   ingestion now" each still have their own direct call path (with their
   own confirm, for the reset button) into the same `onResetAll`/
   `onTriggerIngestion` props — the embedding-model-triggered path above
   is an additional caller, not a replacement. "Browse…" next to the local
   folders field calls `postBrowseFolder()`; a non-null result is appended
   as a new line in `watchDirsText` (skipped if already present), composing
   with manually pasted paths rather than replacing them, then calls
   `saveScopeTextarea()` explicitly — a path picked this way never fires
   the textarea's own `onBlur`, since the user never focused it — see
   DECISIONS.md, 2026-08-30 and 2026-09-01. **Branch point**: when
   `sources.running_in_docker` is true (see `GET /api/settings/sources`
   above), "Local folders to watch" renders read-only instead — a plain
   list built from the same `watchDirsText` state, no "Browse…" button, no
   editable textarea, with a hint pointing at `HOST_WATCH_DIR` + restarting
   the stack as the way to actually change it (see DECISIONS.md,
   2026-09-02). The "Data Ingestion" section also renders the
   `ingestionStatus` prop (from `App.jsx`, see above), when non-null, as a
   "Started at HH:MM:SS" line plus a progress bar — an indeterminate
   sliding-segment animation while `status === "running"` (no known total
   item count to compute a real percentage from — extraction discovers
   items as it streams through each source), filled solid green/red once
   the run finishes (solid blue for `"cancelled"` — neutral, not styled as
   an error); the live `items_processed` count is shown as text alongside
   it, and while `status === "running"`, `currentItem` renders below as a
   monospace line (e.g. `"github: owner/repo (12/65)"`, from the
   `on_progress` callback wired through `scheduler/daily_batch.py` — see
   DECISIONS.md, 2026-08-31). See DECISIONS.md, 2026-08-30 (the
   live-progress entry). A "Stop" button (`settings-ghost-button-danger`)
   renders next to "Run ingestion now" only while `status === "running"`
   and `dbRunId` is known; clicking it awaits the same `useConfirm()`
   dialog used elsewhere in this component, then calls
   `onCancelIngestion(dbRunId)` (`App.jsx`'s `handleCancelIngestion`) —
   the already-active poll loop picks up the resulting `"cancelled"`
   status on its own, no separate re-fetch needed. See DECISIONS.md,
   2026-08-31.
   Per the Figma redesign (see DECISIONS.md, 2026-08-30, "screen 2 of
   3"), the sections render inside two explicit `.settings-column`
   containers in a real CSS Grid (`.settings-columns`), not an
   auto-balancing multi-column layout — the design originally grouped
   *specific* sections into *specific* sides (Generation Provider/Models
   on the left, Local folders/Notion scope/Data Ingestion/Danger Zone on
   the right); Danger Zone was later moved into the left column
   (Generation Provider/Models/Danger Zone) per direct request, so the
   DOM no longer matches the design's own grouping exactly, but the
   two-explicit-columns structure itself is unchanged. "Data
   Ingestion" and "Connected Data Sources" render as one merged card with
   an internal divider (`.settings-section-header-bordered`) rather than
   two separate cards — same two handlers/states as before, just one DOM
   boundary instead of two. A transient save-status line (`.save-action`,
   "Saving…" / "Saved." / an error) sits in `.settings-header`, top-right
   next to the page title, in place of the "Save Changes" button removed
   2026-09-01 — the same spot the button used to occupy.
5. `Toasts.jsx` — a pure display component: renders whichever
   `{id, kind, text}` entries are in `App.jsx`'s `toasts` state as a
   fixed top-right stack, each auto-removed after 7 seconds
   (`setTimeout` in `App.jsx::addToast()`) or dismissed early by its own
   close button. `App.jsx::pollIngestionCompletion()` is the one caller
   that polls before toasting — every other caller (`handleResetAll()`,
   `SettingsPanel.jsx`'s own error paths via the `onError` prop) toasts
   immediately, since those results are already known synchronously. See
   DECISIONS.md, 2026-08-30
6. `GraphView.jsx` — on mount, calls `api/client.js::getGraph()`; an empty
   result (no confirmed relationships yet) renders an explanatory message
   rather than a blank canvas. A non-empty result drives a **live**
   `d3-force` simulation (`forceSimulation`/`forceLink`/`forceManyBody`/
   `forceCenter`, never `.stop()`-ped) — Obsidian-style, not a one-shot
   static layout (see DECISIONS.md, 2026-08-30, the interactive-graph
   entry, for why that changed). The canvas fills whatever space is
   available rather than a fixed 900x600 box: a `ResizeObserver` on the
   canvas element drives both the SVG `viewBox` and the simulation's
   `forceCenter`, updated on every resize via a separate effect that
   nudges the existing simulation (`alpha(0.3)`, not `.restart()`) so
   nodes drift toward the new center instead of snapping — see
   DECISIONS.md, 2026-08-30 (the full-screen-graph entry). The
   simulation's own node/link objects live in `nodesRef`/`linksRef`
   (mutated in place by both d3's tick loop and the drag handlers below);
   a `nodes`/`links` **state** pair,
   shallow-copied from those refs on every `"tick"` event, is what JSX
   actually renders from — reading a ref directly during render doesn't
   trigger a re-render and trips this project's `react-hooks/refs` lint
   rule. Dragging a node (`onPointerDown`/`onPointerMove`/`onPointerUp` on
   its `<g>`, using pointer capture so the drag tracks even off the small
   circle) converts the pointer's screen position into the SVG's own
   `viewBox` coordinate space (`getScreenCTM().inverse()`) and sets that
   node's `fx`/`fy`, while `simulation.alphaTarget(0.3).restart()`
   "reheats" the layout so neighbors visibly respond; releasing clears
   `fx`/`fy` back to `null` and cools the simulation down again
   (`alphaTarget(0)`). Nodes are colored by `source_type`, edges labeled
   with their relationship `label`. A node's label text is truncated to 32
   characters (`truncateLabel()`) — long GitHub titles otherwise overlap
   and clutter the layout — with the full title available via a native
   SVG `<title>` hover tooltip. See DECISIONS.md, 2026-08-31. A "Filter"
   button in the toolbar (top-right, alongside zoom/fit) opens a floating
   checkbox panel (`graph-view-filter-panel`, closes on an outside-click
   `pointerdown` listener) listing every `source_type`
   except `browser_history` — that source is excluded from relationship
   detection entirely (`pipeline/relationships.py`, see CLAUDE.md), so a
   node for it can never exist on this graph; listing it would promise
   something that can never appear. See DECISIONS.md, 2026-08-30. This
   replaced an earlier bottom-of-canvas legend whose swatches doubled as
   filter toggles — same underlying state (`visibleSourceTypes`, a `Set`,
   toggled via `toggleSourceType()`) and filtering behavior (hides, not
   dims, non-matching nodes/edges at render time only, never touching the
   running simulation), just moved into the toolbar and given a visible,
   badge-counted button ("Filter (3/5)") plus "All"/"None" quick actions,
   since the old legend read as decoration rather than a control — see
   DECISIONS.md, 2026-09-01. Pan/zoom is a hand-rolled `{x, y, k}` transform applied via
   a wrapping `<g transform="translate(x,y) scale(k)">` (wheel to zoom
   anchored at the cursor, toolbar `+`/`−` buttons, dragging the
   background to pan) — `toSimulationPoint()` now inverts both the SVG's
   own `viewBox` scaling *and* this transform, in that order, so node
   dragging still lands in the same raw simulation space regardless of
   zoom level. Clicking a node (vs. dragging it — distinguished by
   screen-pixel movement between pointerdown/pointerup, threshold 4px)
   sets `selectedNodeId`; the selected node, its one-hop neighbors, and
   the edges between them render at full opacity, everything else dimmed.
   `fitToView()` computes the bounding box of the currently-visible node
   positions and sets the transform to center/contain it — run once
   automatically when the simulation's `"end"` event fires (layout
   settled), again whenever the filter set changes, and on demand via a
   "Fit view" button. See DECISIONS.md, 2026-08-31.
7. `api/client.js` is the only module making network calls (per
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
