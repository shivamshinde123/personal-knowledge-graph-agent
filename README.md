# Personal Knowledge Graph Agent

A fully local, privacy-first system that continuously ingests personal data from
six sources, links it across those sources, and answers natural language
questions about it through a LangGraph agent.

Everything runs on one machine. There is no multi-tenant layer, no hosted
database, and no authentication — by design.

## What it does

The system ingests from **Local Files, Notion, Gmail, GitHub, Google Calendar,
and Browser History** once a day, filters out noise, chunks and embeds what
survives, detects relationships across sources, and makes the result queryable:

> _"What did I work on related to RAG pipelines in the last 2 months?"_

The answer comes back synthesized and cited, with links back to the original
Notion page, email, or commit.

## Architecture

Six layers, each depending only on the layer below it:

| Layer | Responsibility |
|---|---|
| **Data Sources** | Notion, Gmail, GitHub, Google Calendar, filesystem, browser |
| **Ingestion** (`extractors/`, `pipeline/`) | Extraction, noise filtering, chunking, metadata, embeddings |
| **Understanding & Storage** (`storage/`) | SQLite + FTS5, Chroma (embedded), Neo4j Community Edition |
| **Query & Reasoning** (`agent/`) | LangGraph: Router → Search nodes → Merger → Synthesizer |
| **Interface** (`api/`, `frontend/`) | FastAPI on `localhost:8080` + React chat UI |
| **Evaluation** (`eval/`) | LangSmith tracing, datasets, evaluators |

Search is hybrid — semantic (Chroma) plus keyword (SQLite FTS5/BM25) — merged
via Reciprocal Rank Fusion. Relationships are found by narrowing candidates with
vector similarity first, then confirming with an LLM, never by comparing against
the full dataset.

The LLM provider is configurable: fully local (Ollama), fully cloud
(OpenRouter, any supported model), or mixed — cheap ingestion tasks local,
answer synthesis in the cloud.

## Setup

Requires [uv](https://docs.astral.sh/uv/), Neo4j Community Edition, and
(for local inference) Ollama.

```bash
# 1. Install dependencies into a managed virtual environment
uv sync

# 2. Configure
cp config/.env.example config/.env   # then fill in every value

# 3. Run the tests
uv run pytest
```

`config/.env.example` documents every variable and where its credential comes
from. Full setup steps, including the OAuth flows for Gmail and Google Calendar,
are in `docs/Environment_Config_Reference.docx`.

All Python commands go through uv (`uv add`, `uv run`) — never `pip` directly.

## Repository layout

```
extractors/   one module per data source
pipeline/     filtering, chunking, metadata, embeddings, relationships
storage/      sqlite_store.py, chroma_store.py, neo4j_store.py + schema/
providers/    LLM provider abstraction (base, local, openrouter)
agent/        LangGraph node wiring
api/          FastAPI app, routes, schemas
frontend/     React app
eval/         LangSmith evaluation runner + test question set
scheduler/    daily_batch.py entrypoint
config/        config.yaml, .env.example, settings.py
tests/          mirrors the source tree
docs/           design document set
```

## Documentation

| File | Contents |
|---|---|
| `CLAUDE.md` | Working context and conventions for this repository |
| `DECISIONS.md` | Implementation decisions made during coding, and why |
| `FLOW.md` | How the code actually executes, per entry point |
| `docs/` | The full design document set (12 documents) |

## Status

Under active development. Storage layer in progress; see `DECISIONS.md` for the
running log.
