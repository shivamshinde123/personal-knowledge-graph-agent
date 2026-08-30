# Implementation Decisions

A running log of meaningful decisions made **during coding** that were not
already settled in `/docs`. Newest entries at the top.

Decisions already locked in by the design document set are not repeated here —
see `CLAUDE.md` and the documents in `docs/` for those.

---

## 2026-08-30 — Embedding is cloud-only, unconditionally, regardless of `provider_mode`

**Context**: The dimension-matching fix (below) still left two embedding models in play, kept in sync by construction. Requested directly: simplify further — remove the local embedding model entirely, always embed via the cloud model, and keep `provider_mode` controlling generation only. This was raised alongside a direct question: does switching `provider_mode` (or embedding models generally) automatically re-embed anything, or is it just a text hint? Answer, honestly: at the time, nothing was automatic — it was only a message. Removing the second embedding model entirely makes that question moot for `provider_mode`: since there's now only one embedding model, ever, a mode switch can no longer change embeddings at all, so there's nothing left to auto-reset for.

**Decision**: `providers/local_provider.py` no longer has any embedding code at all — `create_local_provider()` builds a generation-only provider (no `embed_fn`), so `generate_embeddings()` on it raises `ProviderError` (the existing safety net for an `embed_fn`-less provider). `providers/base.py::get_provider("embedding")` unconditionally returns `create_openrouter_provider()`, regardless of `provider_mode` — the `if mode == "fully_cloud"` branch that used to gate this is bypassed entirely for the `"embedding"` task. `providers/openrouter_provider.py::EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS` are unchanged (`text-embedding-3-small`, truncated to 384) — there's no longer a second model to match dimensions against, but truncating still keeps stored vectors small.

**Real, explicit trade-off**: `fully_local` no longer means "zero network dependency" — every ingestion run still calls OpenRouter for embeddings, so `OPENROUTER_API_KEY` is required under `fully_local` too, and ingestion fails item-by-item (caught, logged, skipped — same resilience pattern as any other provider failure) if it isn't set. `fully_local`'s remaining meaning is narrower: generation (metadata extraction, relationship judging, answer synthesis, query condensing) runs through Ollama at no per-call cost; only embedding still costs money and needs network access. This is called out directly in the Settings screen's "Fully Local" description and in the Models section's hint text, not left implicit.

**Also answered directly (not a code change, an existing mechanism)**: re-embedding is already incremental, not whole-corpus, and was before this change too — `scheduler/daily_batch.py::main()` only calls each extractor with `since=<last successful run's watermark>`, so `extract_new_items()` only returns items that are new or whose `last_edited_time` is after that watermark; unrelated unchanged files are never re-read, re-chunked, or re-embedded. When a changed item *is* re-processed, `storage/sqlite_store.py::insert_item()`'s upsert detects it's an update (not a new insert) via the return value differing from the freshly generated id passed in, and `replace_chunks()` deletes *only that item's* existing chunks before inserting the freshly (re-)embedded set — so a file edit re-embeds exactly the chunks belonging to that one file, nothing else. `pipeline/relationships.py`'s edges for that item are also cleared and re-detected fresh (`delete_relationships_for_item()`, called for every updated item before relationship detection runs) so a changed item's connections stay in sync with its new content, again scoped to just that item. See `FLOW.md`'s `scheduler/daily_batch.py` entry point for the exact call chain.

**Verified against real data**: real backend restarted, `GET /api/settings` confirmed to return only `local_generation_model`/`cloud_generation_model` (editable) plus `cloud_embedding_model` (display-only); full backend suite (411 tests) passes.

**A pre-existing test bug this exposed, fixed along the way** (not caused by this change, just surfaced by it): `tests/test_pipeline/test_relationships.py::test_narrowing_prefers_similar_substance_over_a_shared_first_chunk` depended on Chroma's tie-break order between two *byte-identical* boilerplate chunks (shared across all three test items) — with the old local model this consistently resolved one way; with the new embedding model's slightly different floating-point distances it resolved the other way, failing the test. Root cause, verified directly (computed real embedding distances for every candidate text): the shared boilerplate chunk is *closer* to the query than either item's own distinguishing content chunk, for both the correctly-related and the unrelated item — so per-item narrowing's "closest chunk wins" dedup was, for this specific test data, always going to surface a tied boilerplate chunk for both items, never actually exercising content-based distinction at all; it only ever "passed" by tie-break luck. Fixed by rewording the related item's content chunk closer to the source's own wording (verified directly this pushes its distance below the boilerplate's, so it deterministically wins the "nearest chunk" comparison outright, no tie involved) — not a fix to the production algorithm itself, which still has this latent edge case (two items sharing byte-identical boilerplate, where neither's real content beats the boilerplate's own similarity) as a real, un-fixed limitation worth flagging for future work.

**Affects**: `providers/base.py`, `providers/local_provider.py`, `providers/openrouter_provider.py`, `config/settings.py`, `config/config.yaml`, `agent/embedding_info.py`, `api/schemas.py`, `api/routes/settings.py`, `frontend/src/components/SettingsPanel.jsx`, `tests/test_pipeline/test_relationships.py`

---

## 2026-08-30 — Freeze the embedding models; match their vector dimensions

**Context**: The four-field model config (previous entry, below) let both `local_embedding_model` and `cloud_embedding_model` be freely edited. That's a real footgun: nothing stopped a user from picking two embedding models of different output dimensionality, which Chroma can't reconcile in one collection at all (not just "different semantic space" — an outright dimension mismatch error, or silently-wrong results depending on how it fails). Requested directly: freeze both so they can't be changed, and pick two models whose vector sizes match automatically.

**Decision**: `local_embedding_model`/`cloud_embedding_model` are no longer config fields at all — they're frozen Python constants: `providers/local_provider.py::EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"` (native 384-dim output) and `providers/openrouter_provider.py::EMBEDDING_MODEL = "openai/text-embedding-3-small"`, called with LangChain's `OpenAIEmbeddings(..., dimensions=384)` — OpenAI's v3 embedding models support truncating their native output via a `dimensions` parameter (Matryoshka representation learning: the model is trained so its leading dimensions alone remain a valid, if slightly lower-quality, embedding), and OpenRouter honors it — verified directly with a real authenticated call: `dimensions=384` returned an exactly-384-wide vector, not the model's native 1536. Both providers' `EMBEDDING_DIMENSIONS` constant is `384`, matched by construction, not by convention. `config/settings.py::update_llm_config()` no longer accepts embedding-model parameters at all (removed from the function signature, not just ignored) — calling it with one raises `TypeError`, so the freeze holds even against a caller who doesn't go through the API. `PUT /api/settings`'s schema (`SettingsUpdateRequest`) has no embedding-model fields either; `GET`/`PUT /api/settings` still *report* both frozen model names (via the new `agent/embedding_info.py` thin wrapper, since `api/routes/settings.py` isn't allowed to import `providers/` directly) purely for display, so the Settings screen can show the user what's actually being used even though it can't be changed. The frontend renders those two fields as permanently `disabled`/`readOnly`, labeled "(fixed)", regardless of `provider_mode` — unlike the generation model fields, which stay editable but only enabled for the active mode.

Matching dimensionality removes the *crash/mismatch* risk of switching `provider_mode`, but the two models are still different embedding spaces trained independently — a mode switch still calls for "Reset all data" + re-ingestion for search relevance to be trustworthy, same as before this change; the Settings copy says so directly.

**Verified against real data**: real authenticated call to `OpenAIEmbeddings(model="openai/text-embedding-3-small", dimensions=384, base_url=OPENROUTER_BASE_URL, api_key=<real key>).embed_documents(...)` returned a real 384-length vector. `GET`/`PUT /api/settings` round-tripped against the real running backend — a `PUT` attempting to set `local_embedding_model`/`cloud_embedding_model` had no effect, confirmed via the response and a follow-up `GET`.

**Affects**: `providers/local_provider.py`, `providers/openrouter_provider.py`, `providers/base.py` (docstring only), `config/settings.py`, `config/config.yaml`, `agent/embedding_info.py` (new), `api/routes/settings.py`, `api/schemas.py`, `frontend/src/components/SettingsPanel.jsx`

---

## 2026-08-30 — Four-field model config; cloud embeddings via OpenRouter; drop `mixed` provider mode

**Context**: Settings only exposed two model fields (`local_model`, `cloud_model`) covering generation only — embedding always ran locally via `sentence-transformers`, hardcoded, with no way to configure or use a cloud embedding model. This was based on an earlier finding that OpenRouter had no embeddings API — re-checked directly, live, this session, and that finding was wrong as of today: OpenRouter now has a real `/api/v1/embeddings` endpoint (confirmed via a real authenticated call — a real embedding vector came back, 1536-dimensional, matching `text-embedding-3-small`) and a real catalog of embedding models (`openrouter.ai/collections/embedding-models` — `text-embedding-3-small/large`, `mistral-embed-2312`, `gemini-embedding-001`, `qwen3-embedding-4b/8b`, and others, several with 2026 dates suggesting they were added after this project's earlier check). The earlier "no embeddings" finding was correct at the time it was made (checked directly then too) — this is a case of the provider's own capabilities changing, not an error in the original research. Also requested directly: drop `mixed` mode, since only `fully_local`/`fully_cloud` are wanted going forward.

**Decision**: `LLMConfig` now has four model fields — `local_generation_model`, `local_embedding_model`, `cloud_generation_model`, `cloud_embedding_model` — replacing `local_model`/`cloud_model`. `ProviderMode` is now `Literal["fully_local", "fully_cloud"]`; `get_provider()` lost its `mixed` branch and its per-`task` routing entirely, since with only two modes every task (`task` is now an unused parameter, kept only so a future per-task override doesn't require every call site to change) always resolves to the same provider.

Embeddings now go through the same `ProviderInterface` abstraction as generation, rather than `pipeline/embeddings.py` loading `sentence-transformers` directly — added `ProviderInterface.generate_embeddings()`, backed on `LangChainProvider` by a plain `embed_fn` callable (not a LangChain `Embeddings` object directly, since the two providers wrap it differently: `create_local_provider()` wraps `sentence-transformers` — moved from `pipeline/embeddings.py` into `providers/local_provider.py`, since model choice is now a provider-level concern — `create_openrouter_provider()` wraps LangChain's `OpenAIEmbeddings` pointed at OpenRouter's base URL, the same "go through LangChain" pattern `ChatOpenAI` already uses for generation there). `pipeline/embeddings.py`'s `embed_chunks()`/`embed_query()` are now thin callers of `get_provider("embedding").generate_embeddings()` — `CLAUDE.md`'s "never bypass the LLM Provider abstraction" ground rule now genuinely covers embeddings too, not just generation.

**Known constraint, surfaced directly in the Settings UI**: switching `provider_mode` changes which embedding model is active, and Chroma vectors from different embedding models are not compatible with each other (different dimensionality — verified directly: local MiniLM produces 384-dim vectors, OpenRouter's `text-embedding-3-small` produces 1536-dim). Nothing currently prevents mixing them in the same collection at write time, so switching modes with existing data risks silently-degraded or broken similarity search on the old vectors. The Settings screen's model section explains this directly and points at "Reset all data" (already built — see the 2026-08-30 full-data-reset entry above) as the fix: reset, then re-run ingestion, after switching modes. A stronger guard (e.g. blocking the switch until a reset happens, or tagging vectors with their source model) is future work, not built now.

**Verified against real data**: `GET`/`PUT /api/settings` round-tripped against the real running backend and real `config.yaml`, confirmed correct via a follow-up `GET`, then restored. Both embedding paths verified with real calls, not just construction: local — `get_provider("embedding").generate_embeddings(...)` against the real configured `sentence-transformers` model, returned real 384-dim vectors. Cloud — `create_openrouter_provider().generate_embeddings(...)` against the real OpenRouter API with a real `OPENROUTER_API_KEY`, returned real 1536-dim vectors matching `text-embedding-3-small`'s known dimensionality exactly.

**Affects**: `config/settings.py`, `config/config.yaml`, `providers/base.py`, `providers/local_provider.py`, `providers/openrouter_provider.py`, `pipeline/embeddings.py`, `api/schemas.py`, `api/routes/settings.py`, `frontend/src/components/SettingsPanel.jsx`

---

## 2026-08-30 — Native folder-picker button for local watch folders

**Context**: The "Local folders to watch" field only accepted a pasted path — requested directly: a "Browse…" button that opens a real file explorer and fills the field in.

**Decision**: A browser cannot do this on its own — neither `<input type="file" webkitdirectory>` nor the File System Access API's `showDirectoryPicker()` exposes a picked folder's absolute filesystem path, by deliberate browser sandboxing (there's no web-platform API that returns one at all). Since this backend runs on the same local machine as the user — this whole system is single-user, local-only, per `CLAUDE.md` — the fix is to open a *native* OS dialog from the backend itself: `agent/browse.py::browse_folder()` shells out to a PowerShell `System.Windows.Forms.FolderBrowserDialog` script and returns whatever path it wrote to stdout (`None` if the user cancelled). `POST /api/settings/browse-folder` exposes it; the button appends the returned path as a new line in the existing textarea (skipping it if already present) rather than replacing the field, so it composes with pasting.

PowerShell rather than `tkinter` — this project's environment is Windows (`CLAUDE.md`'s Environment section), and a subprocess dialog gets its own process with its own main thread, sidestepping any "GUI toolkit called from a non-main thread" concern a FastAPI sync route (run in uvicorn's threadpool, not the main thread) would otherwise raise. This makes the feature Windows-only for now — noted directly in `agent/browse.py`'s docstring as a known limitation, not silently assumed to be portable.

**Verified**: real subprocess call confirmed the `System.Windows.Forms` assembly loads and the script's control flow (`ShowDialog()` returning `OK`/cancelled) is correct; the actual interactive click-through was left for the user to verify themselves, since it requires a real human clicking a real dialog — not something a script can drive.

**Affects**: `agent/browse.py` (new), `api/routes/settings.py`, `api/schemas.py`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/api/client.js`

---

## 2026-08-30 — Toast notifications for ingestion completion and reset

**Context**: "Run ingestion now" only confirmed the run had *started* — there was no way to tell when it actually finished, or whether it succeeded, without manually re-checking Settings. Requested directly.

**Decision**: Added a small toast-notification system (`frontend/src/components/Toasts.jsx`), owned by `App.jsx` rather than `SettingsPanel.jsx`, for two reasons: (1) a triggered ingestion run finishes well after the triggering request returns — `POST /api/ingest/trigger` spawns a background process and responds immediately (per `docs/API_Specification.docx` section 3.8) — so something needs to poll for completion and show the result even if the user has since navigated to Chat or the Graph view; only `App.jsx` is guaranteed to stay mounted for that. (2) A reset also wipes conversation history (`sessions`/`messages`, not just ingested items — see `storage/sqlite_store.py::reset_all()`), which only `App.jsx`'s own session state can react to (clearing the active chat, refreshing the now-empty sidebar).

There's no way to look up a specific triggered run by its display label (`agent/ingest_trigger.py`'s own docstring notes this — the label is independent of the internal run UUID), so completion detection is approximate: `App.jsx` polls `GET /api/sources/status` every 4 seconds, watching for a `last_run` whose `started_at` is at or after the trigger timestamp (with 2s slack for clock comparison) and whose `status` is no longer `"running"`. Gives up after ~6 minutes (90 attempts) with an informational toast rather than polling forever — a local, single-item-at-a-time ingestion run is expected to finish well inside that window.

**Affects**: `frontend/src/App.jsx`, `frontend/src/components/Toasts.jsx` (new), `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`

---

## 2026-08-30 — Full data reset: `POST /api/admin/reset`

**Context**: No way existed to wipe all ingested data and start over — useful for recovering from a bad ingestion run or restarting a demo from a clean slate, requested directly for the Settings screen.

**Decision**: Added a `reset_all()` function to each storage module — `storage/sqlite_store.py` (`DELETE FROM items`/`ingestion_runs`/`sessions`, cascading to `chunks`/`messages` via existing foreign keys), `storage/chroma_store.py` (fetches every id via `collection.get()`, since Chroma's own `delete()` requires `ids` or a `where` filter — there's no single "delete everything" call), and `storage/neo4j_store.py` (`MATCH (n) DETACH DELETE n`, mirroring the test fixtures' own wipe pattern). The three stores have no shared transaction, so `agent/admin.py::reset_all_data()` attempts all three even if an earlier one fails, collecting failures into a single `AdminError` naming exactly which store(s) didn't reset — better to get as far as possible and report precisely what's left in an inconsistent state than to abort early and leave two stores untouched with no visibility into which. `POST /api/admin/reset` requires an explicit `{"confirm": true}` body field (422 otherwise) — a cheap guard against an accidental call to a destructive, irreversible endpoint, on top of the frontend's own `window.confirm()` prompt.

**Verified against real data**: full backend suite passes, including a real-Neo4j-backed API test that writes SQLite/Chroma/Neo4j data, calls the endpoint, and confirms all three are empty afterward.

**Affects**: `storage/sqlite_store.py`, `storage/chroma_store.py`, `storage/neo4j_store.py`, `agent/admin.py` (new), `api/routes/admin.py` (new), `api/schemas.py`, `api/main.py`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/api/client.js`

---

## 2026-08-30 — Relationship graph view: `GET /api/graph`

**Context**: The frontend had no way to see the Neo4j relationship graph at all — the whole point of the graph layer (relationship detection, `RELATES_TO` edges) was invisible to the user. Requested directly (see issue #57).

**Decision**: Added `storage/neo4j_store.py::get_full_graph()` — one `MATCH (i:Item) RETURN i` plus one `MATCH (a:Item)-[r:RELATES_TO]->(b:Item) RETURN ...` per call, returning a new `GraphSnapshot` dataclass (`nodes: list[ItemNode]`, `edges: list[GraphEdge]`). Deliberately a full-graph fetch, not paginated or filtered — per `Database_Schema.docx` section 5, a node only exists once it has at least one confirmed relationship, so the graph is expected to stay small (unconnected items never appear), and a single-user local system doesn't need pagination here. Added the usual thin pass-through wrapper `agent/graph_view.py::get_graph_snapshot()` (storage → agent → api, never api → storage directly, per `Component_Map.docx`), and `GET /api/graph` (`api/routes/graph.py`) mapping the snapshot to `GraphResponse`/`GraphNodeResponse`/`GraphEdgeResponse` (`api/schemas.py`) — an extension beyond `API_Specification.docx`, which predates this feature.

**Test infrastructure fix along the way**: `tests/test_api/conftest.py`'s shared `driver` fixture does not wipe the real local Neo4j database before/after each test — unlike every other Neo4j `driver` fixture in the codebase (`tests/test_storage/test_neo4j_store.py`, `tests/test_agent/test_graph_view.py`). That's a deliberate, pre-existing choice there (most `test_api/` route tests never write real graph data, so wiping on every test would be wasted work), but it means any test that *does* write real relationship data — like the new `test_graph_route.py` — must wipe around itself rather than relying on the shared fixture to do it. Added an `autouse` `clean_graph` fixture, scoped to that one test file, rather than changing the shared fixture's behavior for every other `test_api/` test.

**Verified against real data**: real `write_relationship()` call against the real local Neo4j instance, followed by a real `GET /api/graph` request against the real running backend, returned the expected node/edge shape; the manually-inserted verification nodes were then removed via a targeted, ID-scoped delete so the real graph was left in its original state.

**Affects**: `storage/neo4j_store.py`, `agent/graph_view.py` (new), `api/routes/graph.py` (new), `api/schemas.py`, `api/main.py`, `tests/test_api/conftest.py` usage pattern (see above), `tests/test_agent/test_graph_view.py` (new), `tests/test_api/test_graph_route.py` (new)

---

## 2026-08-30 — Configurable local watch folders and Notion page scope, from Settings

**Context**: `LOCAL_FILES_WATCH_DIRS` was only editable by hand-editing `config/.env`; Notion had no scoping at all — `extractors/notion.py` always ingested every page the integration could see, with no way to restrict it. Requested directly (see issue #55, partially addressed here — the first-run wizard half is still open).

**Decision**: Added `NOTION_PAGE_IDS` (comma-separated, same pattern as `LOCAL_FILES_WATCH_DIRS`) to `EnvSettings`, with a `notion_page_ids_list` computed property mirroring `watch_dirs`. `extractors/notion.py::extract_new_items()` now fetches each configured page directly by id (`client.pages.retrieve()`) instead of the workspace-wide `client.search()` when `notion_page_ids_list` is non-empty — an unfetchable configured page (deleted, access revoked) is logged and skipped rather than aborting the rest, same resilience pattern as everything else in the pipeline. An empty list keeps the original "whole workspace" behavior unchanged.

Added `config/settings.py::update_source_config()` — the `.env` equivalent of `update_llm_config()`, but simpler: `.env` is flat `KEY=VALUE` lines, so `python-dotenv`'s own `set_key()` already rewrites one line in place without a custom round-trip parser the way `config.yaml`'s structured, commented format needed. New endpoints `GET`/`PUT /api/settings/sources`, and a Settings screen section with a one-folder/id-per-line textarea for each field.

**Verified against real data**: real `dotenv.set_key()` round-trip test confirmed a Windows path with backslashes and spaces (`C:\Users\Shivam D Shinde\...`) and a comma-separated list both survive being written and re-read correctly (single-quoted in the file, not escaped or corrupted). Then, against the real running backend and the real `config/.env`: `GET /api/settings/sources` returned the real configured watch dir; a real `PUT` setting `notion_page_ids` to a test value round-tripped correctly on a follow-up `GET`, and was then restored to its original (empty) value — confirmed the real `.env` file was left correctly restored, not corrupted, afterward.

**Affects**: `config/settings.py`, `extractors/notion.py`, `api/schemas.py`, `api/routes/settings.py`, `frontend/src/api/client.js`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`

---

## 2026-08-30 — `SourceStatus` reports a running total, not just the last run's count

**Context**: `agent/sources_status.py::get_sources_status()`'s `items_processed` is, by design, the count ingested during the *most recent run's window* — legitimately `0` whenever a run finds nothing new (nothing changed since last time). Flagged directly from real use of the running Settings screen: 16 real items existed across three sources, but the screen showed "0 ingested" for all of them, because the most recent run happened to process nothing new. Technically correct, but reads as "nothing has ever been ingested," which isn't true.

**Decision**: Added `SourceStatus.total_items` — a straight `COUNT(*)` per `source_type`, independent of any run window. The Settings screen now shows both (e.g. "12 total, 3 new"), so a source with real historical data reads correctly even when the latest run had nothing to do.

**Verified against real data**: the real `GET /api/sources/status` response, against the real 16-item database, returned `local_file: total_items=12`, `notion: total_items=2`, `browser_history: total_items=2` — matching the real counts — with `items_processed: 0` for all three, correctly reflecting that the most recent run found nothing new.

**Affects**: `agent/sources_status.py`, `api/schemas.py`, `api/routes/sources.py`, `frontend/src/components/SettingsPanel.jsx`

---

## 2026-08-29 — Live, cached connection checks for the Settings screen

**Context**: The Settings screen's "Connected Data Sources" section (`agent/sources_status.py`) reports the *last daily batch run's* outcome per source — before this fix, that meant every source, including the three with no extractor built yet (Gmail, GitHub, Calendar), showed a green "OK" the moment nothing had ever run or failed, which reads as "verified working" when it actually means "nothing has been checked." Flagged directly from using the running frontend against real data.

**Decision**: Added `agent/connection_check.py` — a separate concern from `sources_status.py`, actually checking each source's connectivity/configuration right now: a real, cheap Notion API call (`users.me()`) if `NOTION_API_KEY` is set; a filesystem check for local files' watch directories and the browser history file; and, honestly, a fixed `"not_configured"` for Gmail/GitHub/Calendar with a "not yet built" detail message, never defaulting to `"ok"`. Results are cached in-process (module-level, 5-minute default TTL) since the Notion check is a real network call too expensive to repeat on every Settings-page load. Two new endpoints (extension beyond `docs/API_Specification.docx`, same pattern as `POST /api/ingest/trigger`): `GET /api/sources/connections` (cache-or-fresh) and `POST /api/sources/connections/verify` (always forces a fresh check — the Settings screen's "Reverify" button).

**A bug caught by my own test, before it shipped**: the initial cache implementation derived the cache's age from `_cache[0].checked_at` — an `IndexError` waiting to happen if `check_all_connections()` ever returned an empty list. Fixed by tracking `_cache_time` as its own module-level variable, independent of the cached list's contents.

**Alternatives considered**: Folding connection status into the existing `GET /api/sources/status` response — rejected: that endpoint's meaning ("last batch run's outcome") and this one's ("is it connected right now") are genuinely different questions that happen to share a "per source" shape; conflating them would make the batch-history data harder to reason about. Live-checking on every single `GET` with no cache at all — rejected given the real Notion API call's cost/latency; the cache plus an explicit "Reverify" button gives the same up-to-date-on-demand behavior without paying for a real API call on every page navigation.

**Verified against real data**: the real running frontend, against the real backend and real `config/.env` credentials. `GET /api/sources/connections` returned genuinely live results — `notion: "ok", "Notion API token verified."` (a real API call succeeded), `local_file`/`browser_history: "ok"` (real filesystem checks passed), and `gmail`/`github`/`calendar: "not_configured"` (correctly distinct from "ok" now). Confirmed caching: a second `GET` returned the identical `checked_at` timestamp; `POST /api/sources/connections/verify` returned a new, later timestamp, confirming it bypasses the cache as intended.

**Affects**: `agent/connection_check.py`, `api/routes/sources.py`, `api/schemas.py`, `frontend/src/api/client.js`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`

---

## 2026-08-29 — `openrouter_provider.py` caps `max_tokens` explicitly

**Context**: `providers/openrouter_provider.py::create_openrouter_provider()`'s `ChatOpenAI` construction set no `max_tokens`, defaulting to the routed model's own maximum (64000 for `anthropic/claude-sonnet-4`) — first hit as a real diagnostic-script failure during the boilerplate-fix work (see this file's "Relationship prompt neutralizes shared boilerplate" entry), then again for real while verifying the new LangSmith eval layer end to end: a real answer-synthesis call failed with an OpenRouter 402 because the account's remaining credit balance couldn't cover a 64000-token request. Tracked as GitHub issue #44.

**Decision**: Added `llm.cloud_max_tokens` to `config.yaml`/`config/settings.py::LLMConfig` (default `4096`) and pass it as `ChatOpenAI(..., max_tokens=settings.config.llm.cloud_max_tokens)`. 4096 is generous for every current cloud-routed task (`"answer"`, `"eval"` — both prose-plus-short-JSON responses) without risking the same credit-exhaustion failure on a routine call.

**Alternatives considered**: Leaving `max_tokens` unset and instead catching the 402 and retrying with a lower cap — rejected as needless complexity; a fixed, sane cap prevents the failure outright rather than reacting to it after the first attempt already failed. Making the cap per-task (e.g. a smaller cap for `"eval"` than `"answer"`) — not done; both tasks produce comparably short output, and a single shared setting is simpler until real usage shows otherwise.

**Verified against real data**: before the fix, a real `"answer"` call failed with `402 - This request requires more credits ... You requested up to 64000 tokens`. After the fix, the exact same real question, run through the real agent end to end, succeeded — a real answer citing the correct source, with real faithfulness (1.0) and relevance (1.0) judge calls completing normally, no 402. A second, separate 402 (`in_flight_budget_exhausted`) hit later in the same verification session is a real, external account-level constraint (the OpenRouter account's remaining budget, run down by this session's own real testing) — not a code defect, and not something this fix is meant to address.

**Affects**: `config/config.yaml`, `config/settings.py`, `providers/openrouter_provider.py`

---

## 2026-08-29 — LangSmith evaluation layer: eval task, opt-in tracing, real Dataset + evaluate()

**Context**: `docs/Technical_Design_Document.docx` section 13's "realistic evaluation workflow" — a 20-30 question test set, run through LangSmith after any meaningful architecture change, tracking Recall@K for retrieval alongside faithfulness/relevance for final answers — was entirely unbuilt; `eval/` was an empty package. `docs/File_Folder_Structure.docx` specifies `eval/test_questions.json`, `eval/run_evaluation.py`, and `eval/evaluators.py` (faithfulness/relevance/recall scorers).

**Decisions**:
1. **New `"eval"` provider task**, routed like `"answer"` (cloud in `mixed` mode) — judging answer quality deserves the same caliber of model as generating the answers being judged, and eval runs are infrequent (only `eval/run_evaluation.py`, never the query path), so the extra cost is acceptable. Added `ProviderInterface.generate_eval_judgment(criterion, question, answer, context) -> EvalJudgment` (`criterion` is `"faithfulness"` or `"relevance"`), going through the provider abstraction like every other LLM call — never a raw LangChain call from `eval/`. Generalized the preamble-tolerant JSON parser from the 2026-08-29 boilerplate fix (`_extract_json_object()`) to take a `required_key` parameter instead of hardcoding `"related"`, since eval judgments need the same "model explains itself before the JSON" tolerance the relationship judgments already needed.
2. **Tracing is opt-in, and deliberately *not* wired into `agent/graph.py::run()` itself.** Added `agent/tracing.py::enable_tracing()` (idempotent; sets `LANGSMITH_TRACING`/`LANGSMITH_API_KEY`/`LANGSMITH_PROJECT` env vars, which LangChain/LangGraph's own auto-instrumentation reads — no `langsmith` import needed inside `agent/graph.py` itself). Many tests call the real, unmonkeypatched `get_settings()` (only specific sub-values get monkeypatched, not the whole function) — if tracing enabled itself automatically inside `run()` whenever a real `LANGSMITH_API_KEY` is configured, every test invoking the agent would start sending real trace data to the user's real LangSmith project. Instead, `enable_tracing()` is called explicitly by the two real entry points that should be traced: `api/main.py`'s startup lifespan (tests substitute a no-op lifespan, so this code path never runs during a test — see `tests/test_api/conftest.py`) and `eval/run_evaluation.py`.
3. **The eval runner uploads a real LangSmith Dataset and uses the `langsmith` SDK's `evaluate()`**, not just local console scoring — `_ensure_dataset()` reuses an existing dataset (by name) rather than duplicating it every run, so results stay comparable across runs over time. `eval/test_questions.json` holds 26 questions built against the real, currently-ingested corpus (project design docs + Notion recipe pages) — genuinely runnable and verifiable today, rather than fictional placeholders for sources that don't have extractors yet (Gmail/GitHub/Calendar).
4. **Faithfulness context is reconstructed exactly as `agent/synthesizer.py::synthesize()` builds it** — all of a cited source item's chunks joined with `"\n\n"` — rather than approximated, since `eval/run_evaluation.py::_make_target()`'s `result.sources` correspond 1:1, in order, with what `synthesize()` actually passed to the answer LLM.

**Alternatives considered**: Reusing the existing `"answer"` task for eval judging instead of a new `"eval"` task — considered, but a distinct task keeps "answering questions" and "judging answers" separately traceable in provider routing/logs, matching the precedent set by `"condense"` getting its own task despite similarity to `"relationship"`. Scoring locally without a formal LangSmith Dataset/experiment — rejected per explicit direction: the project's stated purpose is partly to demonstrate applied LangSmith usage, so the idiomatic, dashboard-visible workflow is the point, not a shortcut around it.

**Verified against real data**: real `enable_tracing()` call against the real configured `LANGSMITH_API_KEY` — confirmed `True`. Real `langsmith.Client()` connected to the real account; `_ensure_dataset()` created a real Dataset with all 26 real questions, confirmed readable back via `list_examples()`. Running the full target+evaluator pipeline against one real question surfaced a real, separate bug — documented in this file's own "openrouter_provider.py has no max_tokens cap" entry — rather than a bug in this layer; once that was fixed, re-verified end-to-end (see that entry's verification note).

**Affects**: `providers/base.py`, `agent/tracing.py`, `api/main.py`, `eval/evaluators.py`, `eval/run_evaluation.py`, `eval/test_questions.json`, `pyproject.toml` (added `langsmith` as an explicit dependency)

---

## 2026-08-29 — Ingest trigger spawns the daily batch as a subprocess, not an in-process call

**Context**: `docs/API_Specification.docx` section 3.8 requires `POST /api/ingest/trigger` to manually trigger an ingestion batch. But `docs/Component_Map.docx`'s dependency rules state the Interface layer "depends only on the Agent layer's public entrypoint, never reaching into Storage or Providers directly," and the Agent layer itself "depends on Storage and the LLM Provider, but never on Extractors or the Pipeline layer" — neither layer is documented as allowed to call into the Scheduler. Importing and calling `scheduler/daily_batch.py::_run()` in-process from an API route would cross that boundary directly. It would also mean relationship/ingestion writes running concurrently, in a background thread, against the same long-lived `app.state.conn` SQLite connection normal request handling uses — `storage/sqlite_store.py::connect()`'s own docstring already flags that `check_same_thread=False` lifts Python's same-thread check but adds no locking, so concurrent callers must serialize writes themselves; nothing in the app does that today.

**Decision**: Added `agent/ingest_trigger.py::trigger_ingestion()`, following the same precedent as `agent/health.py` and `agent/sources_status.py` (thin agent-layer wrappers around non-query concerns, so the Interface layer's "depends only on Agent" rule holds even for things that aren't really about the LangGraph agent's reasoning). It spawns `scheduler/daily_batch.py` as an independent OS process via `subprocess.Popen`, exactly as cron/Task Scheduler would — no Python import of `scheduler` anywhere in `api/` or `agent/`, and the spawned process opens its own fresh SQLite/Chroma/Neo4j connections, entirely separate from `app.state`'s. `api/routes/ingest.py` returns `202 Accepted` with `{"status": "started", "run_id": <label>}` immediately, without waiting for the subprocess to finish — matching the spec's literal example (`"run_manual_20260814_1530"`, a human-readable label, not the UUID `start_ingestion_run()` assigns internally). There's no endpoint to look up a run by this label; outcome is checked afterward via the existing `GET /api/sources/status`.

**A real bug found along the way**: the project's own documented invocation — `uv run python scheduler/daily_batch.py`, stated in this file's own module docstring, `FLOW.md`, and `CLAUDE.md`/`AGENTS.md` — fails with `ModuleNotFoundError: No module named 'config'` when actually run. A raw script path only adds *the script's own directory* (`scheduler/`) to `sys.path`, not the project root, so `daily_batch.py`'s absolute imports (`from config.settings import ...`, etc.) can't resolve. `uv run python -m scheduler.daily_batch` (module invocation) adds the current working directory instead, and does work — verified directly, both as a raw shell command and via the new endpoint spawning it for real. This had gone uncaught because the test suite only ever imports `scheduler.daily_batch` as a module (pytest puts the project root on `sys.path` itself), never invokes it as a subprocess the way a real cron job or this endpoint would. Fixed the invocation used by `agent/ingest_trigger.py` and corrected the documented command in this module's docstring and `FLOW.md`; `CLAUDE.md`/`AGENTS.md` are the user's own instruction files, not left to me to edit, so the same correction there is flagged to the user rather than made directly.

**Alternatives considered**: An in-process call via FastAPI `BackgroundTasks` — rejected for the dependency-boundary and connection-sharing reasons above. A `GET /api/ingest/status/{run_id}` companion endpoint to poll a specific triggered run — not built; out of scope for what the spec actually asks for (only `GET /api/sources/status`, which already reports the most recent run regardless of how it was triggered, is specified).

**Verified against real data**: the real endpoint, via a real `TestClient` against the real app (not the test-only no-op lifespan), returned `202` with a `run_manual_...` label; polling the real `ingestion_runs` table afterward showed the spawned subprocess's row transition from `running` to `success` within seconds — confirming the full real path (HTTP request → subprocess spawn → real ingestion pipeline → real SQLite row) works end to end.

**Affects**: `agent/ingest_trigger.py`, `api/routes/ingest.py`, `api/schemas.py`, `api/main.py`, `scheduler/daily_batch.py` (docstring only)

---

## 2026-08-29 — Clear an edited item's relationships before re-detecting them

**Context**: `has_any_relationship()` (added for the bidirectional-duplicate-relationships fix, see below) skips re-judging a candidate if any edge already connects it to the source, in either direction — with no concept of *when* that edge was judged or whether the content behind it has since changed. When an already-related item is edited and re-ingested (`insert_item()`'s upsert updates the existing row, `replace_chunks()` swaps in new chunks/embeddings), `detect_relationships()` runs again — but for any candidate it was already related to, the skip check short-circuits before ever re-judging the pair against the new content. A relationship confirmed against content that's since been completely rewritten stays in the graph indefinitely, never revisited (GitHub issue #43).

**Decision**: `scheduler/daily_batch.py::_process_item()` now returns whether `insert_item()` updated a pre-existing row rather than inserting a new one — free to compute, since the upsert already keeps an existing row's original id, so comparing the returned id to the freshly generated one passed in is enough (no extra query). `_run()` tracks these as `updated_item_ids` and, before the relationship-detection loop, calls a new `storage/neo4j_store.py::delete_relationships_for_item()` for each one — an edges-only delete (unlike `delete_item()`, the node and its properties are left in place) — so re-detection starts clean and `has_any_relationship()` only ever short-circuits relationships already judged against the content currently in the graph.

**Alternatives considered**: Tracking a content hash or timestamp per edge and comparing it against the item's current content on each check — rejected as more moving parts (schema change on the edge, hash computation, staleness-comparison logic) for the same outcome a blunt "clear and re-judge" achieves more simply. The known cost — an edit re-pays the LLM judgment cost for that item's relationships rather than skipping them — is the correct trade-off here: a document that changes should have its relationships re-verified, not left stale.

**Verified against real data**: real SQLite/Chroma/Neo4j and the real embedding model (LLM call faked). A real item was related to another via a genuine confirmed judgment; its content was then replaced with something unrelated (simulating an edit); `delete_relationships_for_item()` correctly removed all of its edges (not just the one under test); re-running detection against the new content, with the LLM correctly saying "not related," left it with zero relationships rather than the stale one persisting. Also verified via `scheduler/daily_batch.py::_run()` integration tests: an edited item that becomes genuinely unrelated loses its stale edge; an unedited item re-processed in the same batch (every item is reprocessed when `since` doesn't filter anything out) keeps its still-valid relationship, since re-detection re-confirms it rather than just failing to delete it.

**Affects**: `storage/neo4j_store.py`, `scheduler/daily_batch.py`

---

## 2026-08-29 — Per-candidate source chunk selection, and a distance cutoff before LLM judgment

**Context**: Two remaining weak spots in relationship detection, raised in discussion after the boilerplate-parsing fix above. First: the LLM confirmation call always showed the *source* item's first chunk (`chunks[0].text`) as its representative text, regardless of which candidate was being judged — arbitrary in the same way the pre-2026-08-24 narrowing query was before it switched to a pooled whole-document embedding, just one stage later in the pipeline. Second: Chroma's narrowing search always returns its top-K nearest neighbors, even when none of them are genuinely close — a document with no real matches anywhere in the corpus still gets `relationship_candidate_count` arbitrary candidates judged by the LLM, each one a chance for a spurious confirmation.

**Decision**:
1. **Per-candidate source chunk selection.** Added `storage/chroma_store.py::get_item_chunk_vectors()` (each chunk's text paired with its own embedding, unlike `get_item_embeddings()` which returns vectors only). For each candidate, `pipeline/relationships.py` now computes the candidate's whole-document embedding (mean of its own chunks) and picks whichever of the *source's* chunks is closest to it by cosine similarity (`_most_similar_chunk_text()`) — a different chunk may be selected for different candidates of the same source item. The candidate side needed no equivalent change: Chroma's search result *is* the candidate's actual matching chunk already, since narrowing queries with the source's pooled embedding in the first place — only the source side lacked a natural "the chunk that matched," since its pooled vector isn't tied to any one of its own chunks.
2. **A distance cutoff before judgment.** Added `retrieval.relationship_candidate_max_distance` (`config.yaml`, default `0.6`, cosine distance; `null` disables it). A candidate whose Chroma-returned `distance` exceeds this is skipped before `has_any_relationship()` or any LLM call — cheaper than the check it replaces, since it's a comparison against a number Chroma already returned, not a new computation.

**Alternatives considered**: Summarizing each candidate document with an LLM call and using the summary as confirmation text — rejected: needs caching to avoid re-summarizing the same item on every future comparison (schema work), runs on the cheap local model so risks dropping the exact detail that would've mattered, and doesn't reliably avoid the boilerplate problem either (a summarizer could just as easily keep a boilerplate line it judges "important"). Deleting an item's relationships and re-detecting from scratch whenever it's edited (a separate, related staleness gap raised in the same discussion, where `has_any_relationship()` never revisits an edge once written even if the endpoint's content later changes) — deliberately deferred, out of scope for this change; noted here so it isn't lost.

**Verified against real data**: real distance calibration first, then real end-to-end runs. Sampled candidate-narrowing distances for 16 real ingested items (project design docs, Notion recipes, browser history) — same-project docs clustered at cosine distance ≈0.22–0.5, while genuinely unrelated pairs (recipes vs. design docs) jumped to ≈0.57–0.9+, giving a real, corpus-derived basis for the `0.6` default rather than a guessed number. Then, against the real SQLite/Chroma/Neo4j stores and the real embedding model (LLM call faked): (1) a source item with an irrelevant first chunk (a generic "internal notes" header) and a relevant second chunk correctly had the *second* chunk's text sent to the LLM once a genuinely related candidate was found — confirmed by inspecting the exact prompt argument, not just the outcome; (2) with an artificially strict cutoff (`0.05`), a clearly unrelated real candidate (a pasta recipe vs. a storage-layer doc) was filtered before any LLM call, and zero LLM calls were made at all; (3) with the real configured default (`0.6`), a genuinely related real pair was still judged normally, confirming the default doesn't filter out real relationships in this corpus.

**Affects**: `storage/chroma_store.py`, `pipeline/relationships.py`, `config/settings.py`, `config/config.yaml`

---

## 2026-08-29 — Relationship prompt neutralizes shared boilerplate; parser tolerates a reasoning preamble

**Context**: The 2026-08-24 relationship-confirmation fix (skepticism-by-default prompt + confidence threshold) was only ever verified against the cheap `anthropic/claude-3-haiku`, and a real false positive remained open (see `memory/relationship_confidence_pattern_matching.md`): two texts sharing a literal boilerplate line ("Companion document to: <some other document>") got confirmed as `companion_to`, even though the line doesn't name either text as the other's companion — it names a third, unrelated document. Re-tested against the actual configured default, `anthropic/claude-sonnet-4` (real OpenRouter calls; temporarily set `provider_mode: fully_cloud` for the test, reverted immediately after): the same false positive reproduced, with *higher* confidence (1.0 vs. haiku's 0.9) — ruling out "model capability" as the cause and confirming it's a genuine prompt-design flaw, not something a stronger model fixes on its own.

**Decision**: Added an explicit instruction to `_build_relationship_prompt()`: before judging, name any sentence/header/phrase that appears verbatim in both texts, and treat such shared boilerplate as *not* evidence of a relationship unless it specifically names the other text (not a third document) as companion/reference. Re-tested against real `claude-sonnet-4`: this alone made the model correctly reject the false-positive pair — but also made it reliably explain its reasoning before the JSON, violating the prompt's existing "respond with ONLY JSON" instruction and breaking `_parse_relationship_response()`'s `json.loads(raw)`. Rather than fighting the model back into silent, unexplained JSON-only output (real-tested and unreliable), added `_extract_json_object()`: scans the response for brace-balanced `{...}` substrings and returns the last one that's valid JSON containing a `related` key, tolerating a reasoning preamble instead of requiring pure JSON.

**Alternatives considered**: Stripping boilerplate text programmatically before it reaches the prompt (the original memory's suggestion) — rejected as harder to get right than it sounds: "boilerplate" would need identifying without domain-specific heuristics (exact-substring matching two chunks that happen to be different lengths, header vs. body ambiguity), whereas the LLM can already recognize verbatim-shared text with a plain-language instruction. Forcing the model into single-shot JSON-only output via a lower `temperature` or a stricter format instruction — not attempted, since OpenRouter's `ChatOpenAI` wrapper here doesn't expose reliable structured-output enforcement across arbitrary routed models, and the preamble-tolerant parser is simple and already real-verified.

**Verified against real data**: four real `claude-sonnet-4` calls via OpenRouter (capped `max_tokens=2000` for cost — the app's default, uncapped, exceeded the available credit balance on a first attempt; noted as a separate, pre-existing latent issue in `providers/openrouter_provider.py`, not fixed here since it's out of scope for this fix). (1) The original false-positive pair (cookie recipe vs. ingestion design, sharing the boilerplate line) → correctly rejected (`None`), where it was previously wrongly confirmed. (2) A genuine positive control — a text whose "Companion document to:" line actually names the other text's real title → correctly confirmed as `companion_to`. (3) A genuinely related pair with no shared boilerplate at all (a design plan and its implementation notes) → still correctly confirmed as `implements`, confirming the fix didn't make the model over-conservative generally. (4) A genuinely unrelated pair with no shared boilerplate → still correctly rejected.

**Affects**: `providers/base.py`

---

## 2026-08-29 — Condense follow-up questions into a standalone query before retrieval

**Context**: Conversation history reaches the answer-synthesis prompt
(`providers/base.py::generate_answer(..., history=...)`), but the Router
and search nodes (`agent/router.py`, `agent/search_nodes.py`,
`agent/graph_traversal.py`) only ever see the current turn's raw question
text. A vague follow-up like "why was the second one chosen?" has no
topical keywords of its own, so retrieval has nothing to go on —
previously verified against a real two-turn conversation where a follow-up
retrieved completely unrelated content (see
`memory/followup_question_retrieval_gap.md`).

**Decision**: Added `ProviderInterface.generate_search_query(question,
history) -> str` (a new `"condense"` task) that rewrites a follow-up into
a standalone query, resolving references like "it"/"that"/"the second
one" against the prior turns. `agent/graph.py` runs this as a new
`condense_query` node, immediately after `START`, only when the session
has history (a fresh, first-turn question is already standalone, so this
skips the LLM call entirely in the common case). Its output becomes
`search_query` in the graph's shared state; the Router and both search
nodes retrieve on `search_query` instead of `question`. The original
`question` is untouched everywhere else — it's still what's persisted to
`sessions`/`messages` and what's passed to `generate_answer()`, so the
user-visible transcript and the final answer's phrasing are unaffected.
A `ProviderError` from the condense call is caught and logged, falling
back to retrieving on the raw question rather than failing the whole
turn — a degraded (but not broken) follow-up is better than an error.
In `provider_mode: mixed`, `"condense"` is routed to Ollama alongside
`"metadata"`/`"relationship"`, not OpenRouter: it runs on every follow-up
question, and a rough rewrite that still improves retrieval over the raw
follow-up is good enough — the final answer is what actually needs the
better model.

**Alternatives considered**: Passing `history` directly into the Router's
regex heuristics and each search node — rejected because it would spread
history-handling logic across three separate call sites and couple
retrieval's keyword/semantic matching to conversation state instead of a
single upstream rewrite; a dedicated condensing step keeps history
resolution in one place and lets every downstream node stay
history-agnostic. Embedding the last turn's answer text directly into the
vector search query (no LLM call) — rejected as too blunt: it would bias
toward whatever was retrieved last turn even for an unrelated follow-up,
where a real rewrite (or no rewrite, for a fresh topic change) is needed.

**Verified against real data**: ran `agent/graph.py::run()` end-to-end
against the real SQLite store, a real Chroma collection, the real
embedding model, and the real Neo4j instance (only the condense/answer
LLM calls faked, local Ollama's configured model unavailable in this
environment) for a real two-turn conversation: turn one asked about
storage technology choices; turn two, a deliberately vocabulary-free
follow-up ("Why was the second one picked?"), correctly retrieved the
same real item once condensed into a query naming the actual technology —
confirming the condensed query, not the raw follow-up, drives retrieval,
and that history correctly reaches the condense call (2 prior turns) while
a fresh session makes zero condense calls.

**Affects**: `providers/base.py`, `agent/graph.py`

---

## 2026-08-29 — Skip relationship candidates already connected in either direction

**Context**: `pipeline/relationships.py::detect_relationships()` runs once
per newly-processed item, independently narrowing candidates and asking the
LLM to judge each one. Because it runs per-item rather than per-pair, the
same real-world relationship between item A and item B can get judged
twice: once when A is processed (writing `A -[label]-> B`), and again,
independently, when B is later processed (writing a second, separate
`B -[label2]-> A` edge — possibly with a different label from a second,
independent LLM call). `storage/neo4j_store.py::write_relationship()`'s
`MERGE` is keyed on direction + label, so it has no way to recognize these
as "the same relationship" and dedupe them itself.

**Decision**: Added `storage/neo4j_store.py::has_any_relationship(driver,
item_a, item_b) -> bool`, using an undirected Cypher pattern
(`(a)-[:RELATES_TO]-(b)`) that matches an edge in either direction
regardless of label. `detect_relationships()` now calls this before every
LLM judgment call and skips the candidate entirely (no LLM call, no graph
write) if it returns `True`. This makes the check itself the source of
truth for "already related" — cheaper than an LLM call, and avoids ever
writing a second, possibly differently-labeled edge for the same
underlying relationship.

**Alternatives considered**: Deduplicating in `write_relationship()` itself
(e.g. `MERGE` an undirected relationship) — rejected because Neo4j
relationships are inherently directional, and losing direction would lose
information (e.g. "implements" reads differently depending on which item
is the implementer). Deduplicating after the fact with a periodic cleanup
job — rejected as unnecessary complexity when the write can simply be
prevented at judgment time, and it would still cost an LLM call per
redundant pair either way.

**Verified against real data**: ran `detect_relationships()` against the
real SQLite/Chroma/Neo4j instances (the LLM call faked, same as the
integration test suite — local Ollama wasn't reachable in this
environment). Inserted two genuinely near-duplicate real items, ran
detection from item A first (wrote an edge to B among its confirmed
candidates), then ran detection from item B: item A's text was never sent
to the LLM on the second run, and exactly one edge exists between A and B
afterward — confirming the skip check works against the real graph and
real candidate-narrowing query, not just the mocked test fixtures.

**Affects**: `storage/neo4j_store.py`, `pipeline/relationships.py`

---

## 2026-08-25 — `ChatWindow` decides once, at mount, whether to load session history

**Context**: The sidebar now lists real sessions and lets the user pick
one to reopen. `ChatWindow` needs to load that session's history when
opened, but must *not* re-fetch and clobber its own local message state
mid-conversation as new turns come in (its `sessionId` prop also changes
after the very first turn in a brand-new chat, once the backend assigns a
real id).

**Decision**: `App.jsx` forces a full `ChatWindow` remount (a bumped `key`)
whenever the *user* changes which session is active — "New chat" or
picking a different sidebar entry — never on the ordinary `sessionId`
prop update that happens after a turn completes within the same instance.
`ChatWindow` itself reads its `sessionId` prop only inside a
mount-only `useEffect` (empty dependency array) to decide whether to call
`getSessionHistory()`; it holds its *own* `sessionId` state afterward,
updated locally as turns complete, and never re-fetches based on prop
changes.

**Verified against real data**: fetched a real, previously-recorded
two-turn session (from the earlier conversation-memory verification round)
via the real `GET /api/sessions` and `GET /api/sessions/{id}` endpoints —
confirmed the exact real messages, roles, timestamps, and sources come
back in the shape `ChatWindow`/`MessageBubble` expect.

**Affects**: `frontend/src/App.jsx`, `frontend/src/components/ChatWindow.jsx`,
`frontend/src/components/Sidebar.jsx`, `frontend/src/api/client.js`

---

## 2026-08-25 — `GET /api/sessions/{id}` returns a plain 404 JSON response, not `HTTPException`

**Context**: An unknown `session_id` needs to be a 404 (per REST convention
and the general shape of the other endpoints), but FastAPI's `HTTPException`
gets handled by Starlette's own default `HTTPException` handler — which
takes precedence over `api/main.py`'s registered `Exception` handler — and
returns `{"detail": str}`, not this project's `{"error": str, "detail":
str}` shape from `API_Specification.docx` section 2.

**Decision**: `api/routes/sessions.py::get_session_history()` returns a
`JSONResponse(status_code=404, content={"error": "not_found", ...})`
directly from the route function instead of raising `HTTPException` —
simplest way to keep this one case consistent with every other error shape
in the API, without adding a sixth registered exception handler just for
`starlette.exceptions.HTTPException`.

Also: session/message reads go straight through `storage/sqlite_store.py`
from `api/routes/sessions.py`, not via an `agent/` module — same reasoning
as `api/routes/settings.py` (`DECISIONS.md`, 2026-08-25): this is a plain
data read, not agent reasoning, so there's no meaningful "agent" step to
route it through.

**Affects**: `api/routes/sessions.py`

---

## 2026-08-25 — `agent/graph.py::run()` resolves the session, loads history, and persists the turn — around the graph, not inside it

**Context**: With `storage/sqlite_store.py::record_conversation_turn()`
and `providers/base.py`'s `history` parameter both in place, something
has to load a session's prior turns before the graph runs and persist the
new turn after — and decide where that logic lives relative to the
`StateGraph` itself.

**Decision**: `run()` does this work itself, outside `_build_graph()`'s
node sequence: resolves `session_id` (existing or fresh) and loads history
via a new `_load_history()` helper *before* calling `graph.invoke()`
(added to the initial state so `synthesize_node` can pass it to
`synthesize()`), then calls `record_conversation_turn()` *after* the graph
returns, using the final answer/sources. Kept out of the graph's own node
sequence because session resolution and persistence aren't part of the
retrieval pipeline the router/merger/synthesizer nodes model — they're
framing around one call to it, the same way `scheduler/daily_batch.py`'s
`main()` wraps `_run()` with connection setup/teardown rather than making
connection lifecycle a pipeline "step."

**Verified against real data — real limitation found**: ran a real two-turn
conversation against the real ingested docs corpus. Turn 1 ("What storage
technologies does this system use?") got a correct, real answer listing
SQLite/Chroma/Neo4j. Turn 2 ("Why was the second one chosen over
alternatives?") — meant to resolve to Chroma — instead answered about
*frontend framework choice* (React vs. plain HTML/JS), a completely
different part of the docs. Root cause, confirmed directly: history *did*
reach the prompt correctly (the persisted messages show both turns
recorded correctly), but **retrieval (vector/keyword search) runs on the
current question's raw text only** — "Why was the second one chosen over
alternatives?" carries no topical keywords of its own, so it retrieved
whatever scored best for that literal wording, which happened to be an
unrelated section that also discusses "two options... second chosen."
History reaching the LLM prompt was necessary but not sufficient — the
retrieval step itself needs to be history-aware (e.g. rewriting/expanding
a vague follow-up question using prior turns before running search) for
follow-up questions to reliably retrieve the *right* context. This is a
real, open gap, not yet fixed — noted here rather than glossed over.

**Affects**: `agent/graph.py`

---

## 2026-08-25 — Conversation history is passed to `generate_answer()` as a separate, non-cited parameter

**Context**: `Technical_Design_Document.docx` section 8.4 says follow-up
questions ("tell me more about the second one") should work via LangGraph
state carrying prior context, but `ProviderInterface.generate_answer()`
had no way to receive it — only the current question and retrieved
context.

**Decision**: Added `providers/base.py::ConversationTurn` (`role`, `text`)
and a `history: Sequence[ConversationTurn] = ()` parameter to
`generate_answer()`, threaded through `agent/synthesizer.py::synthesize()`
the same way. `_build_answer_prompt()` renders history as its own labeled
section, explicitly told to the model as being *for resolving references
only, not citable content* — keeping the existing citation numbering
(`[1]`, `[2]`, …) tied only to the actual retrieved context, so history
can't accidentally get cited as if it were a source. Empty by default, so
every existing caller (and every existing test double implementing
`generate_answer`) needed a matching default — updated in
`tests/test_agent/test_graph.py` and `test_synthesizer.py`'s fakes, caught
immediately by the full suite when the real signature changed underneath
them.

**Affects**: `providers/base.py`, `agent/synthesizer.py`

---

## 2026-08-25 — Session/message storage: new tables, a single `record_conversation_turn()` write path, session title auto-generated from the first question

**Context**: Conversation memory needs somewhere to persist sessions and
their messages, but `Database_Schema.docx` predates this feature — it has
no `sessions`/`messages` tables. `UIUX_Wireframes.docx` section 2.1 says
the sidebar lists sessions "by auto-generated title" without specifying how
the title is generated.

**Decision**: Two new tables, `sessions` (`id`, `title`, `created_at`,
`updated_at`) and `messages` (`id`, `session_id`, `role`, `text`,
`sources` — JSON-encoded, `created_at`), following this file's existing
schema conventions (`ON DELETE CASCADE`, an index on the foreign key). A
single `record_conversation_turn(conn, session_id, question, answer,
sources)` is the one write path: it upserts the session (creating it with
an auto-generated title on first turn, only refreshing `updated_at` on
later turns — an `INSERT ... ON CONFLICT DO UPDATE`, same pattern as
`insert_item()`) and inserts both the user and agent messages in one call,
so callers never have to remember to do these three things in the right
order separately.

The title is the question text itself, truncated to 60 characters — not a
dedicated LLM summarization call. Cheaper, zero added latency on every
first turn, and a truncated question is already a reasonable session label
(matching the wireframe's own examples: "RAG pipelines...", "Interview
prep...").

`Message.sources` is stored/returned as plain `list[dict]`, not
`agent/synthesizer.py`'s `Source` dataclass — this module never imports
from `agent/`, per the project's one-way dependency rule (storage sits
below agent). Conversion to a typed shape is the caller's job.

**Alternatives considered**: An LLM call to generate a "real" summary
title — rejected as unnecessary cost/latency for a V1 feature; can be
revisited later if truncated-question titles prove genuinely unclear in
practice. Separate `create_session()`/`add_message()`/`touch_session()`
functions instead of one `record_conversation_turn()` — rejected as more
surface area for callers to get wrong (e.g. forgetting to touch the
session on a later turn); every real caller needs all three writes
together anyway.

**Affects**: `storage/sqlite_store.py`

---

## 2026-08-25 — Frontend scaffolded with Vite; default `oxlint` replaced with ESLint per `Coding_Conventions.docx`

**Context**: `Technical_Design_Document.docx` section 9.2 already decided
on a React frontend over plain HTML/JS. `Coding_Conventions.docx` section 3
specifies "linting via ESLint with the standard React hooks rule set,"
but the current `npm create vite@latest -- --template react` scaffold
defaults to `oxlint`, not ESLint.

**Decision**: Scaffolded via Vite (fast, standard, matches "fastest to
build" from the design doc's reasoning for choosing React), then swapped
`oxlint` for `eslint` + `eslint-plugin-react-hooks` +
`eslint-plugin-react-refresh`, and added `prettier` (also required by the
conventions doc, absent from the default scaffold). Component styling uses
one shared stylesheet (`src/index.css`) rather than CSS modules — the
conventions doc explicitly allows either, and a single stylesheet is
simpler for an app this size. Two screens (chat, settings) are switched
via local `useState`, not a router — the wireframe only describes a
"Settings link," no deep-linking requirement, so a router would be
unused complexity.

**Known, deliberate gap**: the session sidebar always shows "No past
conversations yet" — there's no `GET /api/sessions` endpoint yet (needs
real session persistence, the conversation-memory phase). Built this way
honestly rather than faking session data, per the same reasoning the
`/api/sessions` endpoints themselves were deferred earlier this session.

**Affects**: `frontend/` (new)

---

## 2026-08-25 — Frontend API client uses `127.0.0.1`, not `localhost`

**Context**: Verified directly against this real dev machine:
`http://localhost:8080` didn't reach the real FastAPI backend at all — an
unrelated WSL/Docker port-forward was already listening on `[::1]:8080`
(IPv6), and this machine's DNS resolution tried `::1` before `127.0.0.1`
for the hostname `localhost`, so requests silently went to the wrong
service and got a nonsensical response instead of a connection error that
would have been obvious immediately.

**Decision**: `frontend/src/api/client.js`'s `API_BASE_URL` uses
`http://127.0.0.1:8080/api` explicitly rather than `localhost`, removing
the ambiguity entirely. `api/main.py`'s CORS `allow_origin_regex` still
accepts both `localhost` and `127.0.0.1` origins (the Vite dev server
itself could still come up on either), since the fix only needed to be on
the *request target* side, not the *origin* side.

**Affects**: `frontend/src/api/client.js`

---

## 2026-08-25 — CORS enabled for `localhost`/`127.0.0.1` origins on any port

**Context**: The React dev server (Vite) runs on its own port (5173 by
default), a different origin from the FastAPI backend (8080) even though
both are local — the browser blocks `fetch()` calls across origins unless
the server opts in via CORS headers. `System_Architecture_Document.docx`
section 6.2 says frontend/backend "communicate only over localhost" but
doesn't address the browser's same-origin policy, since that's an
implementation detail below the architecture doc's level.

**Decision**: `api/main.py` adds `CORSMiddleware` with
`allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+"` — any port, so
Vite's dev server still works if its default port is already taken and it
picks a different one — rather than a wildcard origin, matching the
project's local-only, single-user framing.

**Affects**: `api/main.py`

---

## 2026-08-25 — `PUT /api/settings` writes `config.yaml` with `ruamel.yaml`'s round-trip mode, not plain PyYAML

**Context**: `config/settings.py::load_config()` already reads `config.yaml`
with plain `yaml.safe_load()`, which is fine for reading. But
`PUT /api/settings` needs to *write* it back, and `config.yaml` is a
hand-maintained file where every setting has an inline comment documenting
its valid values (`provider_mode: mixed # fully_local | fully_cloud |
mixed`) — a first attempt using plain `yaml.safe_load()` + `yaml.safe_dump()`
would silently discard every comment and reformat the whole file (verified
directly: it also stripped the quotes off `schedule: "0 23 * * *"` and
reindented the list entries) the very first time a user changes a setting
through the Settings screen.

**Decision**: `config/settings.py::update_llm_config()` uses `ruamel.yaml`'s
`YAML()` round-trip mode (`preserve_quotes = True`,
`indent(mapping=2, sequence=4, offset=2)` — tuned to match this file's
existing indentation exactly) to load, mutate only the `llm` section's
given keys in place, and write back — verified directly against the real
`config.yaml`: a diff after the write showed only the two intended lines
changed, comments/quotes/list formatting all untouched. The new `llm`
section is validated via `LLMConfig.model_validate()` *before* anything is
written to disk, so a bad update (an invalid `provider_mode`) can't
corrupt the file. A `path` parameter (defaulting to the real file) lets
tests write to a temp file without ever touching the real `config.yaml` or
the process-wide `get_settings()` cache — that cache is only invalidated
when the write actually targeted the default path.

**Alternatives considered**: Plain PyYAML dump — rejected outright, as
verified above. Hand-rolled regex-based line patching — rejected as more
fragile and harder to reason about than a real round-trip-preserving YAML
parser for what's still structured editing.

**Affects**: `config/settings.py`, `api/routes/settings.py`

---

## 2026-08-25 — Per-source item counts/status for `GET /api/sources/status` are derived, not stored

**Context**: `Database_Schema.docx`'s `ingestion_runs` table has a single
aggregate `items_processed` integer and one `error_log` string across all
sources for a run — but `API_Specification.docx` section 3.3 wants a
per-source breakdown (`{"source_type": "notion", "items_processed": 4,
"status": "ok"}` for each of the six sources). Extending the schema with a
per-source breakdown column, or a new child table, would work but is a
real schema change for a read-only display endpoint.

**Decision**: `agent/sources_status.py::get_sources_status()` derives both
numbers from data that already exists: `items_processed` per source is a
`COUNT(*)` on `items` filtered to `source_type` and `ingested_at` within
`[run_started_at, run_completed_at]` (or through now, if still running);
`status` is `"error"` if the run's `error_log` string contains that source's
name (every error message `scheduler/daily_batch.py` writes is prefixed
`"{source_name}: ..."` or `"{source_name}/{item_id}: ..."`, so a substring
check is reliable here), else `"ok"`. All six source types are always
listed, even ones with no extractor built yet (0 items, `"ok"` — nothing
has failed if nothing has run).

**Alternatives considered**: A dedicated per-source breakdown table —
deferred; the derived approach needs no migration and no changes to
`scheduler/daily_batch.py`'s existing bookkeeping, at the cost of being an
approximation (an item's `ingested_at` timestamp could in principle fall
just outside the run's recorded window under clock skew, though in
practice it's set during that exact run). Worth revisiting only if this
approximation turns out to be wrong in practice.

**Affects**: `agent/sources_status.py`, `api/routes/sources.py`,
`storage/sqlite_store.py` (`get_last_ingestion_run()`, new)

---

## 2026-08-25 — Errors are mapped to `{error, detail}` via handlers registered once in `api/main.py`

**Context**: `API_Specification.docx` section 2 requires every error
response to be `{"error": string, "detail": string}` with a standard HTTP
status code, but doesn't specify HTTP status codes for specific failure
types, or where the mapping logic should live.

**Decision**: Four exception handlers registered once in
`api/main.py::_register_exception_handlers()`, applying to every route
(not just `/api/query`): `RequestValidationError` → 422, `ProviderError`
(an LLM call failed after retries) → 502 (the failure is in an upstream
dependency, not this service), `VectorStoreError`/`GraphStoreError`/
`StorageError` (Chroma/Neo4j/SQLite) → 500, and any other exception → 500
with a generic `"internal_error"`. Route handlers stay free of
try/except noise — they just call `agent/graph.py::run()` and let
failures propagate.

The four storage/provider exception *types* are imported into `api/main.py`
purely so the handlers can pattern-match on them for HTTP status mapping —
not to call anything in `storage`/`providers`. This is presentation-layer
plumbing done once, centrally, not business logic reaching into those
layers from a route module (`api/__init__.py`'s stated layering rule is
about the latter).

**Verified against real data**: a real query against the real production
stores actually hit `ProviderError` — the configured OpenRouter account is
out of credits for `claude-sonnet-4` — and the handler correctly returned
`502 {"error": "provider_error", ...}` rather than a raw traceback. Worth
noting for whenever real `/api/query` usage is exercised again: OpenRouter
credits need topping up, or `cloud_model` switched to a cheaper model, for
the default `mixed` provider mode to actually answer queries right now.

**Affects**: `api/main.py`

---

## 2026-08-25 — FastAPI resources live on `app.state`, tests inject a no-op lifespan

**Context**: `api/main.py` needs to hold a long-lived SQLite connection,
Chroma collection, and Neo4j driver for the whole app process, opened once
at startup rather than per-request (matching `scheduler/daily_batch.py`'s
`main()`/`_run()` split). Tests need to exercise real routes via FastAPI's
`TestClient` without ever triggering the real, settings-derived
connections — the same "don't silently touch real resources in tests"
lesson from `scheduler/daily_batch.py`'s `_EXTRACTORS` test-isolation bug
(DECISIONS.md, 2026-08-24) applies here just as much.

**Decision**: `create_app(*, lifespan_fn=lifespan)` takes the lifespan
context manager as a parameter, defaulting to the real one. Tests pass a
no-op lifespan and set `app.state.conn`/`collection`/`driver` directly to
test doubles before making requests. Route handlers read resources from
`request.app.state` rather than via FastAPI's `Depends()`-based dependency
injection — simpler here since nothing needs `dependency_overrides`, and
route modules never need to import anything back from `api/main.py` (no
circular-import concerns to design around either).

**Alternatives considered**: `Depends()`-based dependency functions with
`app.dependency_overrides` in tests — rejected as more machinery than this
app needs; `app.state` plus an injectable lifespan achieves the same test
isolation more simply.

**Affects**: `api/main.py`, `tests/test_api/conftest.py`

---

## 2026-08-25 — SQLite connections open with `check_same_thread=False`

**Context**: `api/main.py` opens one SQLite connection at startup and
holds it in `app.state` for the app's lifetime — necessary because
`agent/graph.py::run()`'s signature takes an already-open `conn` (matching
`scheduler/daily_batch.py`'s established `_run(conn, collection, driver)`
pattern). FastAPI runs synchronous route handlers in a threadpool, though,
so the thread handling a given request is never the thread that opened the
connection during lifespan startup. `sqlite3.connect()` defaults to
`check_same_thread=True`, which raises `sqlite3.ProgrammingError` the
moment a connection is used from any thread other than the one that
created it — this was caught immediately by a real API test failure, not
theoretically.

**Decision**: `storage/sqlite_store.py::connect()` now passes
`check_same_thread=False`. This is a narrowly scoped fix: it lifts
sqlite3's same-*thread* restriction but adds no locking of its own, so
genuinely concurrent access from multiple threads at the same instant is
still the caller's responsibility to serialize. Accepted as reasonable for
this project's actual scale — a single-user, localhost-only chat interface
realistically sees one in-flight query at a time, not concurrent write
contention.

**Alternatives considered**: Opening a fresh SQLite connection per request
instead of sharing one — rejected as unnecessary overhead (a fresh
connection re-runs schema initialization every request) for a project
whose actual concurrency profile doesn't need it; worth revisiting if this
project ever needed genuine concurrent-request support. Adding a
`threading.Lock()` around every SQLite call — rejected as unneeded
complexity for the same reason.

**Affects**: `storage/sqlite_store.py`

---

## 2026-08-25 — Health check lives in `agent/health.py`, not `api/routes/health.py`

**Context**: `api/__init__.py`'s own docstring states the API layer
"depends only on the agent's public entrypoint, never reaching into
`storage` or `providers` directly," but a meaningful health check has to
individually probe SQLite, Chroma, Neo4j, and the configured LLM provider
— all storage/provider-layer concerns.

**Decision**: The actual check logic (`check_health()`) lives in a new
`agent/health.py`, since `agent/__init__.py`'s docstring is the layer
explicitly allowed to depend on `storage`/`providers`. `api/routes/health.py`
does nothing but call it and shape the response — it never imports from
`storage`/`providers` itself, preserving the stated layering rule.
`agent/health.py` isn't in `File_Folder_Structure.docx`'s documented file
list, but is a minimal, narrowly-scoped extension consistent with the
existing `agent/` package boundary. The `llm_provider` check only confirms
the configured provider can be *constructed* (catches a missing API key,
the most common misconfiguration) — it never makes a real LLM API call,
which would add real cost/latency to every health check.

**Affects**: `agent/health.py`, `api/routes/health.py`

---

## 2026-08-25 — Agent graph uses a linear StateGraph with internal short-circuiting, not conditional-edge fan-out

**Context**: `agent/router.py::route()` decides which of vector search,
keyword search, and graph traversal should actually do work for a given
question. LangGraph supports this with conditional edges (a router
function returning which node(s) to visit next), which is the more
"native" way to express conditional branching in a StateGraph.

**Decision**: `agent/graph.py` instead wires all six nodes into one fixed,
linear sequence (`router → vector_search → keyword_search →
graph_traversal → merge → synthesize → END`) via plain `add_edge()` calls,
and has each retrieval node check `state["decision"]` itself, returning an
empty hit list as a cheap no-op when its flag is `False` rather than being
skipped by the graph structure. Reasoning: every "skip" here is a
near-zero-cost no-op (an empty list, no I/O), so conditional-edge fan-out
buys nothing functionally while adding real complexity — fan-out/fan-in
bookkeeping, and reducer functions on shared state fields to handle
concurrent branch writes safely. A fixed linear graph keeps every node a
plain, individually testable function and keeps the state schema simple
(no reducers needed, since nothing runs concurrently).

**Alternatives considered**: Conditional edges — rejected per the above;
worth revisiting only if a future node becomes expensive enough that
literally not calling it (vs. calling it and cheaply no-opping) matters,
which isn't the case for any of the current six nodes.

**Scope note — conversation memory deferred**: `run()` accepts
`session_id` per `API_Specification.docx`'s `POST /api/query` contract
(generating a new one via `uuid4` if not given), but this pass does not
implement multi-turn conversation memory — the state schema has no running
message history, and the synthesizer prompt doesn't incorporate prior
turns. `Technical_Design_Document.docx` section 8.4's follow-up-question
support ("tell me more about the second one") is real, separate work
(conversation history in state, coreference-aware prompting) deliberately
left for a dedicated follow-up unit of work rather than bolted on here.

**Affects**: `agent/graph.py`

---

## 2026-08-25 — Answer Synthesizer caps context at `retrieval.top_k_vector`, with a no-LLM-call fallback

**Context**: `agent/merger.py::merge()` can return many ranked items (up
to the sum of whatever vector/keyword/graph-traversal contributed), but
neither `Technical_Design_Document.docx` nor `config.yaml` specifies how
many of those the synthesizer should actually pull full text for and pass
to the LLM as context — passing all of them would be expensive, slow, and
likely to dilute the answer with marginally-relevant material.

**Decision**: `agent/synthesizer.py::synthesize()` takes only the top
`top_k` merged results (default `config.yaml`'s `retrieval.top_k_vector`)
— reusing an existing config value rather than introducing a new,
undocumented knob with no obvious default of its own. Separately, if no
merged result yields usable context (empty input, or every item was
deleted from SQLite / has no chunks), the function returns a fixed
"nothing found" answer without calling the LLM at all — there's nothing
meaningful to ground an answer in, so a real API call would just be wasted
cost and latency for a response the code can already determine in advance.

**Alternatives considered**: A dedicated `retrieval.top_k_synthesis` config
field — deferred; reusing `top_k_vector` is a reasonable default for now,
and a dedicated field can be added later if real usage shows it needs to
differ.

**Affects**: `agent/synthesizer.py`

---

## 2026-08-25 — Result Merger uses the standard RRF constant k=60

**Context**: `Technical_Design_Document.docx` section 6 names Reciprocal
Rank Fusion as the merging method (`score += 1 / (k + rank)` per list an
item appears in) but doesn't specify the smoothing constant `k`, which
controls how much a low rank is discounted relative to a high one.

**Decision**: `agent/merger.py` uses `k=60`, the constant from the paper
that introduced RRF (Cormack, Clarke & Buettcher, 2009) and the value most
hybrid-search implementations default to — using the field's own
established default rather than inventing a project-specific number with
no particular justification.

**Alternatives considered**: None seriously — no project-specific reason
surfaced to deviate from the standard value, and picking an arbitrary
different constant would need its own justification this project has no
basis for.

**Affects**: `agent/merger.py`

---

## 2026-08-25 — Graph traversal expands outward from search hits and ranks by shared-connection count

**Context**: `Technical_Design_Document.docx` section 8.2 step 3 says
"connected nodes are pulled from Neo4j via graph traversal" but doesn't
specify a starting point. A natural-language question has no direct entry
point into the graph on its own — `Neo4jStore` is queried by item id, not
by text — so traversal has to start from somewhere. Neither doc specifies
how discovered neighbors should be ranked relative to each other, either,
since `storage/neo4j_store.py::get_related_items()` only returns one
item's unordered one-hop neighbors.

**Decision**: `agent/graph_traversal.py::graph_traversal()` takes the
seed hits already found by vector/keyword search and expands one hop out
from each of them via `get_related_items()`, excluding anything already in
the seed set. Neighbors are ranked by how many *distinct* seed items they
connect to — a neighbor reachable from several of the seeds is a stronger
signal than one reachable from only one — with ties broken by the best
(lowest) rank among the seeds that reached it. A seed whose lookup fails is
logged and skipped, matching the rest of the codebase's per-item
resilience pattern, rather than aborting traversal for every other seed.

**Alternatives considered**: Extracting probable entity names from the
question text itself (e.g. via NER) to give traversal an independent
starting point — rejected as significant added complexity and a new
failure surface for a personal tool where the search-hit-seeded approach
already covers "how does X relate to Y"-style questions well, since X and Y
are exactly what vector/keyword search should already be surfacing.

**Affects**: `agent/graph_traversal.py`

---

## 2026-08-25 — Keyword search node builds a safe FTS5 query with stopword removal and OR-joined terms

**Context**: `storage/sqlite_store.py::keyword_search()` was already
built and tested against simple, pre-formed FTS5 match expressions (e.g.
`"router"`), but never against a raw natural-language question. Passing a
question directly as the `MATCH` expression has two problems: FTS5's
special characters/operators (`" * ^ - + ( ) : NEAR AND OR NOT`) can turn
an ordinary sentence into invalid syntax or an unintended query, and FTS5's
default implicit join between space-separated bare terms is `AND` — too
strict for natural language, since requiring every single word (including
filler words like "what"/"the"/"is") to appear would return far fewer
results than a human would expect.

**Decision**: `agent/search_nodes.py::_to_fts_query()` extracts
alphanumeric words, drops a small stopword list (articles, auxiliary
verbs, question words, pronouns), double-quotes each remaining term (so
each is matched as a literal string rather than parsed for FTS5 operators
— sidesteps the special-character problem entirely), and joins them with
explicit `OR` — so a question matches items containing *any* significant
term, ranked by BM25 relevance rather than requiring all of them. A
question with no significant terms left (e.g. "What is this?") returns no
hits without ever calling `keyword_search()`, rather than running an
empty/degenerate query.

**Alternatives considered**: Passing the raw question straight through —
rejected, breaks on ordinary punctuation/apostrophes and is over-strict via
implicit `AND`. Using FTS5's built-in `NEAR`/proximity operators for a
closer approximation of natural phrasing — rejected as unnecessary
complexity; plain `OR` plus BM25 ranking already surfaces the most
relevant results without hand-tuning proximity windows.

**Affects**: `agent/search_nodes.py`

---

## 2026-08-25 — Query Router is a rule-based heuristic, not an LLM call

**Context**: `Technical_Design_Document.docx` section 8.2 says the Query
Router "decides which retrieval methods are actually needed" but doesn't
specify the mechanism. Two signals settle it in favor of a heuristic over
an LLM call: `providers/base.py`'s `Task` type (`"metadata" | "relationship"
| "answer"`) has no `"route"` entry, and `Component_Map.docx`'s `QueryRouter`
row lists only `VectorSearchNode`/`KeywordSearchNode`/`GraphTraversalNode`
under "Calls" — never `ProviderInterface`, unlike `AnswerSynthesizer`'s row
which explicitly does.

**Decision**: `agent/router.py::route()` is a pure function using simple
pattern matching, no LLM call:
- `keyword_search` is always `True` — cheap, deterministic, and "search is
  hybrid" is already a locked-in architectural default (`CLAUDE.md`), so
  there's no real cost to always including it.
- `vector_search` is skipped only for what looks like an exact lookup: a
  quoted phrase, or a "from/by \<someone\>" filter pattern — matching the
  doc's own example ("keyword search ... for a specific lookup like
  'emails from this recruiter'"). Every other question runs it.
- `graph_traversal` runs only when the question's phrasing implies the
  answer depends on how items relate to each other (e.g. "how does X relate
  to Y", "what led to X") — a fixed list of relationship-implying phrases,
  matching the doc's other example ("graph traversal for a broad thematic
  question" being about *connections*, not just content).

**Alternatives considered**: An LLM-based router (asking the model to
classify which nodes are needed) — rejected per the two signals above, and
because it would add LLM latency/cost to every single query for a decision
a handful of regexes can make deterministically and near-instantly.

**Affects**: `agent/router.py`

---

## 2026-08-25 — Browser history is excluded from relationship detection, enforced in `pipeline/relationships.py`

**Context**: A real end-to-end run of the daily batch (local files +
browser history) surfaced generic browsing-history titles like "Sign in to
your account" getting confirmed as `implements`/`discussed_in` relationships
against real project design docs — a clear false positive, and a good
illustration of why: a bare page title has almost no content for the
relationship LLM call to reason about. Re-reading `CLAUDE.md`'s already
locked-in decisions turned up the actual root cause: "Browser History is
intentionally the lightest-weight source (title + URL + metadata only, no
full page fetch, no relationship detection)" — `scheduler/daily_batch.py`
was calling `detect_relationships()` unconditionally for every processed
item regardless of source, which directly violates this. This was a real
correctness bug against an already-decided design, not a prompt-tuning
problem — the earlier relationship-confidence work (2026-08-24) was
treating a symptom of this same gap without knowing the gap existed.

**Decision**: Enforce the exclusion inside
`pipeline/relationships.py::detect_relationships()` itself (a
`_NO_RELATIONSHIP_DETECTION_SOURCE = "browser_history"` module constant),
not in `scheduler/daily_batch.py`'s orchestration loop, so the rule lives
in the one module that owns relationship-detection logic and protects any
current or future caller automatically:
1. `detect_relationships()` returns `[]` immediately, before any Chroma
   query or LLM call, if the source item's `source_type` is
   `browser_history` — it never *initiates* detection.
2. The Chroma candidate-narrowing `where` clause also excludes
   `source_type = browser_history`, so a browser history item is never
   surfaced as a *candidate* for another item's detection either — the
   source is excluded from the graph in both directions, matching
   `Database_Schema.docx` section 5's "not necessarily a graph node" model
   for items with no relationships.

**Verified against real data**: re-ran the exact real scenario that
produced the false positive (the same two "Sign in to your account" items,
same effective SQLite item ids via the upsert-on-`source_ref_id`
semantics) — zero relationship calls, zero edges written, confirmed both
through the daily batch's own output and a direct Neo4j query. (Note:
running the real test suite between the two verification runs also wiped
the shared local Neo4j instance, per the Neo4j test fixtures' documented
`MATCH (n) DETACH DELETE n` setup/teardown — expected test behavior, not
part of this fix, but it means the graph edges from earlier real-data demos
this session no longer exist in Neo4j, though the underlying SQLite/Chroma
data is untouched.)

**Affects**: `pipeline/relationships.py`, `tests/test_pipeline/test_relationships.py`

---

## 2026-08-24 — Browser history reads a copy of the live SQLite file, one row per URL

**Context**: Chrome (and other Chromium-based browsers) hold an exclusive
lock on their `History` SQLite file while running, so opening the
configured `BROWSER_HISTORY_PATH` directly would fail with "database is
locked" almost every time the daily batch runs — the browser is normally
open. Neither `docs/Data_Extraction_Specification.docx` nor
`Environment_Config_Reference.docx` mention this. Separately, Chrome's
schema has both a `urls` table (one row per URL, with `visit_count` and
`last_visit_time` reflecting only the *most recent* visit) and a `visits`
table (one row per individual visit event); the spec's "filtered to visits
after `last_run_timestamp`" wording doesn't say which granularity to use.

**Decision**: Copy the file to a temp directory before opening it
(`shutil.copy2` into `tempfile.TemporaryDirectory()`), and read from the
`urls` table — one `ExtractedItem` per URL, using `last_visit_time` as
`last_edited_at` (there's no natural "created" concept for a history entry,
so `created_at` is left `None`). This matches `source_type`/`source_ref_id`
upsert semantics elsewhere (`source_ref_id = url`) and keeps re-visits from
producing duplicate items.

**Alternatives considered**: Reading the `visits` table for one row per
visit event — rejected; `ExtractedItem`/`items` has no field for individual
visit timestamps beyond `last_edited_at`, and per-visit granularity doesn't
match "browser history is the lightest-weight source" (section 8.2) or the
one-row-per-URL shape everywhere else in the design.

**Affects**: `extractors/browser_history.py`

---

## 2026-08-24 — Domain blocklist matching is plain substring containment

**Context**: `config.yaml`'s `filters.browser_history.domain_blocklist`
mixes two entry shapes: bare domains (`"facebook.com"`) and
domain-plus-path prefixes (`"google.com/search"`) — meant to block Google's
search-results pages specifically without blocking all of Google (e.g.
Maps). Neither doc specifies the matching algorithm.

**Decision**: `entry in url` — plain substring containment against the full
URL string. Handles both entry shapes without needing to parse the URL:
`"facebook.com"` matches any URL containing that domain anywhere;
`"google.com/search"` only matches URLs whose path actually starts with
`/search`, correctly leaving `google.com/maps` unblocked.

**Alternatives considered**: Parsing the URL with `urllib.parse` and
matching hostname/path separately — rejected as unnecessary complexity for
a filter with at most a handful of configured entries; plain substring
matching is easy to reason about from the config value alone.

**Affects**: `extractors/browser_history.py`

---

## 2026-08-24 — Relationship confirmation is biased toward "unrelated" and filtered by confidence

**Context**: Two real ingestion runs (the project's own design docs, and
two real Notion recipe pages) showed the relationship-confirmation LLM call
over-confirms far too readily. The original prompt told the model to pick
`"discussed_in"` "when no more specific label applies" — a built-in fallback
that made `related: true` the path of least resistance. In the docs run,
65/78 `companion_to` edges traced back to every doc sharing a literal
"Companion to: X" boilerplate line, not real content overlap. In the recipe
run, two nearly-empty recipe template pages still got linked to unrelated
project design docs. Confidence scores were already being requested and
stored, but nothing ever used them — a `related: true` at confidence 0.1
was written to the graph exactly like one at 0.95.

**Decision**: Two changes, together:
1. Rewrote `_build_relationship_prompt()` to explicitly state that most
   vector-narrowed candidate pairs are NOT related, to require pointing at
   a specific concrete connection rather than shared topic/vocabulary, and
   to define each label's actual criteria (e.g. `companion_to` now requires
   the pair to be *explicitly* designated as companion documents, not just
   two documents from the same project) — removing the old fallback bias
   toward `discussed_in`.
2. Added `config.yaml`'s `retrieval.relationship_confidence_threshold`
   (default `0.6`). `pipeline/relationships.py::detect_relationships()`
   discards a confirmed judgment whose `confidence` is below this threshold
   before writing it to Neo4j — the same as if the model had said
   `related: false`. A judgment with no confidence value at all isn't
   filtered (nothing to compare), since the schema doesn't require it.

**Alternatives considered**: Fixing only the prompt — rejected, since a
better prompt still doesn't stop a single bad/overconfident judgment from
being written; the confidence threshold is a structural backstop
independent of prompt quality. Fixing only the threshold — rejected, since
the original prompt's built-in bias toward finding *some* label meant most
judgments would come back with moderate-to-high confidence regardless of
whether they were actually correct.

**Verified against real data — partial success, real limitation found**:
calling `provider.generate_relationship()` directly against the exact pair
that produced the original false-positive (the "New Recipe" page vs. the
`Product_Requirement_Document.docx` chunk containing "Companion document
to: ...") still returned `companion_to` at confidence `0.9` — confidently
wrong, so the confidence threshold doesn't catch it either. Isolating
further: the same call with the literal "Companion document to:" phrase
stripped from the candidate text correctly returned "not related". So the
root cause is `claude-3-haiku` doing surface lexical pattern-matching on the
word "Companion" rather than genuine judgment, and it does this with high
confidence — neither the reworded prompt nor a confidence floor overrides
that for this specific model. A genuinely related pair (two paraphrases of
"the storage layer uses SQLite/Chroma/Neo4j") was still correctly confirmed
as `implements`, and an unrelated pair with no shared boilerplate word was
correctly rejected — so the fix is a real, if partial, improvement: it
narrows the false-positive surface to cases with literal shared boilerplate
vocabulary, it doesn't eliminate that specific failure mode. Likely
model-capability-dependent (this was tested only against the cheap
`claude-3-haiku` used for low-cost demo runs, not the project's configured
default `claude-sonnet-4`) — worth re-testing with a stronger model before
concluding whether further prompt work is needed.

**Affects**: `providers/base.py`, `pipeline/relationships.py`,
`config/config.yaml`, `config/settings.py`

---

## 2026-08-24 — Notion extractor logs scan progress every 25 pages

**Context**: A full, unfiltered `extract_new_items()` run visits every page
the integration can see, one Notion API call per page's block tree.
Verifying the extractor against this workspace's real data showed this can
take a long time on a workspace with many/deeply nested pages, with no
output at all until the whole run finishes — indistinguishable from a hang
from the outside (this is exactly what made an earlier, unrelated bug look
like a stuck process before it was traced to a real cause).

**Decision**: Log an INFO-level progress line every 25 pages scanned
(`_PROGRESS_LOG_INTERVAL`), plus a start line and a final summary line
(pages scanned vs. items extracted). Each individual `search()` pagination
call also logs at DEBUG. 25 is an arbitrary but reasonable cadence — frequent
enough to show a long run is alive, infrequent enough not to spam logs on
an ordinary-sized workspace.

**Alternatives considered**: A time-based interval (e.g. log every 30s) —
rejected as more code for no real benefit here, since page count is already
a meaningful, monotonically increasing progress signal.

**Affects**: `extractors/notion.py`

---

## 2026-08-24 — Daily batch integration tests must pin `_EXTRACTORS` to `local_file`

**Context**: Adding `notion.extract_new_items` to `scheduler/daily_batch.py`'s
`_EXTRACTORS` registry broke `tests/test_scheduler/test_daily_batch.py`
silently: `TestFullRun` and `TestFailureHandling` call `daily_batch._run()`
directly against the real, module-level `_EXTRACTORS` list, without mocking
it (only the LLM provider is faked in that suite). Since this machine's
`config/.env` has a real `NOTION_API_KEY` configured, every one of those
"local files" integration tests started making real calls against the real
Notion workspace, running for over an hour before being caught.

**Decision**: Add an autouse `local_files_only` fixture to both
`TestFullRun` and `TestFailureHandling` that pins
`daily_batch._EXTRACTORS` to `[("local_file", local_files.extract_new_items)]`
for the duration of each test in those classes — matching what the classes'
own docstrings already claim to test. Any test that needs a different
extractor set (e.g. to simulate a source-level failure) still overrides
`_EXTRACTORS` explicitly afterward within the test body, which layers
correctly on top of the autouse fixture's shared `monkeypatch`.

**Alternatives considered**: Requiring `NOTION_API_KEY` to be unset while
running tests — rejected as fragile and easy to violate by accident (as
happened here); test isolation should not depend on what happens to be in a
developer's `.env`. General rule going forward: any test file exercising
`daily_batch._run()`/`main()` must explicitly control `_EXTRACTORS` rather
than rely on the real registry, so adding a new source extractor can never
silently make existing tests real-network-dependent again.

**Affects**: `tests/test_scheduler/test_daily_batch.py`

---

## 2026-08-24 — Notion extractor uses the official `notion-client` SDK

**Context**: `Tech_Stack.docx` and `Environment_Config_Reference.docx` specify
`NOTION_API_KEY` (already present as `EnvSettings.notion_api_key`) and the
extraction behavior (poll pages via the API, compare `last_edited_time`
against `last_run_timestamp`, convert blocks to plain text), but neither
document names a specific HTTP client library for the integration.

**Decision**: Use Notion's official `notion-client` Python SDK
(`Client(auth=...)`, with `client.search()` and
`client.blocks.children.list()`) rather than calling the REST API directly
with `requests`/`httpx`. It's maintained by Notion, handles pagination
cursors and auth headers, and returns plain dicts shaped exactly like the
REST API's JSON — so the extractor's block-parsing code reads the same
either way, with less boilerplate.

**Alternatives considered**: Raw HTTP calls via `httpx` — rejected as pure
extra maintenance (manual pagination, headers, versioning) with no benefit,
since no other part of the system depends on an HTTP client abstraction.

**Affects**: `extractors/notion.py`, `pyproject.toml`

---

## 2026-08-24 — Relationship labels are constrained to a fixed vocabulary

**Context**: A real end-to-end ingestion run against the repo's own 12
design documents (see below) produced 94 confirmed relationships, but with
16 distinct free-form labels — the original prompt only gave the model
examples ("implements", "discussed_in", "planned_in"), not a closed set.
Most of that spread was near-duplicate phrasings of the same underlying
relationship (`discussed_in` / `discusses` / `discusses_in`, `described_in`
/ `documented_in` / `is_described_in`), which fragments anything downstream
that wants to reason about or filter by relationship type
(`agent/graph_traversal.py`, eventual UI filtering). `Database_Schema.docx`
already implied a small canonical set by only ever naming three example
labels, never suggesting free-form generation was intended.

**Decision**: `providers/base.py` defines `_RELATIONSHIP_LABELS =
("implements", "discussed_in", "planned_in", "companion_to")` — the three
schema-doc examples plus `companion_to`, which came directly out of the
real test run (several of this project's own design docs are explicitly
"Companion to" one another, a genuinely distinct relationship from generic
`discussed_in`). `_build_relationship_prompt()` now presents this as a
closed choice rather than examples, instructing the model to fall back to
`discussed_in` when nothing more specific applies (matching how it already
dominated the free-form results — 58 of 94 edges — so it's the natural
default). `_parse_relationship_response()` rejects any label outside this
set, which triggers a retry via the existing "any exception is retryable"
mechanism rather than silently accepting an off-vocabulary label.

**Alternatives considered**:
- *Leave labels free-form* — rejected based on direct evidence from a real
  run: 16 labels for 94 edges is already too fragmented to be useful, and
  nothing bounds the label space from growing further as more sources are
  ingested.
- *A larger fixed vocabulary (6+ labels covering source-specific cases like
  `follows_up` for email threads)* — rejected for now as speculative ahead
  of those extractors actually existing; easy to extend
  `_RELATIONSHIP_LABELS` later once a real source produces relationships
  that don't fit the current four.

**Affects**: `providers/base.py`

---

## 2026-08-24 — Daily batch: metadata is batched per source, run status has three tiers, and a mid-item failure rolls back the item row

**Context**: `scheduler/daily_batch.py` is the first module to actually
compose every pipeline stage in sequence, which surfaced questions none of
the individual modules needed to answer on their own: how items flow into
`pipeline/metadata.py`'s batching, what `ingestion_runs.status` should be
when only some sources fail, and what happens to a partially-processed item
when a later pipeline stage fails.

**Decisions**:
- Each source's *entire* filtered item list is passed to
  `generate_metadata()` in one call, not item-by-item — calling it once per
  item would silently defeat `config.yaml`'s
  `ingestion.batch_metadata_group_size` grouping, making every configured
  batch size behave as if it were 1. `_run()` collects
  `filtered_items = [item for item in raw_items if apply_noise_filter(item)]`
  per source first, then batches.
- `status` has three tiers, not the two `success`/`failed` a first read of
  `Database_Schema.docx` might suggest: `"success"` (no errors at all),
  `"partial_failure"` (at least one item was processed despite some
  errors), `"failed"` (zero items processed). Only `"success"` advances the
  watermark (already `storage/sqlite_store.py`'s behavior); `partial_failure`
  vs `failed` distinguishes "mostly worked, retry the gaps" from "nothing
  usable came out of this run" in `ingestion_runs` for anyone debugging a
  bad run later.
- `_process_item()` writes the SQLite item row *before* chunking/embedding
  it (needed either way, since `embed_chunks()` requires the item's
  effective id for Chroma metadata). A test writing a real integration run
  caught that if chunking or embedding then failed, the item row was left
  behind with zero chunks — a silent partial write, undercounting
  `items_processed` while still polluting `items`. `_process_item()` now
  wraps chunking/embedding/`replace_chunks()` in a try/except that calls
  `delete_item()` before re-raising, so an item in SQLite always either has
  its chunks too, or doesn't exist at all.
- Both extractor-level failures (`ExtractorError`) and per-item failures
  (any other exception during `_process_item()`) are caught, logged, and
  appended to a shared `errors` list rather than aborting the run — matching
  the resilience pattern already established in extractors, metadata
  generation, and relationship detection.

**Affects**: `scheduler/daily_batch.py`

---

## 2026-08-24 — Relationship candidate narrowing uses a whole-document embedding, not the first chunk alone

**Context**: A real ingestion run (using the fixed relationship-label
vocabulary from PR #12, `providers/base.py`) showed `companion_to`
dominating far more than expected — 84 of 94 edges. Investigating why:
`detect_relationships()` used only the item's
*first* chunk to build the query embedding for Chroma's candidate-narrowing
search, and every one of these design documents' first chunk is
near-identical boilerplate (title, "Prepared for: Shivam Shinde", a
"Companion to: X" line). Two items sharing that boilerplate look highly
similar by cosine distance on chunk 0 alone, regardless of what the rest of
each document actually says — the narrowing step was effectively ranking on
metadata noise, not content.

**Decision**: The candidate-narrowing query vector is now the mean of
*every* chunk embedding the item already has in Chroma
(`storage/chroma_store.py::get_item_embeddings()` fetches them,
`pipeline/relationships.py::_mean_embedding()` averages them) — not a fresh
embedding of concatenated raw text, and not a re-embedding call at all.
Averaging already-computed per-chunk vectors is standard practice for
approximating whole-document semantics, and reuses vectors
`pipeline/embeddings.py::embed_chunks()` already wrote during ingestion, so
this adds no new embedding cost. Concatenating and re-embedding the full
text was rejected outright: `all-MiniLM-L6-v2` has a limited input length,
so a long document would just get silently truncated by the model itself —
the same "only the front of the document counts" problem this decision
exists to fix, one level down.

The text actually sent to the LLM for the yes/no relationship judgment
(`pipeline/relationships.py`'s `source_text`/`candidate_text`) is
unchanged — still the first chunk. That's a separate, smaller-scope
question (a single representative excerpt is *cheaper* per-candidate to
send the LLM, where whole-document context matters less once vector search
has already narrowed to genuinely similar items) and wasn't part of what
broke here — the failure was specifically in candidate *narrowing*, not
confirmation.

**Verified**: re-ran the real ingestion (12 docs, real OpenRouter calls)
after this change — label distribution split far more evenly across the
fixed vocabulary instead of `companion_to` sweeping nearly every edge just
because chunk 0 matched.

**Alternatives considered**:
- *Concatenate all chunk text and embed it as one string* — rejected; runs
  into the embedding model's input length limit for any sufficiently long
  document, reintroducing a version of the same bias.
- *Leave chunk 0 as the narrowing signal, only expand what's sent to the
  LLM* — rejected; the LLM only ever sees candidates the vector search
  already surfaced, so a biased candidate list can't be corrected
  downstream — genuinely related items with different opening boilerplate
  might never even become candidates.

**Affects**: `storage/chroma_store.py`, `pipeline/relationships.py`

---

## 2026-08-24 — Relationship detection reads SQLite for full item details, despite Component_Map omitting it

**Context**: `Component_Map.docx` lists `RelationshipDetector`'s
dependencies as `ChromaStore, ProviderInterface, Neo4jStore` — no
`SQLiteStore`. But `storage/neo4j_store.py::ItemNode` has `title`/`url`
fields, and Chroma's `VectorChunk` metadata (`item_id`, `source_type`,
`project_name`, `topic`, `created_at`) has neither — there's no source for
those fields other than SQLite's `items` table. This is the same kind of
gap as `pipeline/metadata.py`'s Component_Map read (2026-08-24 entry), but
resolved the opposite way: there, the missing capability (writing SQLite)
was left to the caller; here, the missing *data* has no other owner, so the
module has to go get it.

**Decision**: `detect_relationships()` takes an open SQLite `conn` and
calls `get_item()`/`get_chunks_for_item()` to build accurate `ItemNode`s
for both the source and every confirmed candidate, and to get the source
item's representative chunk text. Chroma's own metadata is still what
narrows and ranks candidates (via `query()`) — SQLite is only consulted
after a candidate has already survived that vector search, so this doesn't
turn relationship detection into a full-table scan.

The candidate search excludes the source item itself (`where: {"item_id":
{"$ne": source_item_id}}` — a chunk from the same item is by far the
closest match otherwise) and narrows to the same `project_name` when the
source item has one classified, matching the "candidate narrowing" language
already in `storage/chroma_store.py`'s docstring. Only the item's *first*
chunk (by `chunk_index`) is used as representative text for both the
narrowing-query embedding and the LLM confirmation prompt — matching
`pipeline/metadata.py`'s "one representative excerpt, not the whole
document" approach to keeping this a reasonably cheap, single call per
item rather than an all-chunks comparison.

A candidate whose provider call or Neo4j write fails
(`ProviderError`/`GraphStoreError`) is logged and skipped, continuing to
the next candidate — matching the resilience pattern used everywhere else
in the pipeline (extractors, metadata generation): one bad candidate
shouldn't cost the item every other relationship it might have.

**Alternatives considered**:
- *Only use Chroma/Neo4j, leave `title`/`url` unset* — rejected; a
  relationship-graph node with no title is far less useful for the eventual
  `agent/graph_traversal.py` and answer citations, for a field that's one
  cheap lookup away.
- *Compare all of an item's chunks, not just the first* — rejected as
  unnecessary cost for now; a representative excerpt already worked well
  enough for metadata classification, and nothing in the design docs asks
  for exhaustive chunk-level relationship comparison.

**Affects**: `pipeline/relationships.py`

---

## 2026-08-24 — `embed_chunks()` writes to Chroma directly and takes an open collection

**Context**: Unlike `pipeline/metadata.py` (kept storage-free — see the
2026-08-24 entry on that), `Component_Map.docx` lists `EmbeddingGenerator`'s
only dependency as `ChromaStore`, and there's no other layer that would
ever need a bare in-memory vector — the entire point of computing one here
is to persist it. Storage-free wasn't the natural design for this stage the
way it was for metadata.

**Decision**: `embed_chunks()` computes vectors and calls
`storage/chroma_store.py::upsert_chunks()` itself, returning SQLite-ready
`Chunk` objects (with a freshly generated `embedding_id` each) for the
caller to persist via `storage/sqlite_store.py::replace_chunks()`. It takes
an already-open Chroma `collection` as a parameter rather than calling
`get_collection()` itself, since that function opens a new client on every
call (by design — see the 2026-08-24 Chroma entry); the caller opens one
collection and reuses it across every item in an ingestion run, the same
way `storage/sqlite_store.py` functions all take an open `conn` rather than
connecting themselves. The `sentence-transformers` model is loaded once via
`functools.cache` keyed on the configured model name, since loading it is
expensive (seconds, even from local cache) relative to encoding.

**Affects**: `pipeline/embeddings.py`

---

## 2026-08-24 — Embedding tests run against the real model and a real embedded Chroma collection

**Context**: `sentence-transformers/all-MiniLM-L6-v2` is config.yaml's
locked-in embedding model (`Tech_Stack.docx`), not an implementation detail
this module is free to swap for something lighter in tests the way
`pipeline/chunking.py` avoided `tiktoken` — there's no reduced-dependency
stand-in for "compute a real embedding" the way there was for "approximate
a token count."

**Decision**: `tests/test_pipeline/test_embeddings.py` uses the real
configured model (already locally cached from `uv add
sentence-transformers` verification) and a real embedded Chroma collection
in a temp directory — no mocking. One test asserts an actual semantic
property (a query about "a kitten on a rug" ranks nearest to "the cat sat on
the mat," not a revenue sentence), which a mocked embedding call couldn't
meaningfully verify. `functools.cache` on model loading keeps the one-time
load cost (seconds) from repeating across the file's tests.

**Affects**: `tests/test_pipeline/test_embeddings.py`

---

## 2026-08-24 — Metadata generation is a pure batching function; a failed group degrades rather than aborts

**Context**: `Component_Map.docx` lists `MetadataGenerator`'s dependencies
as `ProviderInterface, SQLiteStore, EmbeddingGenerator`, which could be read
as this module writing directly to SQLite and chaining into embedding
generation. `Database_Schema.docx` stores `project_name`/`topic` at the
*item* level, and `config.yaml`'s `ingestion.batch_metadata_group_size`
("items grouped per metadata-extraction LLM call") confirms metadata is
generated per item, not per chunk — the FLOW.md text written when the
provider layer was built ("Each chunk →
`get_provider("metadata").generate_metadata()`") was imprecise and is
corrected alongside this change.

**Decision**: `pipeline/metadata.py::generate_metadata(items)` stays a pure
function — `list[ExtractedItem]` in, `list[ItemMetadata]` out, grouped into
`ingestion.batch_metadata_group_size`-sized LLM calls — with no SQLite or
Chroma calls of its own. The item write to SQLite (combining
`ExtractedItem`'s fields with this function's `ItemMetadata` result) is left
to the caller (the eventual `scheduler/daily_batch.py`), matching how
`pipeline/filters.py` and `pipeline/chunking.py` were already built: small,
storage-free functions the orchestrator composes, easy to unit test without
a real or fake database. Component_Map's dependency table is read as the
architectural boundary this module is *permitted* to cross (it may
legitimately end up influencing what gets written to SQLite), not a literal
same-function call requirement — `Component_Map.docx`'s own section 3 states
its rules in terms of what layers may depend on which, not literal call
graphs.

A group's LLM call failing after retries (`ProviderError`) degrades that
group to `ItemMetadata(project_name=None, topic=None)` rather than
propagating and losing every other group's results — both fields are
optional everywhere they're stored (`items.project_name`/`items.topic` are
nullable columns), so an unclassified item is a normal, storage-representable
outcome, not a failure state worth aborting an entire ingestion run over.

**Alternatives considered**:
- *Have `generate_metadata()` write to SQLite/call embeddings itself* —
  rejected; it would require this module to accept a live connection just
  to test its batching logic, and couples metadata generation to storage
  and embedding lifecycles it doesn't otherwise need to know about.
- *Let a failed group's `ProviderError` propagate and abort the whole
  batch* — rejected; one bad LLM call shouldn't cost every other item in
  the run its classification, especially since it's an optional field.

**Affects**: `pipeline/metadata.py`, `FLOW.md`

---

## 2026-08-24 — `pipeline/filters.py` only does cross-source filtering; per-source noise rules stay in their extractors

**Context**: `config.yaml`'s `filters` section already nests
`browser_history` (`min_visit_count`, `domain_blocklist`) and `gmail`
(`excluded_labels`) — but `ExtractedItem`, the common contract every
extractor normalizes into (`docs/Data_Extraction_Specification.docx`
section 2), has no `visit_count` or `labels` field. A generic
`pipeline/filters.py` operating only on `ExtractedItem` therefore
structurally cannot apply those two source-specific rules — it never sees
the raw data they need.

**Decision**: `pipeline/filters.py::apply_noise_filter()` implements only a
new, source-agnostic rule — dropping items whose extracted text is shorter
than a configurable minimum (`filters.min_content_length`, added to
`config.yaml`/`config/settings.py`'s `FiltersConfig` since no such
cross-source threshold existed yet). Browser history's domain
blocklist/visit-count threshold and Gmail's excluded-labels rule will be
applied inside `extractors/browser_history.py` and `extractors/gmail.py`
themselves, before those items are ever normalized into `ExtractedItem` —
each extractor is the only place that still has access to the source-native
fields (a URL's domain, a message's labels) those rules need.

**Alternatives considered**:
- *Add `visit_count`/`labels` fields to `ExtractedItem` so
  `pipeline/filters.py` could apply every rule* — rejected; it would grow
  the shared contract with fields five of six sources never populate, for
  filtering logic that's inherently source-specific anyway.
- *Skip adding `min_content_length` and leave `pipeline/filters.py` as a
  no-op until a real cross-source rule was needed* — rejected; every source
  can produce near-empty extracted text (a mostly-image PDF, a one-line
  email), so a minimum-length floor is a genuine cross-source rule worth
  having now rather than a hypothetical one.

**Affects**: `pipeline/filters.py`, `config/settings.py`, `config/config.yaml`

---

## 2026-08-24 — Chunking splits at paragraph boundaries with a word-count token approximation

**Context**: `config.yaml`'s `chunking.target_chunk_size_tokens` /
`chunk_overlap_tokens` are specified in tokens, and
`Data_Extraction_Specification.docx` section 4.3 says long content should
split "at natural boundaries... rather than arbitrary character counts."
Neither document says which tokenizer to count against, and the two real
candidates — `tiktoken` (already a transitive dependency via
`langchain-openai`) and the actual embedding model's own tokenizer
(`sentence-transformers/all-MiniLM-L6-v2`) — disagree with each other
anyway, so neither is uniquely "correct" here.

**Decision**: `chunk_text()` greedily packs paragraphs (split on blank
lines) into a chunk until the next one would exceed
`target_chunk_size_tokens`; a single paragraph already over the target on
its own (no natural boundary inside it) falls back to a fixed sliding
window using `chunk_overlap_tokens` of overlap between windows. Token count
is approximated as whitespace word count (`len(text.split())`) rather than
a real tokenizer. This keeps chunking fully local with zero network
dependency — `tiktoken.get_encoding()` downloads its vocabulary file on
first use, which would silently break chunking on a machine running
`provider_mode: fully_local` with no internet access, undermining the
"fully local, privacy-first" design goal for a sizing knob that was never
going to exactly match the embedding model's own tokenizer regardless.

**Alternatives considered**:
- *Use `tiktoken`* — rejected for the offline-dependency reason above; it's
  also arguably no more "correct" than a word count, since it isn't
  `all-MiniLM-L6-v2`'s own tokenizer either.
- *Load `all-MiniLM-L6-v2`'s actual tokenizer for exact counts* — rejected
  as premature; it would import `pipeline/embeddings.py`'s model-loading
  machinery into chunking for a soft sizing target, not a hard limit
  anything downstream enforces.
- *No overlap at natural paragraph boundaries, only in the sliding-window
  fallback* — adopted as part of this decision: overlap exists to avoid
  losing context across an *arbitrary* cut point, which paragraph breaks
  aren't, so paragraph-packed chunks don't duplicate content between them.

**Affects**: `pipeline/chunking.py`

---

## 2026-08-24 — Extractor error handling: skip-and-log per item, never raise mid-scan

**Context**: `Coding_Conventions.docx` section 2.4 says extractors "catch
and log their own errors so one source's failure does not stop the daily
batch for the remaining five sources," but doesn't distinguish a
source-level failure (the whole source is unreachable) from an item-level
one (a single file is corrupted). Local Files is entirely item-level: there
is no "the source is down," only individual files that may fail to open,
stat, or parse.

**Decision**: `extract_new_items()` never raises for a per-file problem — an
unreadable stat, a corrupted PDF, an unsupported extension — it logs a
warning and skips that file, continuing the scan. `ExtractorError` (in the
new shared `extractors/base.py`) is reserved for a genuine source-level
failure; Local Files never raises it, since a missing watch directory
(plausible if the user hasn't created it yet) is also just logged and
skipped rather than treated as fatal — the remaining watch directories, if
any, still get scanned. A future extractor with an actual "is the source
reachable at all" question (Notion's API being down, Gmail auth expiring)
is where `ExtractorError` earns its use.

**Alternatives considered**:
- *Raise `ExtractorError` on any file-level failure* — rejected; one
  unparseable PDF among thousands of files shouldn't abort ingestion of
  every other file in the same run.
- *Silently skip missing watch directories with no log line* — rejected; a
  misconfigured or not-yet-created `LOCAL_FILES_WATCH_DIRS` entry should be
  visible somewhere, even if it isn't fatal.

**Affects**: `extractors/base.py`, `extractors/local_files.py`

---

## 2026-08-24 — Local file `created_at` uses `st_ctime` with no cross-platform correction

**Context**: `Data_Extraction_Specification.docx` section 2's common
contract wants a `created_at` timestamp per item, but true file-creation
time isn't portably available from Python's stdlib: `st_ctime` is creation
time on Windows, but metadata-change time on Linux/Mac (getting real
creation time there needs a platform-specific extension, e.g. reading
`st_birthtime` where the OS exposes it).

**Decision**: Use `st_ctime` as a best-effort `created_at` on every
platform, with no correction. This is a single-user local tool, and
`created_at` is never used for anything beyond display — no filtering,
sorting-critical logic, or dedup depends on it being exactly right — so the
Linux/Mac inaccuracy (reporting the last metadata change instead of true
creation) isn't worth a platform-specific dependency to fix.

**Affects**: `extractors/local_files.py`

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
