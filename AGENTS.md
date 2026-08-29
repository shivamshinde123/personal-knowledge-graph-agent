# AGENTS.md

This file gives any coding agent (Codex, Claude Code, or similar) the context needed to work correctly on this repository. Read this in full before making changes. If you are Claude Code specifically, also see `CLAUDE.md` at the repository root — the two files are kept in sync, `CLAUDE.md` carries no additional instructions beyond what's here.

## Project Summary

**Personal Knowledge Graph Agent** — a fully local, privacy-first system that continuously ingests a single user's personal data from six sources (Local Files, Notion, Gmail, GitHub, Google Calendar, Browser History), embeds and links it across sources, and answers natural language questions about it through a LangGraph agent. Built primarily as a portfolio project demonstrating applied depth in LangChain, LangGraph, and LangSmith, and as a genuinely useful personal tool.

This is a single-user, local-only system. There is no multi-tenant, cloud-hosted, or authentication layer, by design.

## Reference Documents

The full design decision history lives in `/docs`. Consult the relevant document before making an architectural change rather than re-deciding something already settled:

| Document | Covers |
|---|---|
| `Technical_Design_Document.docx` | Full decision history and reasoning for every architectural choice |
| `Product_Requirement_Document.docx` | Problem statement, goals, user stories, scope, success metrics, risks |
| `System_Architecture_Document.docx` | Layers, data flow diagrams, deployment model, non-functional requirements |
| `Data_Extraction_Specification.docx` | Exact fields extracted per source, filtering rules |
| `UIUX_Wireframes.docx` | Chat interface and settings screen layout and behavior |
| `File_Folder_Structure.docx` | Full repository layout and folder responsibilities |
| `Component_Map.docx` | Component-to-component dependency rules |
| `Database_Schema.docx` | SQLite DDL, Chroma collection schema, Neo4j node/edge schema |
| `API_Specification.docx` | All FastAPI endpoints, request/response shapes |
| `Environment_Config_Reference.docx` | Every env variable, `config.yaml` structure, setup checklist |
| `Coding_Conventions.docx` | Python/React style, naming, error handling, testing, commit conventions |
| `Tech_Stack.docx` | Full stack with rationale and rejected alternatives |

If a task requires a decision not covered in these documents, make the decision, follow the conventions below for recording it, and note in your output that it extends the existing design.

## Architecture at a Glance

Six layers, each depending only on the layer below it:

1. **Data Sources** — external systems (Notion, Gmail, GitHub, Google Calendar, OS filesystem, browser)
2. **Ingestion Layer** — extractors, noise filtering, chunking, metadata generation; runs once daily as a scheduled batch
3. **Understanding & Storage Layer** — embeddings, relationship detection; SQLite + Chroma + Neo4j Community Edition, all local
4. **Query & Reasoning Layer** — LangGraph agent (Router, Vector Search, Keyword Search, Graph Traversal, Merger, Synthesizer)
5. **Interface Layer** — FastAPI backend (`localhost:8080`) + React chat frontend
6. **Evaluation Layer** — LangSmith tracing, datasets, evaluators

Full diagrams are in `System_Architecture_Document.docx`.

## Tech Stack Quick Reference

LangGraph, LangChain, LangSmith · FastAPI · React · SQLite (+ FTS5) · Chroma (embedded, local) · Neo4j Community Edition (local) · sentence-transformers (local embeddings) · Ollama (local LLM) · OpenRouter (cloud LLM, provider-agnostic) · cron / Task Scheduler for the daily batch · uv (Python package/dependency management).

Full rationale for each choice, and rejected alternatives, are in `Tech_Stack.docx` — read it before proposing a stack change.

## Repository Structure

Follow the layout defined in `File_Folder_Structure.docx`. Summary:

```
pkg-agent/
├── extractors/      # one module per data source
├── pipeline/        # filtering, chunking, metadata, embeddings, relationships
├── storage/         # sqlite_store.py, chroma_store.py, neo4j_store.py + schema/
├── providers/        # LLM provider abstraction (base, local, openrouter)
├── agent/            # LangGraph node wiring
├── api/               # FastAPI app, routes, schemas
├── frontend/          # React app
├── eval/               # LangSmith evaluation runner + test question set
├── scheduler/          # daily_batch.py entrypoint
├── config/              # config.yaml, .env.example
├── tests/                # mirrors source tree
└── docs/                  # design document set (this file's companions)
```

**Dependency direction is one-way**: Extractors → Pipeline → Storage. Agent depends on Storage + Providers only, never on Extractors or Pipeline. Interface depends only on Agent's entrypoint. Never introduce a reverse or circular dependency between these layers — see `Component_Map.docx` for the full dependency rule set.

## Coding Conventions (summary — full detail in `Coding_Conventions.docx`)

- Python: PEP 8, `black` formatting, type hints and Google-style docstrings required on all public functions.
- Python dependency and environment management uses **uv**, exclusively — never `pip install` directly, never `poetry`, never a manually managed `venv`. See Python Environment (uv) below.
- Naming: `snake_case` functions/files, `PascalCase` classes, `UPPER_SNAKE_CASE` constants.
- All LLM calls go through `providers/base.py`'s `ProviderInterface` — never call Ollama or OpenRouter directly from elsewhere.
- Each extractor catches and logs its own errors so one source failing does not block the other five during the daily batch.
- React: functional components + hooks only, `PascalCase` filenames matching the component, all API calls centralized in `frontend/src/api/client.js`.
- Commits follow Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`) — see Git & Commit Practices below for commit frequency and attribution rules.
- Tests live under `tests/`, mirroring the path of the module under test.

### Python Environment (uv)

This project uses [uv](https://docs.astral.sh/uv/) as the sole Python package manager and environment manager. Do not use `pip install`, `poetry`, `pipenv`, or a manually created `venv` anywhere in this repo.

- **Dependency files**: `pyproject.toml` declares dependencies; `uv.lock` is the committed lockfile (replaces `requirements.txt` — do not generate or maintain a `requirements.txt`).
- **Initial setup**: `uv sync` — creates the virtual environment and installs exact locked dependencies in one step.
- **Adding a dependency**: `uv add <package>` (or `uv add --dev <package>` for dev-only tools like `pytest`, `black`, `ruff`) — never hand-edit dependency versions into `pyproject.toml` without running `uv add`/`uv lock` afterward, so `uv.lock` stays in sync.
- **Removing a dependency**: `uv remove <package>`.
- **Running project code**: `uv run <command>` (e.g. `uv run pytest`, `uv run python -m scheduler.daily_batch`) rather than activating the venv manually — this guarantees the locked environment is used. Use module invocation (`-m`) for scripts under a package directory, not a raw file path — `uv run python scheduler/daily_batch.py` fails with `ModuleNotFoundError` because it only puts the script's own directory on `sys.path`, not the project root that its absolute imports need.
- **Upgrading dependencies**: `uv lock --upgrade` (all) or `uv lock --upgrade-package <package>` (single), reviewed and committed deliberately, not as a side effect of an unrelated change.
- `uv.lock` is committed to the repository; the virtual environment directory (`.venv/`) is not.

## Key Decisions Already Locked In

Do not re-litigate these without a clear reason, and if you do change one, record it in `DECISIONS.md` (see below):

- Ingestion runs **once daily** in a scheduled batch, not continuously — accepted trade-off for simplicity and lower API/LLM cost.
- Only sources with **fully automatable, zero-manual-work** ingestion are included (Local Files, Notion, Gmail, GitHub, Google Calendar, Browser History). YouTube history, Kaggle, Slack, Kindle were explicitly excluded.
- All storage is **local and free** (SQLite, Chroma embedded, Neo4j Community Edition) — no hosted/paid storage services.
- Search is **hybrid**: semantic (Chroma) + keyword (SQLite FTS5/BM25), merged via Reciprocal Rank Fusion.
- Relationships are detected via **vector-similarity candidate narrowing, then LLM confirmation**, never an LLM comparison against the full dataset.
- The **LLM provider is configurable**: fully local (Ollama), fully cloud (OpenRouter, any supported model), or mixed (cheap tasks local, answer synthesis cloud). Never hardcode a single vendor's SDK directly into business logic — always go through `ProviderInterface`.
- Browser History is intentionally the **lightest-weight source** (title + URL + metadata only, no full page fetch, no relationship detection).

## Files You Must Create and Maintain: `DECISIONS.md` and `FLOW.md`

These two files do not exist yet — create them the first time they're needed and keep them updated as code is written. They are part of the deliverable, not optional scratch notes.

### `DECISIONS.md`

**Purpose**: a running, human-readable log of meaningful implementation decisions made *during coding* — the ones not already settled in `/docs` — along with the reasoning behind each.

**When to add an entry**: any time you choose between two or more reasonable implementation approaches and the choice isn't already dictated by `/docs`. Examples: choice of a specific chunking boundary algorithm, how retries are structured in a provider call, a schema field added that isn't in `Database_Schema.docx`, a library chosen for a sub-task, a deviation from a documented convention and why.

**When NOT to add an entry**: routine implementation of something already fully specified in `/docs` (e.g. implementing the `/api/query` endpoint exactly as specified in `API_Specification.docx` is not a new decision).

**Format** — append entries in reverse-chronological order (newest at top), each as:

```markdown
## 2026-08-14 — Chunking boundary fallback for unstructured text

**Context**: Local .txt files with no headings or paragraph breaks don't have
a natural chunking boundary the way Notion blocks or Markdown do.

**Decision**: Fall back to a fixed 400-token sliding window with 40-token
overlap (matching config.yaml's chunking defaults) when no structural
boundary is detected.

**Alternatives considered**: Sentence-boundary splitting via a tokenizer —
rejected because it produced very uneven chunk sizes on dense technical text.

**Affects**: pipeline/chunking.py
```

### `FLOW.md`

**Purpose**: a maintained map of how the code actually executes — entry points, the order functions are called in, and which module hands off to which — so a future reader (human or agent) can understand runtime behavior without re-reading every file.

**When to update**: any time a new entry point is added, the order of an existing call chain changes, or a step is added/removed from the ingestion or query flow.

**Required content**:
- One section per entry point (`scheduler/daily_batch.py`, `api/main.py`'s startup, `POST /api/query`, etc.)
- For each entry point, an ordered call chain down to the meaningful leaf calls (not every single line — the functions that matter for understanding behavior)
- Note where branching happens (e.g. Query Router deciding which search nodes to invoke) and what determines the branch

**Format** — one section per entry point, e.g.:

```markdown
## Entry point: scheduler/daily_batch.py (`main()`)

1. `main()` reads `last_run_timestamp` from `ingestion_runs` (storage/sqlite_store.py)
2. For each of the 6 extractors (extractors/*.py):
   a. `extract_new_items(since=last_run_timestamp)` → list of normalized items
   b. Each item → `pipeline/filters.py::apply_noise_filter()`
   c. Surviving items → `pipeline/chunking.py::chunk_text()`
   d. Each chunk → `pipeline/metadata.py::generate_metadata()` (rule-based fields
      direct; project_name/topic via `providers.generate_metadata()`)
   e. Each chunk → `pipeline/embeddings.py::embed()` → stored in Chroma
   f. Raw text + metadata → `storage/sqlite_store.py::insert_item()` /
      `insert_chunk()`
3. After all sources processed: `pipeline/relationships.py::detect_relationships()`
   runs per new item — queries Chroma for candidates, confirms via
   `providers.generate_relationship()`, writes edges via
   `storage/neo4j_store.py::write_edge()`
4. On full success: `storage/sqlite_store.py::update_last_run_timestamp()`

## Entry point: POST /api/query (api/routes/query.py)

1. Request validated against schema (api/schemas.py)
2. `agent/graph.py::run(question, session_id)` invoked
3. `agent/router.py::route()` inspects the question, decides which of
   VectorSearchNode / KeywordSearchNode / GraphTraversalNode to invoke
4. Selected nodes run (agent/search_nodes.py, agent/graph_traversal.py)
5. `agent/merger.py::merge()` — Reciprocal Rank Fusion across whichever
   nodes ran
6. `agent/synthesizer.py::synthesize()` — fetches full text for top results
   from SQLite, calls `providers.generate_answer()`, returns cited answer
7. Response serialized per API_Specification.docx and returned
```

Both files live at the repository root, alongside this file. Update them in the same commit as the code change they describe — do not let them drift out of date.

## Git & Commit Practices

**Commit frequently, in small units.** Commit after each self-contained piece of work is done and working — a single function, a single extractor, a single endpoint, a single test file — rather than accumulating many changes across multiple files/features into one large commit. As a rough guide: if a commit's diff is touching more than one module's worth of new functionality, it should probably have been two commits. Don't wait until a whole feature (e.g. "all six extractors") is done to commit; commit each extractor as it's finished and passing its tests.

**Never include AI attribution in commits.** Do not add `Co-authored-by: Claude`, `Co-authored-by: Codex`, `Generated with Claude Code`, or any similar AI-attribution line, trailer, or footer to any commit message, PR description, or code comment. Commit messages should read exactly as if the user wrote them — no signature, no tool mention, no emoji indicating AI involvement.

**Commit message format**: Conventional Commits style (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`), short imperative summary line, optionally a body explaining *why* if the change isn't self-evident. Example:

```
feat: add Gmail extractor with thread-based grouping

Groups messages by thread_id before chunking so replies in the
same conversation stay logically connected downstream.
```

**Before pushing**: confirm with the user before running `git push`, per the standing rule that side-effectful actions require explicit confirmation — committing locally is fine to do proactively as part of the small-commits workflow above, but pushing to the remote is a separate, confirmed step.

## Branching & PR Workflow

**`main` is protected and locked.** Never commit directly to `main`. All work happens on a feature branch and reaches `main` only through a pull request.

**One branch per feature/unit of work**, not one branch for everything. Examples: a branch for the Gmail extractor, a separate branch for the Notion extractor, a separate branch for the Query Router node — not one long-lived `dev` branch that accumulates unrelated work. This keeps each PR reviewable and keeps `main` always in a working state.

**Branch naming**: `type/short-description`, matching the commit-type prefixes already in use — e.g. `feat/gmail-extractor`, `feat/query-router`, `fix/chunking-boundary`, `refactor/provider-interface`, `docs/update-flow-md`.

**Workflow for a unit of work**:
1. Branch off the latest `main`: `git checkout -b feat/gmail-extractor`
2. Make the small, periodic commits described in Git & Commit Practices above, on this branch.
3. When the feature/fix is complete and its tests pass, push the branch (with confirmation, per the rule above) and open a PR against `main`.
4. PR description summarizes what changed and why — same no-AI-attribution rule applies to PR descriptions as to commit messages.
5. Merge only after the user has reviewed/approved — do not merge your own PR automatically.

**Don't mix unrelated features on one branch.** If mid-task you find yourself wanting to touch a module unrelated to the current branch's purpose, stop and start a separate branch for that instead of folding it in.

## Ground Rules

- Never bypass the LLM Provider abstraction — no direct Ollama or OpenRouter SDK calls outside `providers/`.
- Never send source content to a cloud LLM provider unless the user's configured `provider_mode` is `fully_cloud` or `mixed` for that specific task.
- Never introduce a paid/hosted storage dependency (vector DB, graph DB, relational DB) — local-only is a hard constraint, not a default.
- Never add a source integration that requires manual, non-automatable steps (this was explicitly evaluated and rejected for several candidate sources — see `Technical_Design_Document.docx` Section 2).
- Never batch large amounts of unrelated work into a single commit, and never add AI co-author/attribution lines to a commit or PR — see Git & Commit Practices above.
- Never commit directly to `main` and never merge your own PR without user approval — see Branching & PR Workflow above.
- When in doubt about a convention, check `/docs` before inventing a new pattern.
