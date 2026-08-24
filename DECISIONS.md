# Implementation Decisions

A running log of meaningful decisions made **during coding** that were not
already settled in `/docs`. Newest entries at the top.

Decisions already locked in by the design document set are not repeated here —
see `CLAUDE.md` and the documents in `docs/` for those.

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
