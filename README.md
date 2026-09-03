# Personal Knowledge Graph Agent

A fully local, privacy-first system that ingests one person's data from six
sources - **Local Files, Notion, Gmail, GitHub, Google Calendar, and Browser
History** - links it across sources, and answers natural language questions
about it through a LangGraph agent, with a chat UI and a live relationship
graph.

Everything runs on one machine, for one user. There is no multi-tenant
layer, no hosted database, and no authentication - by design.

<p align="center">
  <img src="docs/screenshots/chat.png" width="100%" alt="Chat screen" />
  <br />
  <img src="docs/screenshots/graph.png" width="100%" alt="Relationship graph" />
</p>

## Features

- Six independent extractors - one source failing never blocks the others.
- Hybrid retrieval (semantic + keyword search + relationship-graph lookup),
  routed automatically per question, merged via Reciprocal Rank Fusion.
- Relationships between items are detected automatically and confirmed by
  an LLM before being stored.
- Local (Ollama) or Cloud (OpenRouter) model provider, switchable from
  Settings.
- A guided setup wizard connects every source from the browser - no manual
  config-file editing for credentials, OAuth, or source scope.

See [`docs/Technical_Report.docx`](docs/Technical_Report.docx) for a full
write-up of the architecture, every source's extraction/filtering rules,
the relationship-detection algorithm, and the storage design.

## Quick start (Docker)

**Prerequisites**: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(or Docker Engine + Compose on Linux).

```bash
git clone https://github.com/shivamshinde123/personal-knowledge-graph-agent.git
cd personal-knowledge-graph-agent

cp .env.docker.example .env
cp config/.env.example config/.env

docker compose up -d --build
```

Every value has a working default - nothing needs editing before this
first run, *except* two things you can only set before the containers
start, if you want these two sources:

- **Local files** - set `HOST_WATCH_DIR` in `.env` to a real folder
  before running `docker compose up`.
- **Browser history** - set `HOST_DATA_DIR` in `.env` to a folder holding
  a copy of your browser's history file.

Both are covered in detail, including real mistakes to avoid, under
[Configuration](#configuration) below. Skip them for now if you don't need
those two sources yet - everything else works without either.

Give it a little over 30 seconds after `up`: Neo4j's own health check
gates when the backend and scheduler are allowed to start.

- Frontend: `http://localhost:3000` (or `HOST_FRONTEND_PORT`)
- Backend API: `http://localhost:8080/api` (or `HOST_BACKEND_PORT`)
- Add `--profile local-llm` (or set `COMPOSE_PROFILES=local-llm` in `.env`)
  to also start an Ollama container, if you plan to run in Local mode.

Open the frontend and go to **Settings → Guided setup**. Everything else -
credentials, Google OAuth, source scope - is connected from there.

## Configuration

Two separate `.env` files are involved, and they are not interchangeable:

| File | Read by | Holds |
|---|---|---|
| `.env` (repo root) | `docker compose` itself | Ports, `NEO4J_PASSWORD`, `HOST_STORAGE_DIR`, `HOST_WATCH_DIR`, `HOST_DATA_DIR` |
| `config/.env` | The application | Every credential and source setting - fillable entirely from the guided setup wizard instead |

Full variable-by-variable reference: [`docs/Environment_Config_Reference.docx`](docs/Environment_Config_Reference.docx).
Both example files (`.env.docker.example`, `config/.env.example`) document
every variable inline as well.

### Watching local folders

`HOST_WATCH_DIR` (root `.env`) mounts one host folder into the containers;
the wizard then lets you pick which of its subfolders to actually watch.

> [!IMPORTANT]
> Point `HOST_WATCH_DIR` at a folder containing **only** what you want
> ingested - not a folder that also contains this repository's own clone.
> If the repo is checked out somewhere inside the mounted folder, its own
> tracked files (`README.md`, `docs/*.docx`, etc.) get ingested as if they
> were your personal notes.

To watch a *different* host folder later, edit `HOST_WATCH_DIR` and run
`docker compose up -d` again - the running app itself cannot mount a new
host folder into a container that's already started.

### Browser history

`HOST_DATA_DIR` (root `.env`) must point at a **folder**, not the history
file itself - pointing it at a file bind-mounts that single file where the
app expects a directory to list, and the picker will show nothing.

Your browser locks its history file while running, so point `HOST_DATA_DIR`
at a dedicated folder holding a **copy**, not your live browser profile:

```bash
# close the browser first
mkdir pkg-agent-hostdata
cp "C:\Users\<you>\AppData\Local\Google\Chrome\User Data\Default\History" pkg-agent-hostdata/
```

```
HOST_DATA_DIR=./pkg-agent-hostdata
```

Pointing it at your whole live profile folder instead of a dedicated copy
also breaks the picker in a different way: a real profile folder contains
tens of thousands of cache files, and the picker's file list is capped -
your actual `History` file can get pushed out of that cap entirely.

### GitHub token scope

A **private** repository returns `404 Not Found` to a token that can't see
it - indistinguishable from "the repo doesn't exist" unless you know to
check this. If you're scoping ingestion to a private repo, make sure
`GITHUB_TOKEN` has the classic `repo` scope, or (for a fine-grained token)
has that specific repository added to its access list.

### Port conflicts

`docker compose up` fails with `port is already allocated` if something
else on your machine already holds one of the ports this stack uses.
Check what it actually is before touching it -
`netstat -ano | findstr :<port>` (Windows) or `lsof -i :<port>` (Mac/Linux)
- and if you still need it, just pick a different port instead
(`HOST_FRONTEND_PORT`, `HOST_BACKEND_PORT`, `HOST_NEO4J_HTTP_PORT`,
`HOST_NEO4J_BOLT_PORT`, all in the root `.env`). If a container ends up
crash-looping from a conflict that was already in progress when it
started, `docker compose down`, fix the port, then `docker compose up -d`.

### Persistence

SQLite and Chroma's data live under `HOST_STORAGE_DIR` (default
`./docker/storage`) - a real host folder, inspectable directly. Neo4j and
Ollama use named Docker volumes. `docker compose down` (no `-v`) keeps
everything; `-v` also wipes Neo4j/Ollama's volumes, but never touches
`HOST_STORAGE_DIR` - delete its contents yourself if you want a clean
slate there too.

## Manual setup (development)

**Prerequisites**: [uv](https://docs.astral.sh/uv/), Node.js + npm, a
running Neo4j Community Edition instance, and (only for Local mode) Ollama.

```bash
# Backend
uv sync
cp config/.env.example config/.env
uv run pytest
uv run python -m api.main

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

All Python commands go through `uv` - never `pip` directly.

## Testing

```bash
uv run pytest                                  # backend
cd frontend && npm run lint && npm run build   # frontend
```

## Architecture

Six layers, each depending only on the layer below it:

| Layer | Responsibility | Where |
|---|---|---|
| Data Sources | Notion, Gmail, GitHub, Google Calendar, filesystem, browser | (external) |
| Ingestion | Extraction, noise filtering, chunking, metadata, embeddings | `extractors/`, `pipeline/` |
| Understanding & Storage | SQLite + FTS5, Chroma (embedded), Neo4j Community Edition | `storage/` |
| Query & Reasoning | LangGraph: Router → Search nodes → Merger → Synthesizer | `agent/` |
| Interface | FastAPI backend + React frontend | `api/`, `frontend/` |
| Evaluation | LangSmith tracing, datasets, evaluators | `eval/` |

```
extractors/    one module per data source
pipeline/      filtering, chunking, metadata, embeddings, relationship detection
storage/       SQLite, Chroma, and Neo4j stores
providers/     LLM provider abstraction (local vs. cloud)
agent/         LangGraph node wiring
api/           FastAPI app and routes
frontend/      React app
scheduler/     daily ingestion batch entrypoint + Docker scheduler sidecar
eval/          LangSmith evaluation runner
config/        typed settings, config.yaml, .env.example
docker/        Dockerfiles and mount targets
tests/         mirrors the source tree
docs/          design document set + the full technical report
```

## Tech stack

LangGraph, LangChain, LangSmith · FastAPI · React 19 + Vite · SQLite + FTS5
· Chroma · Neo4j Community Edition · Ollama / OpenRouter · uv · Docker
Compose

Full rationale for each choice: [`docs/Tech_Stack.docx`](docs/Tech_Stack.docx).

## Documentation

| File | Contents |
|---|---|
| [`docs/Technical_Report.docx`](docs/Technical_Report.docx) | Full architecture, algorithms, and design rationale |
| `DECISIONS.md` | Implementation decisions made during coding, and why |
| `FLOW.md` | How the code actually executes, per entry point |
| `docs/` | The full design document set, kept current as the code changes |

## Roadmap

- Full end-to-end verification of a wizard-driven setup through to a
  working ingestion → graph → chat result
  ([issue #97](https://github.com/shivamshinde123/personal-knowledge-graph-agent/issues/97)).
- Sustained real-data testing of the Gmail and Calendar extractors against
  large accounts
  ([issue #68](https://github.com/shivamshinde123/personal-knowledge-graph-agent/issues/68)).

## Status

Core system built and working end-to-end, live-verified against a real
Docker stack - a genuine "clone and run it" install.
