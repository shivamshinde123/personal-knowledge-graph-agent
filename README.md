# Personal Knowledge Graph Agent

A fully local, privacy-first system that continuously ingests one person's data
from six sources — **Local Files, Notion, Gmail, GitHub, Google Calendar, and
Browser History** — links it across sources, and answers natural language
questions about it through a LangGraph agent, with a chat UI and a live
relationship graph.

Everything runs on one machine, for one user. There is no multi-tenant layer,
no hosted database, and no authentication — by design. It's built primarily as
a portfolio project demonstrating applied depth in LangChain, LangGraph, and
LangSmith, and secondarily as a genuinely useful personal tool.

> **Docker Compose packaging is available** — see [Docker](#docker) below
> for the fastest path to a running stack: `docker compose up -d --build`,
> then connect every source from the browser via the guided setup wizard
> (Settings → Guided setup) — no manual `config/.env` editing required.

<p align="center">
  <img src="docs/screenshots/chat.png" width="100%" alt="Chat screen" />
  <br />
  <img src="docs/screenshots/graph.png" width="100%" alt="Relationship graph" />
</p>

## What it does

The system ingests from all six sources once a day (or on demand), filters out
noise, chunks and embeds what survives, detects relationships across sources,
and makes the result queryable through a chat interface:

> _"What did I work on related to RAG pipelines in the last 2 months?"_

The answer comes back synthesized and cited, with links back to the original
Notion page, email thread, commit, calendar event, or file. A separate graph
view lets you visually explore every confirmed relationship between items.

**Highlights:**

- Six independent extractors — one source failing never blocks the others.
- Hybrid search (semantic + keyword) merged with a relationship-graph lookup,
  routed automatically per question.
- Relationships between items are detected automatically and confirmed by an
  LLM before being stored.
- Choice of a fully local setup (Ollama, generation and embedding both) or a
  fully cloud one (OpenRouter, any supported model, both as well),
  switchable from Settings.
- A chat interface, a settings screen for configuring every source, and an
  interactive graph view of the whole knowledge base.

## How it works

### Ingestion pipeline

Each of the six extractors normalizes what it pulls into a common shape
(title, raw text, timestamps, source reference) before handing it to a shared
pipeline:

1. **Noise filtering** happens in two layers. Source-specific rules run
   first, inside each extractor, since only it has access to that source's
   native data: Browser History drops URLs below a minimum visit count and
   anything matching a domain blocklist (search engines, social media);
   Gmail drops messages under excluded labels (promotions, social); Calendar
   drops recurring events with no description (standing reminders, not real
   meetings). Everything that survives then passes through one universal
   filter — anything empty or shorter than a minimum content length is
   dropped as noise, regardless of source.
2. **Chunking** splits the surviving text at natural paragraph boundaries,
   packed to a target size; a single paragraph too long to fit on its own
   falls back to a sliding window with overlap, since there's no natural
   break to prefer instead.
3. **Metadata generation** derives a project name and topic per item via an
   LLM call, batched across several items per call rather than one at a time.
4. **Embedding** turns each chunk into a vector.

From there, data lands in three places: the raw text and metadata go to
**SQLite** (also indexed for keyword search), the chunk vectors go to
**Chroma**, and — once relationship detection runs — confirmed relationships
go to **Neo4j**. A query at answer time reads from all three: SQLite and
Chroma for retrieval, Neo4j for relationship lookups.

### Models: local vs. cloud

The **Provider Mode** setting is a single toggle that selects both the
*generation* model (metadata extraction, relationship judging, answer
synthesis) and the *embedding* model used for every stored vector:

- **Local** — both generation and embedding run through Ollama, no
  per-call cost and no network dependency at all.
- **Cloud** — both run through OpenRouter, any supported model, highest
  quality, per-call cost.

Switching between Local and Cloud is treated as destructive, not a quiet
setting change — the two modes' embedding models are never compatible with
each other, so a switch requires confirming twice through a deliberately
alarming prompt, then automatically wipes all ingested data (equivalent to
"Reset all data"). It does **not** auto-trigger re-ingestion afterward — run
that yourself once you're ready. Changing either embedding model in place,
without switching modes, still needs the same full reset (and, for the
cloud embedding model specifically, an automatic re-ingest), since existing
vectors won't match a new model's space.

### A note on ingestion cost and time — local is preferred

The **first ingestion run is the expensive one.** It has to work through
everything a source has ever produced — every file, every email in range,
every commit and issue a repository has ever had — and every one of those
items also has to be embedded, checked against the rest of the knowledge
base for relationships (its own LLM call per item), and have its metadata
extracted. That combination is what makes an initial backfill slow, and
under Cloud mode, the point where real dollar cost shows up: a lot of
items, each needing its own round trip to a paid API for generation *and*
embedding.

**After that first run, ingestion settles into something much lighter.**
Each later run only has to look at what actually changed since the day
before — a handful of new emails, a few new commits, a couple of edited
Notion pages — so both the time and the cost drop dramatically once the
backlog is cleared. It doesn't disappear, though: every day still brings
some new content, and that content still needs the same
extract-embed-relate treatment, so there's a real, ongoing cost baked into
just keeping the knowledge base current. On top of that, actually *using*
the assistant — asking it questions — has its own ongoing cost under Cloud
mode, since every answer is its own LLM call at the moment you ask it,
separate from anything ingestion does.

So the honest shape of it is: **high once, then low but never zero** — a
steady background cost from daily upkeep, plus whatever chatting with it
costs on top. **Local mode is the preferred choice** for exactly this
reason: it removes the per-call cost from generation, embedding, *and*
chatting all at once, at the cost of depending on local hardware for speed
instead. Cloud mode is easiest to justify for the first, one-time backfill
of a small or carefully scoped set of sources, not as the everyday running
mode.

### How the relationship graph is created

After a batch finishes storing items, each one is checked against the rest
of the knowledge base: its nearest neighbors are found by vector similarity
first (never a full-dataset comparison), and only those candidates are
then judged by an LLM, which labels the relationship (e.g. `implements`,
`discussed_in`) and a confidence score. Judgments below a confidence
threshold are discarded. Browser History is excluded from this step entirely
— it's intentionally the lightest-weight source, title and URL only.

Confirmed relationships become edges in Neo4j; an item with no confirmed
relationship to anything simply has no node in the graph — an unconnected
item wouldn't add anything to a relationship view. The frontend renders the
result as a live, force-directed graph: drag a node and its neighbors
resettle around it, click one to highlight its connections, and filter by
source from the toolbar.

## Settings

<p align="center">
  <img src="docs/screenshots/settings.png" width="100%" alt="Settings screen" />
</p>

Every setting is editable from the Settings screen and saves itself
immediately — no separate save step.

| Setting | Default |
|---|---|
| Provider mode | Cloud |
| Local generation model | `llama3:8b` |
| Local embedding model | `nomic-embed-text` |
| Cloud generation model | `anthropic/claude-sonnet-4` |
| Cloud embedding model | `openai/text-embedding-3-small` |
| Local folders to watch | none |
| Notion page scope | unscoped — every page the integration can see |
| GitHub repository scope | unscoped — every accessible repo |
| Gmail date range | last 15 days |
| GitHub date range | unbounded |
| Calendar date range | today through 30 days out |

The screen also shows live ingestion progress, lets you trigger or stop a
run on demand, shows each source's connection status, and has a "reset
everything" option for starting over.

### Guided setup

The "Guided setup" button walks through connecting everything in one linear
flow instead of hunting through individual Settings fields: provider mode,
Gmail + Calendar OAuth (paste a Client ID/Secret, sign in via a popup — no
downloaded JSON file, plus their date ranges), Notion/GitHub/OpenRouter
credentials (checked against the real API before saving, plus Notion's page
scope and GitHub's repo scope/date range), local files, and browser
history. Every step past provider mode is optional — skip anything you
don't use. It also pre-fills from whatever's already configured, so
revisiting it later doubles as a way to review your setup, not just a
one-time flow.

## Architecture

Six layers, each depending only on the layer below it:

| Layer | Responsibility | Where |
|---|---|---|
| **Data Sources** | Notion, Gmail, GitHub, Google Calendar, filesystem, browser | (external) |
| **Ingestion** | Extraction, noise filtering, chunking, metadata, embeddings | `extractors/`, `pipeline/` |
| **Understanding & Storage** | SQLite + FTS5, Chroma (embedded), Neo4j Community Edition | `storage/` |
| **Query & Reasoning** | LangGraph: Router → Search nodes → Merger → Synthesizer | `agent/` |
| **Interface** | FastAPI backend + React chat/graph/settings UI | `api/`, `frontend/` |
| **Evaluation** | LangSmith tracing, datasets, evaluators | `eval/` |

Ingestion runs as a scheduled daily batch (also triggerable on demand from
Settings), kept separate from the API process so a long-running batch never
blocks the app or takes it down if something goes wrong.

## Tech stack

| Concern | Choice |
|---|---|
| Agent orchestration | LangGraph, LangChain |
| Observability/eval | LangSmith |
| Backend | FastAPI (Python ≥3.12) |
| Frontend | React 19, Vite |
| Relational/keyword store | SQLite + FTS5 (BM25) |
| Vector store | Chroma (embedded, local) |
| Graph store | Neo4j Community Edition (local) |
| LLM/embeddings | Ollama (local) or OpenRouter (cloud) |
| Package management | [uv](https://docs.astral.sh/uv/) (Python), npm (frontend) |
| Packaging/deployment | Docker Compose |

Full rationale for each choice is in `docs/Tech_Stack.docx`.

## Repository layout

```
extractors/    one module per data source
pipeline/      noise filtering, chunking, metadata generation, embeddings, relationship detection
storage/       SQLite, Chroma, and Neo4j stores
providers/     LLM provider abstraction (local vs. cloud)
agent/         LangGraph node wiring — router, search, synthesizer
api/           FastAPI app and routes
frontend/      React app — chat, graph, and settings screens
scheduler/     the daily ingestion batch entrypoint, plus the Docker scheduler sidecar
eval/          LangSmith evaluation runner and test questions
config/        typed settings, config.yaml, .env.example
docker/        Dockerfiles for the backend/scheduler and frontend images
tests/         mirrors the source tree
docs/          the original design document set — see the note below
```

## Docker

The fastest way to get the whole stack running persistently — backend,
frontend, Neo4j, and (optionally) Ollama, plus a scheduler sidecar that
replaces registering your own cron/Task Scheduler entry — without installing
uv, Node.js, Neo4j, or Ollama on your machine at all.

**Prerequisites**: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(or Docker Engine + Compose on Linux).

```bash
cp .env.docker.example .env       # at the repo root -- fills in docker-compose.yml's own variables
cp config/.env.example config/.env   # the app's own config -- everything in it is fillable from the wizard instead

docker compose up -d --build
```

Every value in the root `.env` above has a working default — this genuinely is a one-command start; nothing needs editing first unless you want to change a default. See `.env.docker.example`'s own comments for what each variable does (`NEO4J_PASSWORD`, `HOST_STORAGE_DIR`, `HOST_WATCH_DIR`/`HOST_DATA_DIR`, ports).

- Frontend: `http://localhost:3000` (or `HOST_FRONTEND_PORT` if you changed it)
- Backend API: `http://localhost:8080/api` (or `HOST_BACKEND_PORT`)
- Add `--profile local-llm` to the `up` command (or set `COMPOSE_PROFILES=local-llm`
  in `.env`) to also start an Ollama container, if `config/.env`'s
  `provider_mode` is `fully_local`.

Open the frontend and go to **Settings → Guided setup** to connect everything — Notion/GitHub/OpenRouter credentials, Gmail + Calendar OAuth, source scoping (repos, page IDs, date ranges), local files, and browser history — from the browser, with no `config/.env` hand-editing needed. See [Guided setup](#guided-setup) below.

**A few things still work a little differently under Docker than a manual setup**, called out directly in Settings/the wizard when they apply:

- **Local folders to watch** — you pick which subfolders of the host folder mounted via `HOST_WATCH_DIR` to actually watch; to mount a *different* host folder entirely (not just a different subfolder of the same one), edit `HOST_WATCH_DIR` in `.env` and run `docker compose up -d` again. There's no "Browse…" button, since there's no GUI inside a container to open a native dialog from.
- **Browser history** — same idea: pick a file reachable under `HOST_DATA_DIR`, or point `HOST_DATA_DIR` at a different host folder and restart to make a different file reachable.
- **SQLite/Chroma storage location** — set once via `HOST_STORAGE_DIR` in the root `.env` before first launch; there's no in-app field for this, since it's a single deploy-time decision, not something to change per subfolder the way local files' scope is.

**Persistence**: every container restarts automatically
(`restart: unless-stopped`) after a crash or a host reboot, as long as
Docker itself is set to start on login — a one-time Docker Desktop setting,
not something `docker-compose.yml` controls. SQLite and Chroma's data live
under `HOST_STORAGE_DIR` (a real host folder, `./docker/storage` by
default — inspect it directly, no `docker volume` commands needed); Neo4j
and Ollama's still live in named Docker volumes. `docker compose down`
(without `-v`) keeps everything across a full stack restart; add `-v` only
when you actually want to wipe Neo4j/Ollama's volumes (SQLite/Chroma's host
folder isn't touched by `-v` at all — delete `HOST_STORAGE_DIR`'s contents
yourself if you want those gone too).

**Useful commands**: `docker compose logs -f backend` (or `scheduler`/
`frontend`/`neo4j`) to tail one service's logs; `docker compose down` to
stop everything without deleting data; `docker compose up -d --build` again
after pulling code changes.

**Why GitHub only, no Docker Hub**: this is primarily a portfolio project —
the audience is someone reading the repo, not blind-pulling a prebuilt
image — and `docker compose up -d --build` from a clone already delivers a
genuine one-command start with no gap to fill. For a tool that ingests this
much personal data (Gmail, browser history, private repos), being able to
read/build the source yourself is also a better trust posture than an
opaque image from a registry. Publishing prebuilt images isn't ruled out
later, but isn't planned.

## Setup (developer / manual)

**Prerequisites**: [uv](https://docs.astral.sh/uv/), Node.js + npm, a running
Neo4j Community Edition instance, and (only for local inference) Ollama.

```bash
# Backend
uv sync
cp config/.env.example config/.env   # fill in the values you need
uv run pytest
uv run python -m api.main

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

`config/.env.example` documents every credential/path the extractors need;
`config/config.yaml` holds non-secret settings (provider mode, chunking,
retrieval). Everything is also editable from the Settings screen once the app
is running. Full setup steps — including the one-time OAuth consent needed
for Gmail and Google Calendar — are in `docs/Environment_Config_Reference.docx`.

All Python commands go through `uv`, never `pip` directly.

## Testing

```bash
uv run pytest                                  # backend
cd frontend && npm run lint && npm run build   # frontend
```

## Documentation

**Important**: the documents in `docs/` were written **at the start of the
project**, before implementation began — they capture the original design
intent, not a live record of the system as it exists today. Real work has
happened since that `docs/` doesn't reflect. For what actually exists and
why, `DECISIONS.md` and `FLOW.md` are the sources kept up to date as the code
changes — read those first, and treat `docs/` as historical context.

| File | Contents |
|---|---|
| `DECISIONS.md` | Implementation decisions made during coding, and why |
| `FLOW.md` | How the code actually executes, per entry point |
| `docs/` | The original design document set — see the note above |

## Roadmap

- Full end-to-end test of a wizard-driven setup through to a working ingestion → graph → chat result ([issue #97](https://github.com/shivamshinde123/personal-knowledge-graph-agent/issues/97)) — the wizard's own mechanics (credential validation, Google OAuth, source scope) and the ingestion → graph → chat pipeline have each been verified independently, but not yet as one combined run.
- End-to-end testing of the Gmail and Calendar extractors against real data ([issue #68](https://github.com/shivamshinde123/personal-knowledge-graph-agent/issues/68)) — the guided OAuth *connection* itself is now live-verified; real extraction against real accounts isn't yet.

## Status

Core system built and working end-to-end — all six extractors, the agent, the
API, and the full frontend are implemented. Docker Compose packaging
([issue #52](https://github.com/shivamshinde123/personal-knowledge-graph-agent/issues/52))
and the guided first-run setup wizard
([issue #92](https://github.com/shivamshinde123/personal-knowledge-graph-agent/issues/92),
closed) are both done and live-verified against a real `docker compose up`
stack — not just unit tests — making this a genuine "clone and run it"
install. See [Roadmap](#roadmap) for what's still open.
