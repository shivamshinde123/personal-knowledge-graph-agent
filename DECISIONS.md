# Implementation Decisions

A running log of meaningful decisions made **during coding** that were not
already settled in `/docs`. Newest entries at the top.

Decisions already locked in by the design document set are not repeated here —
see `CLAUDE.md` and the documents in `docs/` for those.

---

## 2026-08-24 — Chroma collection uses cosine similarity and no attached embedding function

**Context**: `Database_Schema.docx` defines the Chroma field schema (id,
embedding, document, metadata) but doesn't specify a distance metric or
whether the collection should compute its own embeddings. Chroma's default
distance metric is squared L2, and `get_or_create_collection` attaches a
default embedding function unless told otherwise.

**Decision**: `get_collection()` creates the collection with
`metadata={"hnsw:space": "cosine"}`, matching sentence-transformers models
(including `all-MiniLM-L6-v2`, configured in `config.yaml`), which are
trained and compared with cosine similarity — L2 on their output would rank
results differently than the model was designed for. It also passes
`embedding_function=None`: every write in `chroma_store.py` supplies an
already-computed vector (from `pipeline/embeddings.py`), so Chroma is never
asked to embed text itself, keeping embedding model selection entirely in
the pipeline layer rather than split across two places.

**Alternatives considered**:
- *Leave the default L2 metric* — rejected; it's a silent correctness issue
  that would only surface as subtly-off search results, not an error.
- *Let Chroma's default embedding function stay attached* — rejected; it
  would silently attempt to embed text (using its own bundled model, not the
  configured one) on any call that omits an explicit embedding, masking
  bugs where a caller forgot to compute one.

**Affects**: `storage/chroma_store.py`

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
