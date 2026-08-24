# Implementation Decisions

A running log of meaningful decisions made **during coding** that were not
already settled in `/docs`. Newest entries at the top.

Decisions already locked in by the design document set are not repeated here —
see `CLAUDE.md` and the documents in `docs/` for those.

---

## 2026-08-24 — Both concrete providers share one LangChain-backed adapter

**Context**: `Technical_Design_Document.docx` section 12.3 specifies the
provider/adapter pattern and its three-method contract
(`generate_metadata`/`generate_relationship`/`generate_answer`), and
`File_Folder_Structure.docx` lists `local_provider.py` (Ollama) and
`openrouter_provider.py` as separate modules, but neither says how much
those two implementations should actually share. In practice they're
identical: OpenRouter is used specifically because it's OpenAI-API-compatible
and needs no vendor-specific handling, so the only real difference between
"local" and "cloud" here is which chat model backs the calls.

**Decision**: `providers/base.py` defines one concrete class,
`LangChainProvider`, that implements all three interface methods against
any LangChain `BaseChatModel` (prompt building, JSON response parsing, and
retry-with-backoff all live here, once). `local_provider.py` and
`openrouter_provider.py` are reduced to a single factory function each —
`create_local_provider()` / `create_openrouter_provider()` — that resolve
config/env values and construct a `ChatOllama` / `ChatOpenAI` (pointed at
`https://openrouter.ai/api/v1`) to hand to `LangChainProvider`. Using
LangChain's chat model classes specifically (rather than the raw `ollama`
or `openai` SDKs) also keeps the door open for LangSmith tracing later
(`langsmith_api_key`/`langsmith_project` are already in `EnvSettings`) with
no extra instrumentation code, matching the project's stated LangChain/
LangGraph/LangSmith depth goal.

**Alternatives considered**:
- *Two fully separate hand-rolled classes* — rejected; it would duplicate
  every prompt, every parser, and the retry logic itself for no behavioral
  difference, and any prompt-wording fix would need to land in two places.
- *Raw `ollama` + `openai` SDKs instead of LangChain chat models* — rejected;
  LangChain's chat model interface (`.invoke()`, `.content`) is already
  vendor-agnostic in the same way `ProviderInterface` itself is, so building
  on it avoids re-solving that problem, and it's free LangSmith
  instrumentation for later.

**Affects**: `providers/base.py`, `providers/local_provider.py`,
`providers/openrouter_provider.py`

---

## 2026-08-24 — Provider retries and structured JSON prompting live in the shared adapter

**Context**: `Coding_Conventions.docx` section 2.4 requires LLM provider
calls to "use retry with exponential backoff for transient errors, and raise
`ProviderError` after retries are exhausted," but doesn't specify retry
counts/delays (not present in `config.yaml` either), and no document
specifies a response format for `generate_metadata`/`generate_relationship`.

**Decision**: `_retry_with_backoff()` retries up to 3 times with delays of
1s/2s/4s, and treats *any* exception from the wrapped call as retryable —
including a `json.JSONDecodeError` or a `ValueError` from response
validation, not just transport errors. `generate_metadata` and
`generate_relationship` prompt for a strict JSON response (an array of
`{"project_name", "topic"}` objects for metadata; a single
`{"related": bool, "label"?, "confidence"?}` object for relationships) and
parse it inside the retried call, so a one-off malformed response from the
model gets a fresh attempt rather than immediately becoming a
`ProviderError`. `generate_answer` prompts the model to cite context by
position (`[1]`, `[2]`, …) so a caller (the eventual `agent/synthesizer.py`)
can resolve those markers against the `ContextChunk`s it passed in, without
this layer needing to know how citations are rendered in the final API
response.

**Alternatives considered**:
- *Only retry on transport/connection errors, fail fast on parse errors* —
  rejected; LLM output is stochastic, so a malformed JSON response is
  plausibly fixed by simply asking again, and the ingestion tasks calling
  this (`generate_metadata`/`generate_relationship`) run unattended in the
  daily batch where a transient bad response shouldn't abort the item.
- *Have the caller retry instead of the provider* — rejected; every future
  caller (pipeline stages, the synthesizer) would need to reimplement the
  same backoff loop, exactly the duplication the `ProviderInterface`
  abstraction exists to avoid.

**Affects**: `providers/base.py`

---

## 2026-08-24 — Neo4j tests run against a real local container, skipped if none is reachable

**Context**: SQLite has `:memory:` and Chroma has an embedded persistent
mode, so both could be tested for real with no external process. Neo4j
Community Edition has no embedded/in-process mode — the driver only ever
speaks Bolt to a running server, and none was reachable in this environment
by default (Docker Desktop's daemon was running but its pipe wasn't ready
yet). `Coding_Conventions.docx` doesn't cover how to test a module whose
only backing store requires a live external process.

**Decision**: `tests/test_storage/test_neo4j_store.py` is a real integration
suite (no mocking) run against `NEO4J_TEST_URI`/`_USER`/`_PASSWORD` (default
`bolt://localhost:7687` / `neo4j` / `testpassword123`, matching a
disposable `docker run neo4j:5-community` instance), gated by a
module-level `pytest.mark.skipif` that probes connectivity once at
collection time. The suite runs for real wherever a server is reachable
(this session's temporary container, a developer's local Neo4j install, or
a CI job that starts one as a service) and skips cleanly everywhere else,
rather than either failing hard with no server or silently mocking away the
one thing most worth testing — the actual Cypher.

**Alternatives considered**:
- *Mock the driver* — rejected; the value in these tests is verifying the
  Cypher itself (the upsert-both-endpoints `MERGE`, the same-label edge
  dedup, the two-direction `get_related_items` union), which a mock can't
  catch a mistake in.
- *Require Neo4j and fail without it* — rejected; it would make `pytest`
  fail on any machine that hasn't set up Neo4j yet, including this one
  before Docker Desktop's daemon had finished starting.
- *Use a fake/in-memory Cypher engine* — rejected; no actively maintained
  one exists for the neo4j Python driver, and Community Edition being
  genuinely local-only (Tech_Stack.docx) means the real server is always
  the honest target to test against anyway.

**Affects**: `tests/test_storage/test_neo4j_store.py`

---

## 2026-08-24 — Neo4j temporal properties are converted back to stdlib `datetime`

**Context**: `ItemNode.created_at` and `Relationship.created_at` are typed
`datetime | None`, but the neo4j driver returns its own temporal type,
`neo4j.time.DateTime`, for any property read back from a node or
relationship — not `datetime.datetime`. Left unconverted, that silently
violates the declared type and would break any caller doing stdlib
`datetime` operations or JSON serialization on the result (found in Copilot
review of PR #4).

**Decision**: A `_to_datetime()` helper calls `neo4j.time.DateTime`'s own
`.to_native()` method (a no-op for anything that's already a stdlib
`datetime`, so it's safe to apply unconditionally to whatever a driver call
returns) and is used everywhere a temporal property comes back from a
record — `_item_from_node()` and the `Relationship` built in
`get_related_items()`. Covered by
`test_created_at_round_trips_as_a_stdlib_datetime`, which asserts
`type(...) is datetime` rather than just equality, since
`neo4j.time.DateTime` compares equal to an equivalent stdlib `datetime`
despite being the wrong type.

**Affects**: `storage/neo4j_store.py`

---

## 2026-08-24 — `get_driver()` closes the driver if `verify_connectivity()` fails; test suite refuses to wipe a non-local database

Two smaller PR #4 review fixes, bundled since both are narrow correctness
fixes to the same file with no real alternatives considered:

- `get_driver()` previously left the newly constructed driver open if
  `verify_connectivity()` raised, leaking a socket/thread pool on every
  failed connection attempt in a long-running process (e.g. retrying a
  misconfigured URI). It now closes the driver before re-raising.
- `tests/test_storage/test_neo4j_store.py`'s fixture runs
  `MATCH (n) DETACH DELETE n` before and after every test. That's fine
  against the disposable local container it's designed for, but nothing
  previously stopped `NEO4J_TEST_URI` from pointing at a real instance and
  having the suite silently destroy it. The suite now refuses to run
  (`pytest.skip` at module level) against any non-localhost host unless
  `NEO4J_TEST_ALLOW_NONLOCAL_WIPE=1` is explicitly set.

**Affects**: `storage/neo4j_store.py`, `tests/test_storage/test_neo4j_store.py`

---

## 2026-08-24 — SQLite item inserts upsert on `(source_type, source_ref_id)`, returning the effective id

**Context**: `Database_Schema.docx` defines `UNIQUE(source_type, source_ref_id)`
on `items` to "prevent the same source item from being ingested twice," but
doesn't say what should happen when a previously-ingested item is edited at
the source (e.g. a Notion page revised, a local file re-saved) and the daily
batch picks it up again via `extract_new_items(since=...)`. A plain
`INSERT` would raise on the unique constraint; `INSERT OR IGNORE` would
silently keep stale content forever.

**Decision**: `insert_item()` upserts via
`ON CONFLICT(source_type, source_ref_id) DO UPDATE SET ...`, updating every
column except `id` — so a re-ingested edit overwrites the existing row in
place rather than being rejected or ignored. Since the conflict update never
touches `id`, an existing row keeps its original id even though the caller
passed a freshly generated one for what it believed was a new item. To keep
that consistent, `insert_item()` returns the *effective* item id (looked up
by `source_type, source_ref_id` after the upsert) rather than `None`; callers
must use the returned id — not `item.id` — when inserting that item's chunks
via `insert_chunk()`/`replace_chunks()`. A companion `replace_chunks()`
helper deletes an item's existing chunks before inserting the new set in one
transaction, since an edited item's old chunk boundaries and FTS entries are
no longer valid once chunking is re-run over the updated text.

**Alternatives considered**:
- *`INSERT OR IGNORE`* — rejected; a source item edited after its first
  ingestion would never be reflected in storage, silently going stale.
- *Raise on conflict and require the pipeline to call an explicit
  `update_item()`* — rejected as needless ceremony; the pipeline layer
  doesn't know in advance whether a given source ref is new or a re-ingested
  edit, so it would have to catch the conflict and branch anyway.
- *Let the conflict update also overwrite `id`* — rejected; `chunks.item_id`
  is a foreign key, and rewriting `items.id` out from under existing chunk
  rows would either orphan them or require a cascading id rewrite with no
  benefit over just keeping the original id.

**Affects**: `storage/sqlite_store.py`

**Note**: FTS5 keyword search (`chunks_fts`) uses external-content sync
triggers (`chunks_ai`/`chunks_ad`/`chunks_au`) since `Database_Schema.docx`
defines the virtual table but not how it stays in sync with `chunks`. This is
the standard SQLite-documented pattern for `content=` FTS5 tables, not a
deviation worth its own entry.

---

## 2026-08-14 — Blank `.env` values mean "unset", and relative paths anchor to the repo root

**Context**: Two problems surfaced in review of the scaffolding PR, both of
which every fresh setup would have hit, because `config/.env.example` ships each
variable blank and points `SQLITE_DB_PATH` at a relative `./data/...`:

1. A blank entry was read as a configured value, not an absent one. Copying
   `.env.example` gave `NOTION_API_KEY == ""` and, worse,
   `GMAIL_CREDENTIALS_PATH == Path(".")` — since `Path("")` normalizes to the
   current directory. Any caller checking `is None` would treat an unconfigured
   source as configured and fail later, further from the cause.
2. Relative paths resolved against the process working directory. The daily
   batch is launched by cron or Task Scheduler, whose working directory is not
   the repository root, so it would have read and written a *different* SQLite
   file than a manual run from the repo root.

**Decision**:

- A `model_validator(mode="before")` on `EnvSettings` drops blank and
  whitespace-only values from the input, so normal field defaults apply. Blank
  therefore behaves exactly like omitted, for optional secrets (which stay
  `None`) and for defaulted values like `OLLAMA_HOST` alike.
- An `anchor_path()` helper resolves every path-typed setting against
  `PROJECT_ROOT` when relative, leaving absolute paths as given. It is applied
  to all five path fields and to the `watch_dirs` property.

**Alternatives considered**:
- *Require absolute paths and validate loudly* — rejected as hostile to setup;
  `./data/pkg_agent.db` is the natural thing to write, and it is unambiguous as
  long as the anchor is defined.
- *Anchor to the CWD and document it* — rejected because it makes correctness
  depend on how the process was launched, which is precisely what breaks under a
  scheduler.
- *Handle blanks at each call site* — rejected; it pushes the same defensive
  check into every consumer of every optional variable.

**Affects**: config/settings.py, config/.env.example

---

## 2026-08-14 — Configuration loader lives in `config/settings.py`

**Context**: The documented repository layout (`File_Folder_Structure.docx`)
gives `config/` only two files — `config.yaml` and `.env.example` — and does not
say which module is responsible for reading them. The storage layer needs
`SQLITE_DB_PATH`, `CHROMA_PERSIST_DIR`, and the Neo4j credentials before any
other layer exists, so something had to own configuration loading immediately.

**Decision**: Add `config/settings.py`, exposing a cached `get_settings()` that
returns a `Settings` object with two halves: `settings.env` (pydantic-settings
`BaseSettings` reading `config/.env` and the process environment) and
`settings.config` (pydantic models mirroring `config.yaml`'s structure). Every
`.env` field is optional with a sensible default, so a partially configured
machine still loads and each component validates only the variables it actually
needs. `reload_settings()` clears the cache, which `PUT /api/settings` will need
later.

**Alternatives considered**:
- *Read config inside `storage/`* — rejected because the pipeline, agent, and
  API layers all need configuration too; it would have forced them to import
  from storage for unrelated reasons.
- *A new top-level `core/` package* — rejected because it adds a folder that is
  not in the documented tree, for a single module.
- *Plain `os.getenv()` at each call site* — rejected because it gives no
  validation, no typing, and no single place to see what the system reads.

`config/` was chosen because configuration is already its documented concern and
because the module is a true leaf: it imports nothing from any other layer, so
every layer can depend on it without breaking the one-way dependency rule in
`Component_Map.docx`.

**Affects**: config/settings.py, config/__init__.py

---

## 2026-08-14 — Flat top-level packages, no `src/` layout and no build backend

**Context**: `uv init` offers a packaged (`src/`) layout, but
`File_Folder_Structure.docx` specifies flat top-level packages (`extractors/`,
`pipeline/`, `storage/`, …) at the repository root.

**Decision**: Follow the documented flat layout and treat the repository as an
application rather than a distributable library — `pyproject.toml` therefore has
no `[build-system]` section, and `uv sync` installs only the dependencies. Test
imports resolve via `pythonpath = ["."]` in `[tool.pytest.ini_options]`.

**Alternatives considered**: Adding a build backend and installing the project
in editable mode — rejected as unnecessary ceremony for a single-user
application that is never published to an index.

**Affects**: pyproject.toml

---

## 2026-08-14 — Initial commit on `main` as the repository baseline

**Context**: `CLAUDE.md` states that `main` is protected and that all work
reaches it through a pull request, but a repository needs a first commit before
any branch can be created from it.

**Decision**: One bootstrap commit on `main` containing only the pre-existing
design documents, `CLAUDE.md`, `AGENTS.md`, and `.gitignore` — no source code.
Every subsequent change, starting with `chore/project-scaffolding`, follows the
documented branch-and-PR workflow.

**Affects**: repository history only
