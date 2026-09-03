# Implementation Decisions

A running log of meaningful decisions made **during coding** that were not
already settled in `/docs`. Newest entries at the top.

Decisions already locked in by the design document set are not repeated here —
see `CLAUDE.md` and the documents in `docs/` for those.

---

## 2026-09-03 — Gmail/Calendar extraction silently never ran for guided-OAuth-only setups

**Context**: Found live, running a real ingestion test — Gmail and Calendar both reported `status: "ok"` with `0` items in `GET /api/sources/status`, even though `GET /api/sources/connections` correctly showed both as connected, and a real calendar event existed well inside the configured date range. Checking the actual backend logs during a fresh trigger showed the real symptom: `ERROR: gmail extraction failed: GMAIL_CREDENTIALS_PATH is not configured` — an outright failure, not an empty result, that the source-status endpoint's summary view didn't surface clearly enough to distinguish from "ran fine, found nothing."

**Root cause**: `extractors/gmail.py`'s and `extractors/calendar.py`'s own `extract_new_items()` both had an early, unconditional gate — `if settings.env.gmail_credentials_path is None: raise ExtractorError(...)` (and the Calendar equivalent) — that ran *before* `_get_credentials()` was ever called. `_get_credentials()` itself already correctly tries the guided setup wizard's shared Google OAuth token first, falling back to the older per-service credentials file only if that's unavailable (per its own docstring and `agent/connection_check.py`'s matching logic) — but this early gate short-circuited extraction entirely whenever the older `GMAIL_CREDENTIALS_PATH`/`GOOGLE_CALENDAR_CREDENTIALS_PATH` env var wasn't set, regardless of whether the guided OAuth flow was connected. Since the guided wizard (issue #92) never sets either of these older env vars — it only sets `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET` and the shared token file — **every guided-OAuth-only setup had Gmail/Calendar extraction fail outright**, silently, while its own connection check reported everything fine. This bug predates this session (introduced whenever `_get_credentials()`'s shared-token-first logic was added without removing the now-redundant, now-wrong gate ahead of it) and was only caught by actually running a real ingestion against a real guided-OAuth connection — the connection-check-based tests already in place couldn't catch it, since they test `agent/connection_check.py`'s logic directly, not `extract_new_items()`'s own separate gate.

**Fix**: removed the early gate from both `extract_new_items()` functions entirely — `_get_credentials()` already raises a clear, correct `ExtractorError` when truly nothing is configured (neither the shared token nor the older per-service file), so the gate added nothing except a wrong extra failure mode. Verified directly against a real guided-OAuth connection: Calendar now correctly extracts real events (including one previously silently missed on a real date inside the configured range), and Gmail now correctly extracts real messages instead of failing immediately.

**Affects**: `extractors/gmail.py`, `extractors/calendar.py`, `tests/test_extractors/test_gmail.py`, `tests/test_extractors/test_calendar.py`.

---

## 2026-09-02 — Two bugs found running the real Docker stack end-to-end (not just the manual dev backend)

**Context**: The wizard/picker work up to this point had only been verified against a manually-run `uv run python -m api.main` backend, never a real `docker compose up` stack. Running the actual stack surfaced two real bugs neither unit tests nor the manual-backend testing could have caught.

**Bug 1 — Neo4j auth failure on first boot**: `config/.env`'s own `NEO4J_PASSWORD` (what the backend app uses to *connect* to Neo4j, via `env.neo4j_password`) and the root `.env`'s `NEO4J_PASSWORD` (what initializes the Neo4j *server* itself, via `NEO4J_AUTH`) are two separate files with the same variable name — nothing kept them in sync. A `config/.env` left over from earlier manual-dev testing had a stale password that didn't match what the Neo4j container was actually initialized with, so the backend failed to start with `Unauthorized`. **Fix**: `docker-compose.yml` now overrides `NEO4J_USER`/`NEO4J_PASSWORD` in the backend/scheduler containers' `environment:` block from the exact same `${NEO4J_PASSWORD:-pkg-agent-local}` expression the `neo4j` service itself uses — the same treatment `NEO4J_URI` already got, for the same reason (anything that must match container-to-container can't be left to `config/.env`, which is oriented around user-facing, wizard-writable settings).

**Bug 2 — Browser-history radio button never showed as selected**: `agent/mounted_files.py::list_watched_directories()` (local files) returns full, ready-to-use container paths (`/data/watched/project-a`), but `list_host_data_files()` (browser history) returns paths *relative* to `/host-data` (`"History"`) — an inconsistency between the two functions' contracts that `SetupWizard.jsx` didn't account for. It compared the saved full path against the bare filename for the radio's `checked` state (always false) and saved the bare filename itself (an unusable, incomplete path) on click — found live by clicking it and seeing nothing visibly select. **Fix**: the wizard now reconstructs `/host-data/${file}` itself before using it for both the `checked` comparison and the saved value.

**Also fixed**: `docker/storage/` (the new `HOST_STORAGE_DIR` default) drops files beyond just `pkg_agent.db`/`chroma/` (e.g. `backend_port.txt`) that weren't covered by the existing `*.db`/`chroma/` `.gitignore` patterns — added a `docker/storage/*` / `!docker/storage/.gitkeep` pair so the whole directory's generated contents are covered generically rather than relying on that pattern list staying exhaustive.

**Affects**: `docker-compose.yml`, `frontend/src/components/SetupWizard.jsx`, `.gitignore`.

---

## 2026-09-02 — Google OAuth needed `OAUTHLIB_INSECURE_TRANSPORT=1` for the guided flow to work at all

**Context**: Found by actually walking through the wizard's Google step live — `POST /setup/google/oauth/callback`'s token exchange failed every time with `Google authorization failed: (insecure_transport) OAuth 2 MUST utilize https.` `oauthlib` (underneath `google-auth-oauthlib`'s `Flow`) refuses to exchange a code against any redirect/callback URL that isn't `https://`, unless `OAUTHLIB_INSECURE_TRANSPORT` is set — checked directly in `oauthlib.oauth2.rfc6749.utils.is_secure_transport()`, which has no built-in exception for `localhost`/`127.0.0.1` despite that being oauthlib's own most common real-world case. This app's callback (`GET /api/setup/google/oauth/callback`) is *always* plain `http://` — a manual dev setup's `127.0.0.1:PORT`, or whatever host:port Docker publishes — since nothing in this system ever terminates TLS anywhere.

**Decision**: `extractors/google_oauth.py` sets `os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")` at import time. Safe specifically because of this app's own already-locked-in posture (`CLAUDE.md`: "single-user, local-only... no authentication layer, by design") — the callback never crosses a network boundary this system doesn't already trust by that same design, and this is the standard, widely-documented workaround Google's own client library issues recommend for exactly this local-development scenario. Not something that would be appropriate for a multi-tenant or publicly-deployed service, which this deliberately isn't.

**Affects**: `extractors/google_oauth.py`.

---

## 2026-09-02 — SQLite/Chroma storage location under Docker: a deploy-time `.env` var, not an in-app field

**Context**: Asked directly — since this project ships as Docker-only, could the wizard let a user pick where SQLite/Chroma's data lives? Before this, `docker-compose.yml` stored both under a Docker-managed named volume (`app_data:/app/data`), invisible on the host filesystem and not pointed anywhere the user chooses.

**Decision**: Not an in-app field at all — unlike local files' own picker (`available_watch_directories`, see the "Local files gets a real picker under Docker after all" entry below), there's nothing to *narrow* here at runtime; storage location is a single decision made once, before the containers ever start, so it belongs in the same place `NEO4J_PASSWORD`/`HOST_WATCH_DIR` already live: the root `.env` `docker compose` itself reads. Added `HOST_STORAGE_DIR` there, bind-mounted to `/app/data` in both `backend` and `scheduler`, replacing the `app_data` named volume — defaults to a git-tracked `./docker/storage` folder (empty but for a `.gitkeep`; its real contents already covered by the existing `data`/`*.db`/`chroma/` `.gitignore` patterns) so the out-of-the-box behavior needs no extra configuration, while making the data genuinely inspectable on the host instead of hidden inside a Docker volume. Changing it later does not migrate existing data — same "you move the files, we don't" convention already used for `LOCAL_FILES_WATCH_DIRS`/`BROWSER_HISTORY_PATH`. The wizard's final step adds a one-line informational note pointing at this (Docker mode only), not an editable field.

**Alternatives considered**: An in-app "browse for a folder" step, matching local files' — rejected, since (unlike local files, which narrows an already-mounted tree) there is no already-mounted candidate set to choose from; the running container fundamentally cannot mount a *new* arbitrary host path at runtime, the same root constraint local files hit before its own picker was added, except here there's no "subset of what's already mounted" available to fall back to either.

**Affects**: `docker-compose.yml`, `.env.docker.example`, `docker/storage/.gitkeep` (new), `README.md`'s Docker section, `frontend/src/components/SetupWizard.jsx`'s Done step.

---

## 2026-09-02 — Neo4j gets a default password instead of requiring one before first launch

**Context**: Asked directly — should `NEO4J_PASSWORD` keep hard-failing (`docker-compose.yml`'s `${NEO4J_PASSWORD:?Set NEO4J_PASSWORD in .env}`) until the user sets it, or default to something so `docker compose up -d --build` genuinely needs zero `.env` editing?

**Decision**: Defaults to a fixed value (`pkg-agent-local`) rather than requiring one. A known default sitting in a public repo is a real (if small) exposure — `docker-compose.yml`'s `ports:` mapping for Neo4j publishes to every host interface by default, not just `127.0.0.1`, so a machine on a shared/untrusted network is genuinely reachable — but this app already carries that same exposure for everything else in the stack (the backend API, the frontend, and every other port here are equally unauthenticated and equally published), per its own locked-in "no authentication layer, single-user, local-only" design. Requiring a password specifically for Neo4j while every other port stays wide open wouldn't add real defense, only friction — asked the user directly rather than deciding this alone, given it's a genuine (if small) security trade-off, not a pure implementation detail. Documented in both `docker-compose.yml` and `.env.docker.example`: change it (or bind ports to `127.0.0.1`) if this machine is ever on a shared network.

**Alternatives considered**: Auto-generating a random password on first run — rejected for now as more moving parts than the trade-off justifies (`docker compose` itself can't generate and persist a value into its own `.env` before the containers start; it would need a separate wrapper script this project doesn't otherwise have a place for).

**Affects**: `docker-compose.yml`, `.env.docker.example`.

---

## 2026-09-02 — Guided setup wizard (issue #92, phase 4) — launched from a button, not shown automatically

**Context**: The last remaining piece of issue #92 — a multi-step UI (`frontend/src/components/SetupWizard.jsx`) tying together provider mode, Google (Gmail + Calendar) OAuth, Notion/GitHub/OpenRouter credentials, local files, and browser history into one linear flow. All the backend endpoints it needed already existed from phases 1-3 (`POST /setup/google/oauth/*`, `POST /setup/validate`, `POST /setup/credentials`, `GET /setup/host-data-files`) plus the existing `/settings`/`/settings/sources` endpoints — this phase is pure frontend, no new backend surface.

**Decision**: The wizard is launched from a "Guided setup" button in `SettingsPanel.jsx`'s header, not shown automatically on first run. There is no "is this a first run" signal anywhere on the backend (no `setup_complete` flag, nothing) to key an auto-popup off of, and inventing one just for this would be new backend surface for a fairly small UX win — an unprompted modal covering the whole screen on every load of an *already-configured* install (the far more common case after day one) would be a worse default than a clearly visible, discoverable button. If a real "first run" concept is ever added for another reason, revisit auto-showing the wizard then.

Every step past "provider" (Google/Notion/GitHub/OpenRouter/local files/browser history) is individually skippable with no confirmation — no data source is required by design (see `CLAUDE.md`), and the wizard doubles as a "review my setup" flow on revisit (it pre-fills every step from whatever's already saved/connected), not just a one-time onboarding gate.

**Provider-mode switch inside the wizard**: reuses the same "switching wipes all data" warning and `onResetAll()` call `SettingsPanel.jsx` uses, but with a single confirm rather than Settings' own two-consecutive-confirms "critical" treatment. Settings' extra friction exists because that switch is usually happening against a system with real, already-ingested data at stake; the wizard's typical context is a still-empty install where a lighter single confirm is enough — the same underlying safeguard (nothing switches without an explicit yes, and a decline reverts the pick) without stacking two dialogs for a lower-stakes moment. Only triggers `onResetAll()` at all if the picked mode actually differs from what's currently saved — picking the mode already active is a silent no-op, same as everywhere else in Settings.

**Google OAuth polling**: identical pattern to the wizard's own dependency, `agent/google_oauth.py`'s popup-based flow — `postGoogleOAuthStart()` opens a real popup on Google's consent screen, and the wizard's own tab polls `GET /setup/google/oauth/status` every 2s (capped at 60 attempts, ~2 minutes) since the actual token exchange lands in the popup's callback navigation, not a `fetch()` response this tab ever sees directly. Also stops polling early if the popup was closed without connecting, rather than spinning silently to the full timeout.

**Affects**: `frontend/src/components/SetupWizard.jsx` (new), `frontend/src/components/SettingsPanel.jsx` (new `onOpenWizard` prop + button), `frontend/src/App.jsx` (owns `showWizard` state, passes the same `onTriggerIngestion`/`onResetAll` handlers it already gives `SettingsPanel`), `frontend/src/api/client.js` (new thin wrappers: `postGoogleOAuthStart`, `getGoogleOAuthStatus`, `postSetupValidate`, `postSetupCredentials`, `getSetupHostDataFiles` — all calling endpoints `api/routes/setup.py` already exposed), `frontend/src/index.css` (`.wizard-*` rules, reusing `.confirm-dialog-*`/`.radio-option`/`.watch-dir-picker` styling rather than duplicating it).

---

## 2026-09-02 — `config/.env` had to become a real bind mount — every runtime settings write was silently no-op under Docker

**Context**: Found while manually verifying the local-files picker (see the entry directly below) against a real `docker compose up` stack — not by reasoning about the code, by actually testing it. `docker-compose.yml` used `env_file: - config/.env` to get the host's credentials into the `backend`/`scheduler` containers. Docker's `env_file:` injects **every key** in that file as a real OS environment variable, frozen at container-start. `EnvSettings` (`pydantic-settings`) always prefers a real environment variable over the same key's value in its own `env_file=` dotenv file. Put together: **any** value that existed in `config/.env` when the container started could never be changed at runtime by this app's own code again — not just `LOCAL_FILES_WATCH_DIRS`, but every single field this session's guided-setup-wizard work (issues #52/#92, all three phases) added a write path for: Notion/GitHub/OpenRouter credentials (`update_credentials_config()`), the Google OAuth Client ID/Secret (`update_google_oauth_config()`), and `browser_history_path`/`local_files_watch_dirs` (`update_source_config()`). Every one of those writes would succeed (write to `/app/config/.env` inside the container, an ephemeral, container-local file distinct from the host's real one, since `env_file:` never creates a live link back to the host file) and then be completely invisible to the running process, forever, for the container's lifetime — confirmed directly: a `PUT`, followed immediately by a `GET` in the same request cycle, returned the pre-write value.

This had gone undetected through phases 1-3 because none of that work had been verified against a real, running Docker container end-to-end yet — each phase's own PR said so explicitly ("not yet manually verified against a real Google Cloud OAuth client," etc.). The bug was real from the moment `docker-compose.yml` was first written (issue #52), just never exercised.

**Decision**: replaced `env_file: - config/.env` with a real bind mount. Tried in two steps, the first of which turned out to be insufficient on its own:
1. `./config/.env:/app/config/.env` (a single-file mount) fixed the frozen-env-var problem, but broke the write itself with `OSError: [Errno 16] Device or resource busy` — confirmed via `python-dotenv`'s own `rewrite()` source: `set_key()` writes a temp file in the *same directory* as the target, then does an atomic `os.replace()` onto it, which requires source and destination to be on the same mount point. A single-file bind mount puts the target on a different mount than its own parent directory (the container's own image layer), so the rename across mounts fails.
2. `./config:/app/config` (mounting the whole directory) fixes this — the temp file and the target both live on the same mount now, so the rename succeeds. `config.yaml`/`settings.py`/`__init__.py` (also files in this directory) get shadowed by the host's copies this way too, which is harmless since they're the exact files the image was just built from moments earlier. `config/client_secret_*.json` (if present, from the older manual Gmail/Calendar OAuth path) becomes reachable inside the container as a side effect — unlike baking a secret into a *distributed* image layer (which the `.dockerignore` fix elsewhere in this file specifically guards against, since that leaks to anyone who ever receives that image), a bind mount only exposes the file to this same local machine's own container process — not a new exposure to any other party.

**A second, unrelated bug found while re-verifying the fix, also only by testing**: fixing the backend confirmed the write now round-trips correctly through the API — but `frontend/src/components/SettingsPanel.jsx`'s Local Files section was still reading `sources.running_in_docker`/`sources.available_watch_directories`. `sources` state holds `getSourcesStatus()`'s result (the last ingestion run's outcome), not `getSourceConfig()`'s — the near-identical function names made this an easy mistake, and with no frontend test suite, an easy one to ship unnoticed (in issue #52's own merged work, before this session). Both fields were therefore always `undefined`, meaning the Docker-mode branch of that section could never actually render in the real app regardless of the backend being correct. Fixed by adding a dedicated `sourceConfig` state (populated once from `getSourceConfig()`, previously discarded right after seeding the individual text-field states) and repointing every Docker-mode read at it.

**A mistake made while testing this, worth recording plainly**: verifying the credentials save endpoint against the real stack used this machine's own real `config/.env`, and a test `POST /api/setup/credentials` call overwrote the real `GITHUB_TOKEN` with a placeholder value before a backup had been taken. Recovered by restoring the real value from this session's own earlier terminal output (a `docker compose config` run, several turns before, which had echoed it in cleartext) — the user was told directly and asked to verify the token still works. A `config/.env` backup is taken before any further live-credential testing in this session as of this entry.

**Verified**: rebuilt and ran the real stack (`docker compose up -d --build`) with a real host folder mounted at `HOST_WATCH_DIR`, containing real subdirectories. `GET /api/settings/sources` correctly listed both as `available_watch_directories`; `PUT` narrowing to one of them returned the *new* value immediately (not the stale one), and the write was confirmed present in the real host `config/.env` file afterward — surviving even a full container recreation. A path outside the mount (`/etc/passwd`) was still correctly rejected with `422`. `POST /api/setup/credentials` was also confirmed to actually persist to the real host file post-fix (previously would have silently no-op'd). Full backend test suite still passes; `black`/`ruff`/`npm run lint`/`npm run build` all clean.

**Affects**: `docker-compose.yml` (both `backend` and `scheduler` services). No application code changes beyond what's already described in the entry below.

---

## 2026-09-02 — Local files gets a real picker under Docker after all — reversing part of phase 3

**Context**: Directly asked, after phase 3 shipped, whether local files should really stay read-only-only under Docker, or whether a real picker was worth building despite the earlier "keep it consistent with #52's shipped decision" call. On reflection the earlier reasoning conflated two different things: #52's real constraint is "the running app can't mount a *new* host folder into itself at runtime" (only Docker Compose can, at container-start) — but the code shipped in phase 3 blocked *any* change to `local_files_watch_dirs` under Docker, including narrowing to a subset of what's *already* mounted, which doesn't violate that constraint at all. `LOCAL_FILES_WATCH_DIRS` already supports a comma-separated list, so nothing stops it from naming two specific subfolders under `/data/watched` instead of the whole mounted tree.

**A real, concrete blocker surfaced while building this, not just a design question**: `docker-compose.yml` hardcoded `LOCAL_FILES_WATCH_DIRS: /data/watched` directly in the `backend`/`scheduler` services' `environment:` blocks — verified directly that a real process environment variable set this way *always* wins over anything written to the mounted `config/.env` file (the same precedence this project already relies on for `RUNNING_IN_DOCKER`/`NEO4J_URI`/etc.). That means a picker writing a narrowed value to `config/.env` would have been silently ignored at runtime — the container would keep watching the whole mount regardless of what was picked. This is the concrete technical reason the original "fixed, read-only" restriction wasn't just an arbitrary consistency choice.

**Decision**:
1. Removed `LOCAL_FILES_WATCH_DIRS: /data/watched` from `docker-compose.yml`'s `environment:` blocks entirely — this field is deliberately the one exception that stays writable through the normal `config/.env` path, unlike the others there.
2. `config/settings.py::EnvSettings.watch_dirs` now defaults to `["/data/watched"]` (the whole mount) when unset **and** `running_in_docker` — preserving the out-of-the-box behavior `docker-compose.yml`'s hardcoded value used to guarantee, now via application logic instead of an unremovable environment override.
3. New `agent/mounted_files.py::list_watched_directories()` — lists immediate subdirectories under the fixed `/data/watched` mount (one level deep, matching how `local_files_watch_dirs` entries are used — each is a directory `extractors/local_files.py` recurses within). New `is_within_watched_root(path)` — a pure, filesystem-free path-component check (using `posixpath.normpath()` to resolve `.`/`..` segments *before* comparing, not just `PurePosixPath.parents` alone — verified directly that skipping normalization first lets a traversal like `/data/watched/../etc` lexically appear to have `/data/watched` as a "parent" even though it names a path entirely outside the mount; a real bug caught by its own test, not just a defensive assumption).
4. `PUT /api/settings/sources`'s Docker-mode guard changed from "reject any `local_files_watch_dirs` change" to "reject only entries that fail `is_within_watched_root()`" — a subset of the mount is now genuinely pickable; escaping it is still impossible.
5. `GET`/`PUT /api/settings/sources` both gained `available_watch_directories` (empty outside Docker) so the frontend can render a real checkbox picker instead of a plain list.
6. **A second, independent bug found while wiring the frontend**: `SettingsPanel.jsx`'s Local Files section had been reading `sources.running_in_docker`/`sources.available_watch_directories` — but the `sources` state variable holds `getSourcesStatus()`'s result (the last ingestion run's outcome), not `getSourceConfig()`'s. Both fields were therefore always `undefined` there, meaning the entire Docker-mode branch (shipped as part of #52, weeks — well, hours — before this entry) could never actually trigger in the real running app; the near-identical names (`getSourceConfig()` vs. `getSourcesStatus()`) made this an easy mistake to make and an easy one to miss with no frontend test suite. Fixed by adding a dedicated `sourceConfig` state (populated once from `getSourceConfig()`'s own result, previously discarded after seeding the individual text-field states) and pointing every Docker-mode read at it instead.

**Alternatives considered**: keeping the phase-3 restriction as shipped — reconsidered directly per user judgment call, favoring real usability (avoiding "ingest everything under a broadly-mounted folder, all or nothing") over consistency with a decision that, on inspection, was stricter than the actual constraint required.

**Verified**: `uv run pytest` — new tests for `EnvSettings.watch_dirs`'s Docker default, `agent/mounted_files.py::list_watched_directories()`/`is_within_watched_root()` (including the path-traversal regression case described above), updated `api/routes/settings.py` tests (a within-mount path now succeeds, an outside-mount path still correctly rejected, `available_watch_directories` populated/empty as expected). Full suite passes; `black`/`ruff` clean. `npm run lint`/`npm run build` clean. Verified end-to-end against a real `docker compose up` stack with an actual mounted host folder containing subdirectories — this is what surfaced the `config/.env` bind-mount bug described in the entry directly above (written second, but describing something discovered *during* this verification, so it reads first).

**Affects**: `docker-compose.yml`, `config/settings.py`, `agent/mounted_files.py`, `api/routes/settings.py`, `api/schemas.py`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`, and the corresponding test files.

---

## 2026-09-02 — Browser history gets an update endpoint; local files stays fixed under Docker, phase 3 of the setup wizard (issue #92)

**Context**: Auditing the wizard's remaining two steps (local files, browser history) against what's already shipped surfaced a real tension between two of this session's own earlier decisions, not just a gap to fill. Issue #92's original text says the local-files step should let the user "pick which of [the mounted folders] to actually watch" — but issue #52's own Q&A (this session, same day) deliberately settled on `local_files_watch_dirs` being **entirely fixed and read-only** under Docker, with the only way to change it being editing `HOST_WATCH_DIR` + restarting the stack — already implemented and shipped (`PUT /api/settings/sources` rejects any change to that field with a `422` while `running_in_docker` is set, `SettingsPanel.jsx` renders it as a plain list). Reopening that to allow picking a sub-list of watch dirs from within the mounted tree would mean either contradicting the already-shipped Settings behavior (which uses the same endpoint) or building Settings-only-vs-wizard-only special-casing on the same field — both worse than the alternative.

**Decision**:
1. **Local files**: no new backend work. Under Docker, the wizard's step is read-only/informational — it shows the current `local_files_watch_dirs` value (from the already-existing `GET /api/settings/sources`) with the same "edit `HOST_WATCH_DIR` + restart" instruction Settings already gives, rather than offering interactive picking. Outside Docker, the wizard step reuses the existing `POST /api/settings/browse-folder` + `PUT /api/settings/sources` flow directly — also nothing new needed. This is a deliberate, narrower reading of #92's original text, favoring consistency with #52's already-shipped design over the letter of the earlier issue description.
2. **Browser history** had a real, plain gap, unrelated to the tension above: `BROWSER_HISTORY_PATH` had **no update endpoint at all** — not even in a manual, non-Docker setup — the same category of gap Notion/GitHub/OpenRouter credentials had before phase 2. Fixed the same way: `config/settings.py::update_source_config()` gains a `browser_history_path` parameter (writes `BROWSER_HISTORY_PATH`, `""` clears it — same convention as the six date-range fields already in that function); `SourceConfigResponse`/`SourceConfigUpdateRequest` and both `GET`/`PUT /api/settings/sources` routes now carry it.
3. Under Docker, a raw host path typed into `BROWSER_HISTORY_PATH` is meaningless — the container can only reach whatever was mounted at `/host-data` (`docker-compose.yml`'s `HOST_DATA_DIR`, already documented in the README's Docker section for exactly this purpose). New `agent/mounted_files.py::list_host_data_files()` lists every file under that mount (empty outside Docker, where `/host-data` doesn't exist at all — the wizard falls back to a plain text input for the real host path in that case, same as a manual setup already requires) and `GET /api/setup/host-data-files` exposes it, so the wizard can render a real pick list instead of asking the user to type an in-container path blind. Capped at 500 files (`_MAX_FILES`) — protects against `HOST_DATA_DIR` pointed at something enormous producing an unusable, tens-of-thousands-of-rows response.

**Alternatives considered**: reopening `local_files_watch_dirs`'s Docker-mode restriction to allow picking a sub-list from within the mounted tree — rejected in favor of consistency with the already-shipped #52 decision, as explained above; a general-purpose "browse any mounted subtree" endpoint (arbitrary path parameter) instead of a fixed `/host-data` listing — rejected as unnecessary generality for the one concrete use case that exists today.

**Verified**: `uv run pytest` — new `tests/test_config/test_settings.py` (`browser_history_path` update/clear), new `tests/test_agent/test_mounted_files.py` (empty-when-missing, real listing with POSIX-style relative paths, directories excluded, the 500-file cap), updated `tests/test_api/test_settings_route.py` (every fake `env`/`update_source_config()` double updated for the new field — several pre-existing tests needed `browser_history_path` added to their fake return values or they'd fail with `TypeError: unexpected keyword argument` once the route started passing it through), new `tests/test_api/test_setup_route.py::TestListHostDataFiles`. Full suite passes (729 tests); `black`/`ruff` clean.

**Affects**: `config/settings.py`, `api/schemas.py`, `api/routes/settings.py`, `agent/mounted_files.py` (new), `api/routes/setup.py`, and the corresponding test files. The frontend wizard shell itself — the one piece that actually renders all three phases as connected steps — is the last remaining piece of issue #92, not started yet.

---

## 2026-09-02 — Validate-before-save credentials for Notion/GitHub/OpenRouter, phase 2 of the setup wizard (issue #92)

**Context**: Auditing what the wizard's remaining steps (provider mode, cloud credentials, Notion, GitHub) actually needed from the backend surfaced two real gaps, not just "wire up the frontend": (1) `NOTION_API_KEY`, `GITHUB_TOKEN`, and `OPENROUTER_API_KEY` had **no update endpoint at all** — only source-*scope* fields (`local_files_watch_dirs`, `notion_page_ids`, date ranges, ...) had one (`update_source_config()`); the only way to set these three was editing `config/.env` by hand, which is exactly what #92 exists to eliminate. (2) issue #92 explicitly asks for each credential to be "validated against the real API before letting the user continue (not just saved blind)" — Notion and GitHub already had real live-API checks (`agent/connection_check.py`), but only reading the *already-saved* value from settings, not a value the wizard is about to save; OpenRouter had no live-API check at all anywhere in the codebase — `agent/health.py`'s `_check_llm_provider()` only confirms a provider object can be constructed (a key is present), never that OpenRouter itself accepts it.

**Decision**:
1. New `config/settings.py::update_credentials_config(*, notion_api_key, github_token, openrouter_api_key, path)` — same shape as `update_source_config()`/`update_google_oauth_config()`, writes only the given fields to `config/.env`.
2. Refactored `agent/connection_check.py`'s `_check_notion()`/`_check_github()` to delegate to new, parameterized `notion_key_works(api_key)`/`github_token_works(token)` — pure functions taking a raw value directly rather than reading `env.notion_api_key`/`env.github_token`, so the wizard can validate a *pasted, not-yet-saved* value. The existing six-source `check_all_connections()`/Settings "Reverify" behavior is unchanged — same real API calls, same `ConnectionStatus` shape, just built from the extracted function now.
3. New `openrouter_key_works(api_key)` — verified directly against the real OpenRouter API: `GET https://openrouter.ai/api/v1/key` with `Authorization: Bearer <key>` returns `200` with usage/limit info for a valid key, `401` for an invalid one. Free (not a billed completion call) and fast, the same "cheap, real, no-cost check" shape every other source's validator already has.
4. New `POST /api/setup/validate` (`{"source": "openrouter"|"notion"|"github", "value": str}` → `{"status": "ok"|"error", "detail": str}`) — never touches `config/.env`, just runs the matching validator against the raw pasted value. New `POST /api/setup/credentials` (three optional fields) — saves via `update_credentials_config()`, no validation of its own. The wizard calls `/validate` first and only calls `/credentials` on a confirmed-good result, so a bad credential is never persisted at all — stronger than `PUT /api/settings/sources`'s "save unconditionally" behavior, deliberately, since that endpoint's fields (folder paths, page ID lists) don't have the same "silently breaks every future ingestion run" failure mode a bad API key does.
5. Provider mode itself needed **no** new backend work — `PUT /api/settings` already saves `provider_mode`; the wizard step is a pure UI shell around it (not built yet, tracked with the rest of the frontend wizard).

**A real bug found while testing, not just designing**: `POST /api/setup/validate`'s route handler initially dispatched via a module-level `_VALIDATORS = {"openrouter": openrouter_key_works, ...}` dict built once at import time — this captured the *original* function objects as values, so `monkeypatch.setattr("api.routes.setup.openrouter_key_works", ...)` in a test silently had no effect (the dict still held the pre-patch reference). Fixed by dispatching on the plain module-level names via an `if`/`elif` chain instead — a bare name reference inside a function body is looked up fresh from the module's namespace on every call, which a monkeypatch on that same module attribute does affect.

**Alternatives considered**: none of real weight — this mostly closed gaps the issue already specified rather than choosing between designs.

**Verified**: `uv run pytest` — new `agent/connection_check.py` tests (`TestNotionKeyWorks`, `TestGithubTokenWorks`, `TestOpenrouterKeyWorks`, all against mocked HTTP transports, no real network calls in the test suite itself — though the real OpenRouter endpoint shape was confirmed once, directly, against this machine's own real `OPENROUTER_API_KEY`, both a valid-key `200` and an invalid-key `401`), new `config/settings.py` tests (`TestUpdateCredentialsConfig`), new `api/routes/setup.py` tests (`TestValidateCredential`, `TestSaveCredentials`, including the dict-dispatch bug's regression coverage). Full suite passes; `black`/`ruff` clean.

**Affects**: `agent/connection_check.py`, `config/settings.py`, `api/schemas.py`, `api/routes/setup.py`, and the corresponding test files. The frontend wizard UI for these steps (and local files/browser history, still backend-untouched) is tracked separately, same issue (#92) — not started yet.

---

## 2026-09-02 — Guided Google OAuth (Gmail + Calendar), phase 1 of the setup wizard (issue #92)

**Context**: Issue #92's hardest piece, and the reason it's a separate issue from #52 at all. Today, connecting Gmail/Calendar means downloading a Desktop-type OAuth client JSON from Google Cloud Console, placing it at a configured path, and running `uv run python -m extractors.gmail --setup-auth` (a synchronous CLI script using `InstalledAppFlow.run_local_server()`, which opens a browser and blocks on its own temporary local HTTP server) — fine for a developer, impossible to ask of a wizard running entirely inside this app's own browser tab, with no terminal and no separate file to place.

**Decision**:
1. Asked directly rather than assumed: (a) should the OAuth Client ID/Secret be shipped as one shared credential in the repo, or should each user paste their own? — **paste their own** (two text fields, no downloaded-file-placement step, but still their own Google Cloud project — avoids embedding a real secret in a public repo and a single shared quota across every user). (b) Should Gmail and Calendar be one combined consent screen or two independent ones? — **one combined screen**, requesting both scopes at once, with the resulting token shared by both extractors.
2. New `extractors/google_oauth.py` (a peer to `extractors/gmail.py`/`extractors/calendar.py`, not `agent/` — see its own docstring for why: `agent/` may never depend on `extractors/` for real logic per `docs/Component_Map.docx`, so the actual OAuth mechanics can't live there even though the API layer needs to reach them) implements the OAuth2 authorization-code flow directly with `google_auth_oauthlib.flow.Flow` (not `InstalledAppFlow`) — the backend's own already-running, already-published HTTP endpoint (`GET /api/setup/google/oauth/callback`) is the redirect target, rather than a separate ephemeral local server `run_local_server()` would try to bind. `agent/google_oauth.py` is a thin re-export wrapper (matching `agent/browse.py`'s role) so `api/routes/setup.py` never imports `extractors/` directly, keeping the "Interface depends only on Agent's entrypoint" rule intact.
3. **Client type**: the wizard instructs the user to create a **Desktop app** OAuth client (not "Web application") in Google Cloud Console — Google accepts any `localhost`/`127.0.0.1` redirect URI dynamically for a Desktop-type client without pre-registering it, which is what makes a variable, per-install backend port (different in Docker vs. a manual dev setup) work with zero extra Console configuration. This is a server-side property of the Client ID itself set at creation time in Console — nothing in this code enforces it, only the wizard's own instructional copy communicates it.
4. The redirect URI itself is built from the *inbound request's own* `request.url_for()`, not a configured "public URL" setting — correct automatically whether this is `http://127.0.0.1:8080/...` (manual dev) or `http://localhost:<HOST_BACKEND_PORT>/...` (Docker), with nothing new to configure per deployment.
5. `POST /api/setup/google/oauth/start` builds and returns the consent URL for the wizard to open in a popup; `GET /api/setup/google/oauth/callback` (hit by the browser's own navigation following Google's redirect, never by `fetch()`) exchanges the code and renders a plain HTML success/failure page, since there's no JSON-consuming caller on that request; `GET /api/setup/google/oauth/status` lets the wizard's own tab detect success by polling, since the callback lands in a separate tab/window from the one that opened it.
6. **Backward compatible by construction, not by migration**: `extractors/gmail.py`'s/`extractors/calendar.py`'s own `_get_credentials()` try the new shared token (`extractors/google_oauth.py::load_credentials()`) first, falling back to each extractor's own older, separate per-service token file if the guided flow was never completed — an existing manual setup keeps working completely unchanged, with no config migration needed. `agent/connection_check.py`'s `_check_gmail()`/`_check_calendar()` likewise treat either path as "configured."
7. `GoogleOAuthError` gets its own registered exception handler in `api/main.py`, mapping to `400` (bad client input or Google itself rejecting the request — never this server's fault), distinct from the generic `500` every other unregistered exception falls through to.

**Alternatives considered**: keeping `InstalledAppFlow.run_local_server()` but running it in a background thread with a *fixed* (not random) port, published via Docker Compose — rejected as more complex for no real benefit over just using the backend's own existing, already-published port directly via the base `Flow` class, which is also the standard shape any web app's OAuth integration already takes; a shared embedded OAuth client (see point 1) — rejected per direct product decision.

**Verified**: `uv run pytest` — new `tests/test_extractors/test_google_oauth.py` (start/complete/load-credentials, all `Flow` network calls monkeypatched at the SDK boundary), `tests/test_api/test_setup_route.py` (all three endpoints, including the HTML callback page's success/failure branches and the `400` mapping), updated `tests/test_extractors/test_gmail.py`/`test_calendar.py` (`_get_credentials()`'s shared-token-first-then-fallback), updated `tests/test_agent/test_connection_check.py` (the shared token also counts as "configured"), new `tests/test_config/test_settings.py::TestUpdateGoogleOAuthConfig`. Full suite passes (671 tests); `black`/`ruff` clean. Not yet manually verified against a real Google Cloud OAuth client end-to-end (needs a real Client ID/Secret and a real consent screen) — the mechanics are unit-tested at the SDK boundary, not integration-tested against live Google infrastructure yet.

**Affects**: `extractors/google_oauth.py` (new), `agent/google_oauth.py` (new, thin wrapper), `api/routes/setup.py` (new), `api/schemas.py`, `api/main.py`, `extractors/gmail.py`, `extractors/calendar.py`, `agent/connection_check.py`, `config/settings.py`, `config/.env.example`, and the corresponding test files. Frontend wizard UI, and the remaining wizard steps (provider mode, cloud credentials, Notion, GitHub, local files, browser history), are tracked separately, same issue (#92) — not started yet.

---

## 2026-09-02 — Docker Compose packaging (issue #52)

**Context**: The last major milestone before this project can be shared as a "download and run it" portfolio piece — running it required a full manual developer setup (`uv sync`, `npm install`, installing/running Neo4j and Ollama by hand, registering a cron/Task Scheduler entry) with no persistence guarantees. Scoped explicitly to **infrastructure only** this round, not the full first-run onboarding wizard described in the original issue text — that's been split out to issue #92, so this round could ship without also blocking on OAuth-in-a-container design work. Two real bugs were only discoverable by actually building and running the containers, not by reading code — both are called out below since they'd otherwise silently break anyone else's first `docker compose up`.

**Decision** — the shape of the stack:
1. `docker/backend.Dockerfile`: one shared image for both the `backend` and `scheduler` compose services (same codebase, different `command`) — `python:3.12-slim` + `uv sync --frozen --no-dev` (excludes pytest/black/ruff from the runtime image). **Bug found and fixed**: the first working build used `CMD ["uv", "run", "python", "-m", "api.main"]` — verified directly that `uv run` re-syncs (and re-resolves *every* dependency group, pulling `black`/`ruff` back in) on every single container start, needing network access each time regardless of what was already locked in at build time. Fixed per uv's own documented Docker pattern: `ENV PATH="/app/.venv/bin:$PATH"` then `CMD ["python", "-m", "api.main"]` directly, using exactly what was already installed with no runtime re-sync at all.
2. `docker/frontend.Dockerfile`: multi-stage — `node:22-alpine` builds the static bundle (`npm ci && npm run build`), then a bare `nginx:1.27-alpine` serves `dist/`. No custom nginx config needed — there's no client-side router in this app to need SPA fallback routing for.
3. `docker-compose.yml`: `neo4j` (official `neo4j:5-community`, with a healthcheck the `backend`/`scheduler` services `depends_on: condition: service_healthy`), an optional `ollama` service gated behind a `local-llm` Compose profile (skipped entirely under `provider_mode: fully_cloud`, since nothing else needs it then), `backend`, `scheduler`, and `frontend`. `backend`/`scheduler` both mount the *same*, already-filled-in `config/.env` read-only via `env_file:` — the exact file a manual dev setup already produces, with every credential (Notion, GitHub, OpenRouter) working unchanged — while a small `environment:` block overrides only the handful of fields that must point at in-container/in-network locations instead of host ones (`SQLITE_DB_PATH`, `CHROMA_PERSIST_DIR`, `NEO4J_URI=bolt://neo4j:7687`, `OLLAMA_HOST=http://ollama:11434`, `LOCAL_FILES_WATCH_DIRS=/data/watched`, `RUNNING_IN_DOCKER=true`) — real process env vars set this way take priority over the mounted `.env` file's own values (pydantic-settings' documented precedence order), so this composes cleanly with zero special-casing in `config/settings.py` itself.
4. A second, root-level `.env` (`.env.docker.example` → `.env`, distinct from `config/.env`) holds only what `docker-compose.yml` itself needs to fill in `${VARS}`: `NEO4J_PASSWORD`, `HOST_WATCH_DIR`/`HOST_DATA_DIR` (the two bind-mount sources), and the published host ports.
5. **Bug found and fixed, the more serious one**: `api/main.py` hardcoded `uvicorn.run(app, host="127.0.0.1", ...)` for its "local-only, single-user" security posture on bare metal. Verified directly against a real running stack: the backend logged a fully healthy startup, but was completely unreachable through Docker's published port — `127.0.0.1` inside a container is the container's *own* loopback, invisible to Docker's port-forwarding, which connects to the container's external interface. New `_select_bind_host()` returns `"0.0.0.0"` when `running_in_docker` is set, `"127.0.0.1"` otherwise — the container's network namespace is already the isolation boundary at that point, so this doesn't reopen the LAN-exposure concern the loopback bind exists for on bare metal.
6. `vite.config.js` already computed `VITE_API_BASE_URL` from a dev-only `data/backend_port.txt` file that doesn't exist in a fresh Docker build context at all (and even if it did, the backend's published port is a Compose choice made *before* any container ever runs, not discoverable at frontend-build time). Now prefers a real `VITE_API_BASE_URL` environment variable when set, falling back to the port-file lookup otherwise — `docker/frontend.Dockerfile` passes it as a build `ARG`, itself derived from `docker-compose.yml`'s `HOST_BACKEND_PORT` (the *published* port, since the browser making the request runs on the host, not inside the Docker network).
7. `.dockerignore` (repo root, for the backend build) and `frontend/.dockerignore` (a **separate** file — Docker resolves ignore rules relative to each build's own context, so the root file has zero effect on the frontend build, whose context is `frontend/` itself) — verified directly, twice: a first pass without `**/`-prefixed patterns let `config/client_secret_*.json` (a real, untracked-but-present local Google OAuth credential file) leak straight into a built backend image layer, since Docker's ignore-pattern matching does *not* recurse into subdirectories the way `.gitignore`'s does without an explicit `**/` prefix — every secret pattern was rewritten with one. Separately, omitting `frontend/.dockerignore` let the frontend build's context balloon to ~430MB and 200+ seconds by including the local `node_modules/`/`dist/`, both irrelevant since they're rebuilt fresh inside the image regardless — down to ~60MB/60s once excluded.

**Alternatives considered**: a real `cron` daemon inside the scheduler container instead of `scheduler/loop.py` + `croniter` — rejected (see the separate `croniter` decision entry) for needing extra care around PID-1 signal handling and stdout logging that a plain Python loop avoids; per-source Docker volume mounts for Gmail/Calendar credentials and browser history individually, instead of one generic `HOST_DATA_DIR` passthrough mount — rejected as over-engineering ahead of issue #92's actual OAuth design work, which may reshape how these get provided entirely.

**Verified**: built and ran the real stack end-to-end via `docker compose up -d --build` (all four containers: `neo4j` reaching `healthy`, `backend`, `scheduler`, `frontend` all `Up`) — confirmed `GET /api/health` returns `"ok"` for every store from the *host* machine (not just from inside the container), `GET /api/settings` returns real credentials threaded through from the mounted `config/.env` unchanged, `GET /api/settings/sources` reports `"running_in_docker": true` and `"local_files_watch_dirs": ["/data/watched"]` (the fixed container path, per the `LOCAL_FILES_WATCH_DIRS` override), and both Docker-mode guards (`POST /api/settings/browse-folder`, `PUT .../sources` with `local_files_watch_dirs`) return the expected `422` against the live container. `uv run pytest` — full suite passes (639 tests) including new `tests/test_api/test_main.py` (`_select_bind_host()`); `black`/`ruff` clean.

**Affects**: `docker/backend.Dockerfile`, `docker/frontend.Dockerfile`, `docker-compose.yml`, `.dockerignore`, `frontend/.dockerignore`, `.env.docker.example`, `docker/empty/.gitkeep`, `api/main.py`, `frontend/vite.config.js`, `README.md`, and the corresponding test files.

---

## 2026-09-02 — Local files watch dirs become a fixed, read-only Docker volume mount

**Context**: First real design question for Docker packaging (issue #52): the existing "Local folders to watch" feature has a native OS folder-picker dialog (`agent/browse.py`, shells out to PowerShell — Windows-only, and meaningless inside a Linux container regardless) and lets the user freely edit an arbitrary list of host paths. Neither holds once the backend runs in a container: there's no GUI for a native dialog, and a container can only see host folders that were explicitly mounted as volumes *before* it started — the running app has no way to reach out and mount a new host folder into itself at runtime, only `docker-compose.yml` can, at container-start.

**Decision**: a new `EnvSettings.running_in_docker: bool = False` (`RUNNING_IN_DOCKER` env var — set only by `docker-compose.yml`'s own `environment:` block, never by a developer's `config/.env`) signals this mode. `local_files_watch_dirs` itself keeps working exactly as before under the hood — still just `LOCAL_FILES_WATCH_DIRS`, still read by `extractors/local_files.py` the same way — but under Docker it's set once, by `docker-compose.yml`, to a fixed container path (`/data/watched`) backed by a host-folder volume mount the user configures via `HOST_WATCH_DIR` in the *host's* `.env` before `docker compose up`, not through the running app. `PUT /api/settings/sources` now rejects a `local_files_watch_dirs` change with `422 not_available_in_docker` while `running_in_docker` is set (every other field is unaffected); `POST /api/settings/browse-folder` returns the same `422` instead of attempting the native dialog at all. `GET`/`PUT /api/settings/sources` both return the new `running_in_docker` flag so the frontend knows which UI to render. `SettingsPanel.jsx`'s "Local folders to watch" section renders read-only (a plain list, no "Browse…" button, no editable textarea) when the flag is set, with a hint pointing at `HOST_WATCH_DIR` + restarting the stack as the way to actually change it.

**Alternatives considered**: an in-app folder browser navigating the container's own mounted filesystem (rejected in favor of the simpler fixed-mount approach, per direct product decision — asked directly and answered "fixed host folder(s), mounted once," not "browse the container's filesystem," since the latter is meaningfully more UI work for a niche benefit once there's already a `.env`-based mount step the user has to do once anyway).

**Verified**: `uv run pytest` — new tests in `tests/test_config/test_settings.py` (`running_in_docker` default/env-read), `tests/test_api/test_settings_route.py` (`GET` reports the flag, `PUT` rejects only `local_files_watch_dirs` while every other field still updates, `browse-folder` returns 422 without attempting the dialog). Full suite passes; `black`/`ruff` clean. `npm run lint`/`npm run build` clean.

**Affects**: `config/settings.py`, `config/.env.example`, `api/schemas.py`, `api/routes/settings.py`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`, and the corresponding test files.

---

## 2026-09-02 — Removed the unused `sentence-transformers` dependency; added `croniter`

**Context**: Starting Docker packaging (issue #52) meant building a real container image for the first time, which made an existing problem concrete: `sentence-transformers` (and the `torch`/`transformers`/`scikit-learn` stack it pulls in — over a dozen packages, hundreds of MB) has been listed in `pyproject.toml` since local embedding was first removed a few iterations ago (see the multi-step provider-mode/embedding history earlier in this file), but nothing in the codebase actually imports it any more — confirmed directly via a project-wide search: every remaining mention was a docstring/comment reference (`providers/base.py`, `providers/local_provider.py`, `storage/chroma_store.py`, `pipeline/chunking.py`), not a real `import`. It was dead weight in every `uv sync`, and would have meant baking an unnecessarily huge Docker image (a CPU-only PyTorch install alone is roughly 800MB-1GB) for a dependency doing nothing.

**Decision**: removed via `uv remove sentence-transformers`; the docstrings/comments referencing it were updated to either name what's actually used now (`OllamaEmbeddings`/`OpenAIEmbeddings`) or drop the specific-library claim in favor of a general one. Separately, the Docker scheduler sidecar (see the Docker packaging entry below) needs to run `scheduler/daily_batch.py` on `config.yaml`'s `ingestion.schedule` cron expression from inside a container — added `croniter` (pure Python, no native dependencies, a well-established library for exactly "compute the next fire time for a cron string") rather than shelling out to a real `cron` daemon inside the container, which needs extra care around PID-1 signal handling and stdout logging in containers that a plain Python loop avoids entirely.

**Alternatives considered**: a cron daemon (or `supercronic`, a container-oriented cron replacement) for the scheduler sidecar — rejected in favor of `croniter` + a plain Python loop, since the project is already Python/`uv`-based throughout and a pure-Python loop is directly unit-testable the same way every other module in this codebase is, where a cron daemon's behavior would only be verifiable by actually running a container.

**Verified**: `uv run pytest` — full suite passes (622 tests), confirming nothing actually depended on `sentence-transformers`. `black`/`ruff` clean.

**Affects**: `pyproject.toml`, `uv.lock`, `providers/base.py`, `providers/local_provider.py`, `storage/chroma_store.py` (docstrings only).

---

## 2026-09-01 — Local embedding via Ollama restored (frontend): double-confirm + auto-reset on a provider_mode switch

**Context**: Follow-up to the backend half of issue #88 (see the entry directly below). With both embedding fields now freely editable and `provider_mode` switching which one is active, a mode switch silently invalidates every existing embedding — the same dimension-mismatch risk an earlier iteration solved by *freezing* both embedding fields, which this issue explicitly asked not to do. Instead, the switch itself needed to become deliberately hard to trigger by accident and destructive-by-design: warn clearly, confirm twice, then wipe the database automatically (no separate manual "Reset all data" click needed) — but stop short of auto-triggering re-ingestion, unlike the existing `cloud_embedding_model`-change flow.

**Decision**:
1. `ConfirmDialog.jsx` gains a `critical` prop, layered on top of (not replacing) the existing `danger` prop — `.confirm-dialog.critical`/`.confirm-backdrop.critical` in `index.css` render the *entire* dialog (not just the confirm button) on a solid red background with a white confirm button, a visibly more alarming treatment than `danger`'s red-accented button alone. `useConfirm()` passes `critical` through unchanged, same as every other option.
2. `SettingsPanel.jsx`'s Models section now shows only the generation + embedding pair matching the active `provider_mode` (`isLocal` → local pair, `isCloud` → cloud pair), not all four fields with the inactive ones merely disabled as before — both fields of the inactive pair are still round-tripped through `GET`/`PUT /api/settings` untouched, so switching back doesn't lose an edited value.
3. `saveEmbeddingModel()` (previously hardcoded to `cloud_embedding_model`) is generalized to take the field name as a parameter, so both `local_embedding_model` and `cloud_embedding_model` share the same confirm-then-reset-then-reingest flow — changing either embedding model in place, without switching modes, still needs a reset since it's still a vector-space change.
4. New `saveProviderMode(value)`: awaits two consecutive `confirm({critical: true, ...})` calls (a shared `ProviderModeSwitchWarning` body, since neither prompt is meant to feel like "the real one" — the second exists purely as friction), reverting the radio to its last-saved value if either is declined. Only after both confirms does it call `putSettings({provider_mode})` then `onResetAll()` — explicitly not followed by `onTriggerIngestion()`, unlike `saveEmbeddingModel()`'s flow, per the issue's explicit requirement that a mode switch never auto-starts ingestion.

**Alternatives considered**: reusing the existing `danger` prop for the mode-switch dialog instead of adding `critical` — rejected because the issue explicitly asked for something visually more severe than the app's existing danger styling, and reusing the same look would make the mode switch feel no more consequential than "Reset all data," when it's strictly worse (irreversible data loss *plus* no auto-recovery path via re-ingestion).

**Verified**: `npm run lint`/`npm run build` clean. Not separately unit-tested (no existing frontend test suite in this project — see `Coding_Conventions.docx`); not yet manually verified against a live Ollama instance (no local embedding model pulled on this machine as of this commit) — the confirm-dialog flow, field visibility, and build were checked, but a real `fully_local` embedding call has not been exercised end-to-end yet.

**Affects**: `frontend/src/components/ConfirmDialog.jsx`, `frontend/src/hooks/useConfirm.jsx`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`.

---

## 2026-09-01 — Local embedding via Ollama restored (backend); `fully_local` genuinely zero-cost again

**Context**: A real ingestion run under `fully_cloud` cost roughly $20-25 and took ~3 hours (see the README's cost/time note), almost entirely on cloud LLM calls. `provider_mode` had drifted to only controlling *generation* — embedding always went through OpenRouter regardless of mode (see the multi-step history in this file's earlier `cloud_embedding_model`/local-embedding-removal entries), meaning `fully_local` still required `OPENROUTER_API_KEY` and still incurred embedding cost on every ingested item. Issue #88 asked to bring back a genuine "fully local" mode: both generation and embedding local, no cloud dependency at all, using Ollama only (explicitly not `sentence-transformers`, the original local-embedding path, to avoid juggling two different local inference backends) — while keeping both embedding fields (local and cloud) freely editable rather than re-freezing them as a previous iteration did.

**Decision** (backend half — frontend double-confirm/reset/UI work is a separate, later commit):
1. `providers/local_provider.py::create_local_provider()` now builds an embedding function too, via `langchain_ollama.OllamaEmbeddings` (mirrors `create_openrouter_provider()`'s own `OpenAIEmbeddings`-via-LangChain pattern) — the same "go through LangChain, not a hand-rolled HTTP call" convention as the cloud side.
2. `providers/base.py::get_provider("embedding")` now branches on `provider_mode` like every other task, instead of unconditionally resolving to OpenRouter.
3. `config/settings.py::LLMConfig` gains `local_embedding_model: str = "nomic-embed-text"` back as a real field; `update_llm_config()` gained a matching parameter; `config.yaml` documents it alongside the other three model fields.
4. `api/schemas.py`/`api/routes/settings.py`: `local_embedding_model` added to `SettingsResponse`/`SettingsUpdateRequest`/`SettingsUpdateResponse` and threaded through both routes.

Unlike the earlier "freeze both embedding fields" iteration (forcing both to the same 384 output dimensionality so a `provider_mode` switch could never mismatch vectors), this time both embedding models stay independently editable, per the issue's explicit request. The dimension-mismatch risk this reopens (switching modes, or editing either embedding model to a different width, can leave Chroma holding vectors from an incompatible space) is *not* guarded anywhere in this backend layer — it's deliberately left to the frontend, which issue #88 requires to treat a `provider_mode` switch as fully destructive (double-confirm, then an automatic full reset, no auto re-ingest) before this code is trusted with real data. That frontend work has not landed yet as of this commit — do not treat local-embedding support as safe to exercise via a raw `provider_mode` switch through the API without it.

**Alternatives considered**: re-freezing both embedding models to a shared dimensionality again, like the earlier iteration — rejected because the issue explicitly asked for both fields to stay editable; suggesting suitable local models for the user's hardware via `llmfit` — investigated and explicitly skipped (separate Rust binary requiring manual install, weaker GPU detection on Windows, and unreliable once this app is Dockerized per issue #52's container-based hardware-detection problem).

**Verified**: `uv run pytest` — updated `tests/test_providers/test_local_provider.py` (embed_fn built from the configured/overridden model and host, `generate_embeddings()` delegates to it), `tests/test_providers/test_base.py` (`get_provider("embedding")` now resolves local under `fully_local`), `tests/test_config/test_settings.py`, `tests/test_api/test_settings_route.py`. Full suite passes except one pre-existing, unrelated, environment-dependent failure (`TestEffectiveDateRanges::test_github_still_has_no_default_when_unset`, caused by this machine's own `config/.env` having `GITHUB_DATE_RANGE_START` set — not caused by this change). `black`/`ruff` clean.

**Affects**: `providers/local_provider.py`, `providers/base.py`, `providers/openrouter_provider.py` (docstrings only), `config/settings.py`, `config/config.yaml`, `api/schemas.py`, `api/routes/settings.py`, and the corresponding test files. Frontend double-confirm/auto-reset/field-visibility work tracked separately, same issue (#88).

---

## 2026-09-01 — Restored an always-visible color-key legend on the graph

**Context**: When the per-source filter moved from a passive bottom legend into a toolbar "Filter" button/panel (see the toolbar-panel entry below), the color key went with it — the panel only renders while open, so there was no longer any way to see what a node's color meant without opening Filter first. Flagged directly after using the real app.

**Decision**: added back a small, always-visible, non-interactive legend (`graph-view-legend`, bottom-left of the canvas) — one entry per filterable source type, its swatch and label, independent of the interactive Filter panel. An entry dims (doesn't disappear) when that source is currently hidden by the filter, so the legend stays a consistent reference regardless of filter state. This is deliberately separate from the filter panel, not a merge of the two — the panel is the control (checkboxes, All/None), this is just the reference.

**Affects**: `frontend/src/components/GraphView.jsx`, `frontend/src/index.css`.

---

## 2026-09-01 — cancel_ingestion() no longer finalizes "cancelled" on an unconfirmed kill

**Context**: GitHub Copilot's review of this PR flagged a real gap in the force-kill cancel logic added the same day: `cancel_ingestion()` only guarded the *kill attempt* on `run.pid is not None`, but unconditionally finalized the row to `status="cancelled"` afterward regardless of whether the kill actually happened. Two real cases fell through: a run with no recorded `pid` (e.g. a legacy row from before that column existed) skipped the kill entirely but still got marked cancelled, and an `OSError` other than `ProcessLookupError` (e.g. a permissions failure) left the process possibly still running while the DB claimed otherwise. Either way, the batch process could still be alive and writing to storage while `ingestion_runs` said the run was over — a real state mismatch that could let a second run start concurrently with the "cancelled" one.

**Decision**: `cancel_ingestion()` now only finalizes `status="cancelled"` when the kill is actually confirmed — the signal was delivered successfully, or the process was already gone (`ProcessLookupError`). A missing `pid` returns immediately after setting the cooperative `cancel_requested` flag, without touching `status`; a kill that raises any other `OSError` logs the same warning as before but also returns without finalizing. In both cases the row is left `"running"` for the batch to finalize itself once it reaches its own next check point (or for a human to investigate if it's genuinely wedged) — never a guess dressed up as a confirmed outcome.

**Alternatives considered**: none — this is a straightforward correctness fix following the reviewer's own suggested shape (only set the cooperative flag, don't touch status, when the kill can't be confirmed).

**Verified**: `uv run pytest` — `tests/test_agent/test_ingest_trigger.py::TestCancelIngestion::test_a_missing_pid_does_not_finalize_the_run` rewritten to assert `status == "running"` instead of `"cancelled"`; new `test_a_failed_kill_does_not_finalize_the_run` covers the `OSError` path. Full suite passes; `black`/`ruff` clean.

**Affects**: `agent/ingest_trigger.py`, `tests/test_agent/test_ingest_trigger.py`.

---

## 2026-09-01 — Settings sections rebalanced across the two columns

**Context**: Adding the "Calendar date range" section pushed the right column (source scope + date ranges + Data Ingestion/Connected Sources) considerably taller than the left (Generation Provider/Models/Danger Zone) — asked to balance the two visually. A single centered column (no side-by-side split at all) was tried first, directly per a request, then reverted per a follow-up one, in favor of keeping the two-column layout but rebalancing which sections go in which column.

**Decision**: moved the three source-scope textareas ("Local folders to watch," "Notion page scope," "GitHub repository scope") from the right column into the left, inserted between "Models" and "Danger Zone". Left column is now Generation Provider/Models/the three scope textareas/Danger Zone; right column is the three date-range sections plus Data Ingestion + Connected Data Sources — much closer in height than before, without touching the sections' own content or logic.

**Affects**: `frontend/src/components/SettingsPanel.jsx`.

---

## 2026-09-01 — Consolidated two drifted copies of the source-type color map

**Context**: Asked directly whether the chat screen's "Data Source Indicators" pills used the same colors as the graph's nodes — they didn't. Two separate `SOURCE_TYPE_COLORS` definitions existed: `GraphView.jsx` had its own local copy (already correctly fixed for the GitHub-color and calendar-key issues from 2026-08-31), while `ChatWindow.jsx` imported from a *different* file, `frontend/src/constants/sourceColors.js`, whose own docstring claimed it was "shared between GraphView.jsx and ChatWindow.jsx" — but `GraphView.jsx` had stopped actually using it at some point without updating it, so its notion/gmail/github colors were stale and wrong, and its calendar entry was still keyed `google_calendar` (never matching any real `source_type`, so the calendar pill rendered with no color at all — confirmed directly from a screenshot).

**Decision**: `constants/sourceColors.js` is now genuinely the one place these colors are defined — updated to the correct values (matching what `GraphView.jsx`'s local copy already had), with the key fixed to `calendar`. `GraphView.jsx`'s local copy is deleted; it now imports `SOURCE_TYPE_COLORS` from the shared file like `ChatWindow.jsx` already did. Both call sites can no longer drift apart, since there's only one definition left to edit.

**Verified**: `npm run lint`/`npm run build` clean. No frontend test suite exists in this project to add a regression test to (see `Coding_Conventions.docx`); manually verified in the running app that the chat pills and graph nodes/filter panel now show identical colors per source, including a now-visible calendar dot.

**Affects**: `frontend/src/constants/sourceColors.js`, `frontend/src/components/GraphView.jsx`.

---

## 2026-09-01 — Ingestion cancel now force-kills the process, not just a cooperative flag

**Context**: Verified directly this session — clicking "Stop" on a real, large local-files run (thousands of matched files from an overly broad watch folder) had no visible effect for a long time. The existing cancellation design (`request_ingestion_cancellation()`'s flag, checked inside an extractor's `on_progress` callback or once between sources) only ever gets checked during a source's *extraction* phase. Once extraction for a source finishes, `scheduler/daily_batch.py::_run()` moves into noise-filtering, batched metadata generation, and a per-item chunk/embed/store loop for every filtered item from that source — none of which check the flag at all. For a source with thousands of matched items, that phase alone (especially with `provider_mode: fully_cloud`, one LLM call per item for metadata) can run far longer than any reasonable expectation of "Stop" actually stopping something.

**Decision**: `POST /api/ingest/cancel` (`agent/ingest_trigger.py::cancel_ingestion()`) now force-kills the batch process outright, rather than only setting the cooperative flag (which is still set too, in case the process happens to hit a check point first — cheap and harmless to keep). This requires knowing the process's OS pid, which is recorded by the process *itself*: `storage/sqlite_store.py::start_ingestion_run()` now writes `os.getpid()` into its own `ingestion_runs` row at creation time — the process creating the row always is the process that will run the batch, so there's no coordination gap the way there would be trying to pass the pid in from whichever process spawned it (that process doesn't yet know the row's id at spawn time; the batch process itself doesn't know if/who needs its pid). `cancel_ingestion()` reads the target run's row, calls `os.kill(pid, signal.SIGTERM)` (which `os.kill` maps to `TerminateProcess()` — immediate and forceful, not a catchable signal — on Windows, this project's primary target platform), and then finalizes the row to `status="cancelled"` itself, since a killed process can never call `complete_ingestion_run()` on its own way out. Guarded against a race (the batch legitimately finishing in the instant between reading its status and the kill signal landing) by re-reading the row immediately before writing "cancelled," only doing so if it's still `"running"` — never clobbering a real completed status. A missing pid, an already-exited pid (`ProcessLookupError`), or an unknown/already-finished run are all handled as no-ops on the kill itself, without preventing the row finalization where appropriate.

Because the pid lives on the run's own row rather than in an in-process variable held by whichever API process happened to spawn it, this also transparently covers a batch started by the scheduled cron/Task Scheduler invocation, or a batch whose originating API process has since restarted — the same "cross-process signal via the shared SQLite file" reasoning `cancel_requested`/`current_item` already used, just for one more piece of state.

**Alternatives considered**: keeping the module-level `subprocess.Popen` handle in `agent/ingest_trigger.py` and calling `.terminate()`/`.kill()` on it directly — rejected because it only works for a batch this exact, still-running API process instance spawned, not a scheduled or orphaned one, and provides no advantage over the pid-on-the-row approach for the one case it does cover.

**Verified**: `uv run pytest` — new tests in `tests/test_storage/test_sqlite_store.py` (`TestStartIngestionRunPid`, `TestGetIngestionRun`) and a rewritten `tests/test_agent/test_ingest_trigger.py::TestCancelIngestion` (force-kills the recorded pid with the right signal, finalizes to `"cancelled"`, a missing/already-exited pid doesn't raise, a run that already completed on its own is left alone rather than clobbered, an unknown run id is a no-op) — every one of these mocks `os.kill`, since `start_ingestion_run()` in a test records the *test runner's own pid* (no real subprocess spawned in a unit test), and the real `os.kill` would otherwise send `SIGTERM` to pytest itself. `tests/test_api/test_ingest_route.py` updated the same way. Full suite passes (605 tests); `black`/`ruff` clean. Manually verified against the real app: triggered a real large local-files run, confirmed it was previously unkillable via Stop for a long time, then confirmed the fixed version's Stop button ends the OS process immediately.

**Affects**: `storage/sqlite_store.py`, `agent/ingest_trigger.py`, `api/routes/ingest.py`, and the corresponding test files.

---

## 2026-09-01 — Serialized Settings auto-save to prevent lost updates

**Context**: GitHub Copilot's review of PR #81 flagged a real race in the per-field auto-save added earlier the same day: `PUT /api/settings` and `PUT /api/settings/sources` both implement a read-the-whole-file, mutate-the-one-field, write-the-whole-file-back update (`update_llm_config()`/`update_source_config()`). Two `onBlur`/`onChange` saves firing close together — tabbing quickly through several fields, or a slow save still in flight when the next field blurs — could send overlapping requests; whichever request's write lands second would have read the file *before* the first request's write, silently discarding it (a lost update), and out-of-order responses could briefly show stale values in the UI.

**Decision**: `SettingsPanel.jsx` gains a single shared "tail" promise (`saveQueueRef`) and an `enqueueSave(work)` helper that chains `work` onto it. Every save path (`saveLlmField`, `saveEmbeddingModel`, `saveScopeTextarea`, `saveDateField`) now runs its entire body through `enqueueSave()` — including `saveEmbeddingModel`'s confirm-dialog wait, since the modal already blocks interaction with every other field anyway, so serializing across the wait adds no real delay. This guarantees at most one `PUT` is in flight at a time, in the order the user actually triggered them, without needing any server-side locking (single-user, single browser tab — a client-side queue is sufficient).

**Alternatives considered**: server-side optimistic concurrency (an ETag/version check on `config.yaml`/`.env`) — rejected as overkill for a local, single-user system with no other writer; a client-side ordering guarantee is enough to eliminate the actual race without adding server complexity that would only matter for genuinely concurrent multi-client access, which this app doesn't have.

**Verified**: `npm run lint`/`npm run build` clean. Not separately unit-tested (no existing frontend test suite in this project — see `Coding_Conventions.docx`); manually verified in the running app that rapidly tabbing through several fields no longer drops any of them.

**Affects**: `frontend/src/components/SettingsPanel.jsx`.

---

## 2026-09-01 — Graph filter moved into a toolbar panel; Gmail/Calendar default date ranges

**Context**: Two follow-up requests. (1) The graph's per-source filter previously lived only as small color-swatch chips in a legend row below the canvas — functional, but easy to miss and not obviously interactive; asked for a more visible, dedicated filter control. (2) Gmail and Calendar's date-range scope defaulted to fully unbounded when unset (all-time mailbox / all-time calendar) — asked to default Gmail to the last 15 days and Calendar to today through the next 30 days instead, so a fresh install doesn't pull someone's entire history by default.

**Decision**:
1. `GraphView.jsx` gains a "Filter" button in the existing top-right toolbar (next to zoom/fit), showing a count badge (e.g. "Filter (3/5)") when any source is hidden. Clicking it opens a floating checkbox panel (`graph-view-filter-panel`) — one row per filterable source type with its swatch, plus "All"/"None" quick actions — closing on an outside click. This replaces the old bottom legend entirely (removed, since keeping both would mean two divergent controls doing the same thing); the filtering logic itself (`visibleSourceTypes`, `toggleSourceType()`, the effect that re-fits the view when the filter set changes) is unchanged.
2. `config/settings.py::EnvSettings` gains `effective_gmail_date_range_start`/`_end` and `effective_calendar_date_range_start`/`_end` computed properties, alongside new `calendar_date_range_start`/`_end` raw fields (`CALENDAR_DATE_RANGE_START`/`_END` in `.env`). Unlike `watch_dirs`/`notion_page_ids_list`/`github_repos_list` (reverted to "unset means nothing" the day before), these four **always** resolve to a real window: Gmail defaults to `[today − 15, today]`, Calendar to `[today, today + 30]`, when the underlying `.env` variable is unset **or explicitly cleared** in Settings — there is no "unbounded" state left reachable for these two sources via the UI (an explicit, deliberately old/far date is still possible by typing one in). GitHub's own date range keeps its original unset-means-unbounded behavior; only Gmail and Calendar were asked to default. `extractors/gmail.py` already had a date-range mechanism (`_effective_window()`, floor/ceiling merged with the incremental `since`) — it now reads the `effective_*` properties instead of the raw, possibly-`None` ones, so the merge logic itself needed no change. `extractors/calendar.py` had no date-range filtering at all before this — Calendar's `since`/`updatedMin` only ever meant "changed recently," an orthogonal axis from "happening in this window." Added `timeMin`/`timeMax` to its `events().list()` call, computed from the effective range (day-after-end at midnight, same inclusive-end convention as Gmail's `before:`); Google's API ANDs `timeMin`/`timeMax` with `updatedMin`, so both filters apply independently, same effective shape as Gmail's own combined incremental+range check. `GET`/`PUT /api/settings/sources` return the *effective* value for Gmail/Calendar (visible, not hidden, matching the reasoning the reverted watch-folder default originally had) but the *raw* (possibly `None`) value for GitHub.

**Alternatives considered** (calendar): filtering client-side in `_event_to_item()` after fetching everything — rejected in favor of `timeMin`/`timeMax`, which lets the Calendar API do the filtering server-side, matching how the existing `updatedMin` incremental check already works and avoiding fetching a user's entire event history just to discard most of it locally.

**Verified**: `uv run pytest` — new tests in `tests/test_config/test_settings.py` (`TestEffectiveDateRanges`: each default, each configured-value override, confirms GitHub is unaffected), `tests/test_extractors/test_calendar.py` (`TestDateRangeFiltering`: default window, a configured window, combined with `updatedMin`), `tests/test_api/test_settings_route.py` (both routes' new field); existing `tests/test_extractors/test_gmail.py::TestDateRangeScoping` and its fake-env helper updated for the renamed attribute the extractor now reads. Full suite passes; `black`/`ruff` clean. Frontend: `npm run lint`/`npm run build` clean.

**Affects**: `frontend/src/components/GraphView.jsx`, `frontend/src/index.css`, `config/settings.py`, `extractors/gmail.py`, `extractors/calendar.py`, `api/schemas.py`, `api/routes/settings.py`, `frontend/src/components/SettingsPanel.jsx`, and the corresponding test files.

---

## 2026-09-01 — Settings auto-save, cloud generation as the default provider, no default watch folder/Notion scope

**Context**: Three related requests, all in the direction of "less implicit behavior, less friction": (1) `fully_local` was the default `provider_mode`, but real usage this session showed cloud generation is both faster and noticeably more capable for metadata/relationship judging — asked to make it the default instead; (2) the Settings screen required an explicit "Save Changes" click even though every field is otherwise just a plain input — asked to remove that extra step entirely; (3) `EnvSettings.watch_dirs`' fallback to an auto-created `DEFAULT_WATCH_DIR` (added earlier for issue #55) and the specific `LOCAL_FILES_WATCH_DIRS`/`NOTION_PAGE_IDS` values already sitting in `config/.env` from earlier extractor testing were both a form of implicit default scope the user doesn't want — asked to remove it so an unconfigured install (or the real `.env` right now) genuinely watches/scopes nothing until the user explicitly sets something.

**Decision**:
1. `config/settings.py::LLMConfig.provider_mode` default changed from `"fully_local"` to `"fully_cloud"`; `config/config.yaml`'s committed value changed to match. `fully_local` remains fully supported, just no longer the default for a fresh config.
2. `EnvSettings.watch_dirs` reverted to returning `[]` when `LOCAL_FILES_WATCH_DIRS` is unset — the `DEFAULT_WATCH_DIR` constant and its on-demand `mkdir` are removed entirely (undoing the 2026-08-31 "Default local watch folder" decision below). `agent/connection_check.py::_check_local_files()`'s `if not watch_dirs: return not_configured` branch is reachable again through the real property, as it was before that change. The real `config/.env`'s `LOCAL_FILES_WATCH_DIRS` and `NOTION_PAGE_IDS` (specific paths/page ids set during earlier extractor testing this session) were also cleared back to empty via `update_source_config()`, for the same reason — the app shouldn't keep silently pointed at test-session scope.
3. `SettingsPanel.jsx`'s "Save Changes" button is removed. Every field now saves itself the instant it's committed: a provider-mode radio fires immediately on `onChange`; every text input, textarea, and date field fires on `onBlur`, compared against a `lastSavedRef` snapshot so an unchanged blur (click in, click away) is a no-op rather than a wasted round-trip. The one exception is the embedding-model field, which still can't be silent — changing it strands every existing embedding — so its `onBlur` still opens the existing confirm-then-reset-and-reingest flow; declining reverts the input to its last-saved value rather than leaving a stray unsaved edit with no Save button left to undo it via. The "Browse…" folder picker has no natural blur event (the picked path lands in the textarea without the user ever focusing it), so it calls the same save helper explicitly right after updating the text.

**Alternatives considered**: debouncing every keystroke instead of saving on blur — rejected as needlessly chatty (a network round-trip mid-typing) for a local, single-user backend where blur-to-commit is a completely ordinary desktop-app pattern and adds no perceptible delay.

**Verified**: `uv run pytest` — `tests/test_config/test_settings.py` updated for the new default (`test_parses_the_committed_config_yaml`, `TestGetSettings::test_returns_both_halves`) and the reverted `watch_dirs` behavior (`test_watch_dirs_is_empty_when_unset` replacing the old default-folder tests); full suite passes. Frontend: `npm run lint`, `npm run build` both clean; manually verified in the running app that each field type (radio, text, textarea, date) persists across a page reload without ever clicking anything resembling "Save."

**Affects**: `config/settings.py`, `config/config.yaml`, `config/.env` (real values only, gitignored), `tests/test_config/test_settings.py`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`, `frontend/src/assets/icons/save-icon.svg` (removed, now unused).

---

## 2026-08-31 — Stable dev ports with fallbacks, backend and frontend kept in sync

**Context**: The backend had no reliable default port — `settings.env.fastapi_port` (8080) was documented but nothing actually read it; `uvicorn` was always started manually with an explicit `--port`. A real conflict was hit this session: an unrelated Airflow container occupying 8080 on every interface, forcing a hardcoded `// TEMP` override of `frontend/src/api/client.js`'s `API_BASE_URL` to a different port, which then had to be remembered and kept in sync by hand. See issue #71.

**Decision**: `api/main.py` gains a real entrypoint (`if __name__ == "__main__":`, run via `uv run python -m api.main` — module invocation, matching `scheduler/daily_batch.py`'s existing convention). `_select_port()` tries `settings.env.fastapi_port` first, then `_PORT_FALLBACKS = (8080, 8090, 8091)` in order (a plain TCP bind-and-release check via `socket`, no new dependency), and writes whichever port it actually bound to into `data/backend_port.txt` (gitignored, ephemeral — `data/` already is). `frontend/vite.config.js` reads that same file synchronously at config-load time and injects it as `import.meta.env.VITE_API_BASE_URL` via Vite's `define`, falling back to `8080` if the file doesn't exist yet (backend not started, or started the old way). `client.js`'s `API_BASE_URL` now reads that env var instead of a hardcoded literal — the two processes can never drift out of sync, since the frontend always discovers whatever port the backend actually chose rather than guessing.

The frontend's own serving port (5173) keeps Vite's existing built-in "try the next port if taken" behavior (`strictPort` stays at its `false` default) — explicit `server.port: 5173` just documents the intent rather than reinventing what Vite already does correctly.

**Verified**: `_select_port()`/`_is_port_free()` directly, live — correctly detected the real running backend's port (8081, taken) and fell back to 8080 (free) in a real test. Full suite (542 tests) passes.

**Affects**: `api/main.py`, `frontend/vite.config.js`, `frontend/src/api/client.js`.

---

## 2026-08-31 — Graph view: GitHub node color changed from near-black to blue

**Context**: `SOURCE_TYPE_COLORS.github` was `#16151a` — an almost-black color, presumably chosen to evoke GitHub's own octocat-black branding, but barely distinguishable from the dark theme's background for both node circles and the legend swatch. See issue #70.

**Decision**: changed to `#3b82f6` — reusing the app's existing `--info-blue` CSS token (`frontend/src/index.css`) rather than inventing a new one, for consistency with the rest of the palette. Distinct from the other five source colors already in use (`local_file` purple, `notion` red, `gmail` green, `calendar` amber, `browser_history` gray).

**Affects**: `frontend/src/components/GraphView.jsx`.

---

## 2026-08-31 — Graph view: truncate long node labels, full title on hover

**Context**: GitHub-sourced node titles (commit messages, PR/issue titles) run long and, rendered in full next to every node, overlap and clutter the layout with more than a handful of nodes nearby — see issue #69.

**Decision**: `GraphView.jsx`'s node label is truncated to 32 characters (`MAX_LABEL_LENGTH`, ellipsis appended) via a new `truncateLabel()` helper. The full, untruncated title is still available via a native SVG `<title>` element inside each node's `<g>` — a browser-native hover tooltip, no extra state, positioning, or custom tooltip component needed. Chosen over the other options considered in the issue (zoom-based visibility, dimming by zoom level) as the simplest fix that fully solves the immediate clutter problem; those remain reasonable follow-ups if 32 characters proves too aggressive or too lenient in practice.

**Affects**: `frontend/src/components/GraphView.jsx`.

---

## 2026-08-31 — Ingestion progress counters (GitHub/local files/Notion) and a Stop button

**Context**: The live `current_item` label (added earlier) only ever said *which source* was extracting (e.g. "Extracting github…") — during that source's single, long, blocking `extract()` call there was no sub-progress at all, so a long GitHub scan across many repos looked frozen. There was also no way to stop a run in progress short of manually killing the spawned `python -m scheduler.daily_batch` OS process and hand-patching the stuck `ingestion_runs` row — a real gap felt directly this session (see issues #72, #73). Full design in the approved plan (`cheeky-frolicking-cupcake.md`).

**Decision**: one mechanism serves both features. `extractors/base.py` gains an `OnProgress = Callable[[int, int | None, str], bool]` type alias — `(current, total, label) -> keep_going`. Every extractor's `extract_new_items()` gains an optional `on_progress: OnProgress | None = None` parameter for a uniform call shape; only `github.py` (per repo, `total=len(repos)`), `local_files.py` (per matching file, `total` from a cheap upfront filesystem walk), and `notion.py` (per root page, `total` only when scoped to `NOTION_PAGE_IDS`, else `None`) actually call it this round — `gmail.py`/`calendar.py`/`browser_history.py` accept-and-ignore it (tracked separately in issue #68). Returning `False` doubles as cooperative cancellation: `scheduler/daily_batch.py::_run()` passes a closure that writes the live label via `update_ingestion_run_current_item()` and returns `not is_cancellation_requested(conn, run_id)` — this is also checked once per source as a coarser boundary for the three extractors that don't call the callback at all. `storage/sqlite_store.py` gains a `cancel_requested INTEGER DEFAULT 0` column (migrated via the existing `_migrate_schema()` pattern), `request_ingestion_cancellation()`/`is_cancellation_requested()`, and a new `"cancelled"` `IngestionStatus` value — the cross-process signal works this way because the API process and the spawned batch subprocess only share the SQLite file, not memory (same reasoning as `current_item`'s own round-trip). A cancelled run keeps whatever was already processed and committed (no rollback, same as a crash or extractor failure) and does **not** advance the ingestion watermark. New `POST /api/ingest/cancel` (`agent/ingest_trigger.py::cancel_ingestion()`, a thin wrapper) takes the real `ingestion_runs.id` (`GET /api/sources/status`'s `last_run.run_id`) — not `POST /api/ingest/trigger`'s display-label return value, which is a different string. The frontend threads a second `dbRunId` field alongside the existing display `runId` through `App.jsx`'s `ingestionStatus` (only known once the first poll returns the matching row) for exactly this reason, and shows a "Stop" button in `SettingsPanel.jsx` next to "Run ingestion now" while `status === "running"`, gated behind the existing `useConfirm()` dialog.

**Alternatives considered**: a purely source-level percentage — rejected earlier (user's explicit call) since GitHub/Notion discover items as they stream, not from a known total, so a real item-level percentage isn't honestly computable; a repo/file/page counter is the honest granularity available. Checking cancellation only between sources (no per-extractor callback) — rejected because a single source's `extract()` call can legitimately run for many minutes on its own, which would make Stop feel unresponsive for exactly the sources most likely to need it (GitHub, Notion).

**Verified**: `uv run pytest` — new tests across `tests/test_extractors/{test_github,test_local_files,test_notion}.py` (on_progress called with correct total/label, `False` stops the loop early, no-callback is the default), `tests/test_storage/test_sqlite_store.py` (migration adds `cancel_requested` to a pre-existing table; request/check round-trip), `tests/test_scheduler/test_daily_batch.py` (cancelling mid-source keeps already-processed items and doesn't advance the watermark; cancelling before any source runs processes nothing), `tests/test_agent/test_ingest_trigger.py`, `tests/test_api/test_ingest_route.py` (new cancel endpoint, including an unknown `run_id` still acknowledging rather than erroring). Full suite passes; `black`/`ruff` clean; frontend `eslint`/`prettier`/`vite build` clean.

**Affects**: `extractors/base.py`, `extractors/github.py`, `extractors/local_files.py`, `extractors/notion.py`, `extractors/gmail.py`, `extractors/calendar.py`, `extractors/browser_history.py`, `storage/sqlite_store.py`, `scheduler/daily_batch.py`, `agent/ingest_trigger.py`, `api/schemas.py`, `api/routes/ingest.py`, `frontend/src/api/client.js`, `frontend/src/App.jsx`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`, and the corresponding test files.

---

## 2026-08-31 — Default local watch folder when none is configured

**Context**: `EnvSettings.watch_dirs` returned an empty list when `LOCAL_FILES_WATCH_DIRS` was unset — a fresh install (or a user who never touches Settings) silently ingests zero local files with no indication anything is missing. See issue #55 (originally scoped as a full first-run onboarding wizard; trimmed down to just this piece, with the wizard itself moved to #52 since its real value only shows up once there's an actual stranger-facing Docker install — see that issue's discussion).

**Decision**: added `config/settings.py::DEFAULT_WATCH_DIR` (`~/Documents/PersonalKnowledgeGraphAgent/watched`), and `EnvSettings.watch_dirs` now falls back to `[DEFAULT_WATCH_DIR]` (created on demand via `mkdir(parents=True, exist_ok=True)`) instead of `[]` when unset. This is a fallback for *reading*, not a persisted setting — nothing writes it to `.env` on its own. `GET /api/settings/sources` already returns `env.watch_dirs` as-is, so the Settings screen's watch-folder textarea now shows the default pre-filled instead of empty, for free; it only becomes an explicit, permanent choice once the user actually clicks Save.

**Side effect worth noting**: `agent/connection_check.py::_check_local_files()`'s `if not watch_dirs: return not_configured` branch is now effectively unreachable through the real `EnvSettings.watch_dirs` property (it always returns at least the default) — left in place as a defensive check on the function's own contract (still exercised directly by its unit tests via a faked `env`), not dead code to delete, since the function doesn't know or care how its `watch_dirs` argument was produced.

**Verified**: `uv run pytest` — new tests for the fallback value and the on-demand folder creation (using a monkeypatched `DEFAULT_WATCH_DIR` pointed at `tmp_path`, not the real home directory); full suite passes except the two pre-existing, unrelated `provider_mode` failures.

**Affects**: `config/settings.py`, `tests/test_config/test_settings.py`.

---

## 2026-08-31 — Notion subpages extracted as their own items, not merged into the parent

**Context**: Asked directly whether the Notion extractor pulls in content from subpages nested under a configured page. It did, but only by accident and only partially: `_collect_block_text()` recursed into any block with `has_children`, including `child_page` blocks — folding a subpage's body text into the *parent's* single item. Two real bugs fell out of that: (1) a subpage's own title was silently dropped, since `child_page` blocks store their title under a `child_page.title` key, not the `rich_text` key every other block type uses (`_block_to_text()` only ever read `rich_text`); (2) incremental re-ingestion could miss a subpage edit entirely — `since` filtering only checked the *parent* page's own `last_edited_time`, and editing a subpage's content doesn't bump its parent's timestamp in Notion.

**Decision**: `_collect_block_text()` now intercepts `child_page` blocks instead of recursing into them for text — each becomes its own `ExtractedItem`, extracted by `_extract_page_and_subpages()` recursing via `client.pages.retrieve()` (own id, title, url, `last_edited_time`, `created_time`, own `since` check, own further-nested subpages). Critically, a page's blocks are walked to discover subpages *even when the page itself fails its own `since` check* — otherwise a subpage nested under an unchanged parent could never be discovered at all once ingestion is scoped to specific page ids (`NOTION_PAGE_IDS`), since a subpage isn't independently listed anywhere else. A `seen_page_ids` set dedupes a page reachable more than one way (in unscoped/workspace mode, every page is visited both directly via `search()` *and* again as a subpage discovered while walking another page's blocks).

**Trade-off accepted**: this makes every incremental run walk every configured (or workspace) page's block tree regardless of whether that page itself changed, since subpage discovery has no cheaper path — previously an unchanged page's `since` check short-circuited before any block-fetching API call at all. Acceptable for a single-user workspace of the scale this project targets; revisit if it becomes a real cost at much larger scope.

**Verified**: `uv run pytest tests/test_extractors/test_notion.py` — 5 new tests (subpage becomes its own item with correct title/url, arbitrarily deep nesting, a subpage surfaces even when its unchanged parent is correctly excluded, an unfetchable subpage is skipped not raised, a page reachable as both a root and a subpage isn't duplicated) plus all 15 pre-existing tests pass unchanged.

**Affects**: `extractors/notion.py`, `tests/test_extractors/test_notion.py`.

---

## 2026-08-31 — Graph zoom/pan/filter/highlight

**Context**: The relationship graph had no way to filter by source, zoom, or focus on one node's neighborhood, making it hard to use with more than a handful of nodes.

**Decision — graph pan/zoom**: Hand-rolled (`{x, y, k}` transform state on a wrapping `<g>`, wheel handler + toolbar buttons + background-drag panning), not `d3-zoom`/`d3-selection` — the existing node-dragging code already hand-rolls pointer-event math (`toSimulationPoint()`) rather than using `d3-drag`, so this follows the same style rather than mixing two different interaction paradigms and adding a dependency pair.

**Decision — click-to-highlight**: A node click (not drag — distinguished via screen-pixel movement between pointerdown/pointerup, threshold 4px) sets `selectedNodeId`; the selected node, its one-hop neighbors, and the connecting edges render at full opacity, everything else dimmed to `0.15`. Clicking empty space or the same node again clears the selection.

**Decision — per-source filtering**: The existing legend doubles as the filter control (clicking a swatch toggles that source type) rather than a separate toolbar, since it already lists every real source type in one place. Filtering hides (not dims) non-matching nodes/edges, and is render-only — the underlying simulation never restarts or drops nodes, so toggling a filter back on doesn't reflow the layout or lose position continuity.

**A real pre-existing bug fixed along the way**: `SOURCE_TYPE_COLORS` used the key `"google_calendar"`, but every extractor's actual `SOURCE_TYPE` constant (and therefore every real node's `source_type`) is `"calendar"` (see `extractors/calendar.py`). Calendar nodes were silently falling back to the generic gray color, and the legend entry never matched anything. Found while wiring up per-source filtering, which depends on this key actually matching a real value — fixed to `"calendar"`.

**Decision — auto-fit**: `fitToView()` computes the bounding box of the currently-*visible* (post-filter) node positions and sets the transform to center and fully contain it, clamped to the same zoom range as manual zoom. Triggered automatically once per graph load (via `forceSimulation`'s `"end"` event, guarded by a ref so it never fires twice for the same load) and again whenever the filter set changes (the visible bbox changed), plus a manual "Fit view" button for the user to re-trigger any time.

**Affects**: `frontend/src/components/GraphView.jsx`, `frontend/src/index.css`.

---

## 2026-08-31 — Live "what's being ingested" label

**Context**: Watching a real ingestion run showed a gap: `items_processed` already updated live (2026-08-30 entry, below), but during a single source's *extraction* phase — GitHub's took ~20 minutes across 65 repos — there was no feedback at all, since extraction runs to completion before any item is countable.

**Decision**: `ingestion_runs` gets a new nullable `current_item TEXT` column. Since `storage/sqlite_store.py::connect()` only ever runs `CREATE TABLE IF NOT EXISTS` — never migrates an existing table — and the real, live `data/pkg_agent.db` already has this table with real rows, added a small `_migrate_schema()` step (`PRAGMA table_info`, `ALTER TABLE ... ADD COLUMN` if missing) run once per `connect()`. This is the project's first case of altering an already-populated table; noted here as the pattern to follow if another such column is ever needed, rather than building a versioned migration system for a single-user local app. `scheduler/daily_batch.py::_run()` sets it twice: once per source, right before `extract()` runs (`"Extracting {source_name}…"` — the previously-invisible phase), and once per item alongside the existing `update_ingestion_run_progress()` call (`"{source_name}: {item.title}"`). Both are best-effort (logged, never abort the run), same resilience pattern as the existing progress-update call. `LastRunResponse`/`GET /api/sources/status` exposes it; the frontend's existing 4s poll loop (`App.jsx::pollIngestionCompletion()`) already carries it for free — `SettingsPanel.jsx` renders it as a line under the progress bar, only while a run is `"running"`.

**Affects**: `storage/sqlite_store.py`, `scheduler/daily_batch.py`, `api/schemas.py`, `api/routes/sources.py`, `frontend/src/App.jsx`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`, `tests/test_storage/test_sqlite_store.py`, `tests/test_scheduler/test_daily_batch.py`, `tests/test_api/test_sources_route.py`.

---

## 2026-08-30 — Google Calendar extractor: server-side recurrence collapsing, a configurable noise rule, and dropped low-signal fields

**Context**: Building the Google Calendar extractor (`docs/Data_Extraction_Specification.docx` section 7 and `docs/Technical_Design_Document.docx` section 3.4) — the last of the three previously-unbuilt sources. Auth follows the exact same one-time-OAuth-then-cached-token pattern as `extractors/gmail.py` (a personal calendar has no service-account option either) — see that module's DECISIONS.md entry for the full reasoning; not repeated here. Separate credentials/token files from Gmail (`GOOGLE_CALENDAR_CREDENTIALS_PATH` / `data/calendar_token.json`), matching their already-separate `.env` variables — no assumption that one Google Cloud OAuth app necessarily covers both scopes.

**Recurring events collapse to one item per series, for free, server-side**: the spec requires "recurrence pattern stored once, not per instance." Google's `events().list()` API has a `singleEvents` parameter — `false` (the default) returns each recurring series as a single event carrying a `recurrence` rule; `true` expands it into one entry per occurrence. Simply never passing `singleEvents=True` gets the required behavior directly from the API, with no client-side deduplication logic needed at all.

**"Filters out pure recurring noise" made into a real, configurable rule**, not a hardcoded heuristic: added `filters.calendar.skip_recurring_without_description` to `config.yaml`/`config/settings.py` (default `true`, mirroring the `browser_history`/`gmail` per-source filter sections already established there) — a recurring event with no description is almost always a standing reminder ("daily standup," "take vitamins"), not a real meeting worth ingesting, while a recurring event *with* a description ("Weekly 1:1 — discuss roadmap") is genuinely meaningful and kept regardless of its recurrence.

**Location and color/category tag are dropped entirely, not stored anywhere**: same limitation already hit for Gmail's thread id — `ExtractedItem` has no generic metadata field, and the spec itself marks both "metadata only, not embedded" (location) or "lightweight" (color tag), so neither is worth the complexity of inventing somewhere to put it for genuinely low signal.

**Title/time/attendees are still folded into `raw_text` alongside the description**, not description-only: the spec frames description as "the main embedded text" and title as "mainly...metadata," but *most personal calendar events have no description at all* — a strict "embed only the description" reading would produce an empty, filtered-out item for the common case (e.g. "Dentist appointment," blank description). Time and attendees are also genuinely useful signals in their own right (timeline correlation; "who a meeting was with" — both explicitly called out elsewhere in the design docs as valuable relationship-detection inputs), so all three are included as short context lines around the description rather than reserved as inert metadata with nowhere to live.

**Verified**: `uv run pytest` — new extractor tests (`tests/test_extractors/test_calendar.py`, fake Calendar service objects covering basic extraction, cancelled-event skipping, all-day-event time anchoring, and the recurring-event noise filter both on and off) and updated connection-check tests all pass; **full suite passes with zero failures** (this branch's `config.yaml` currently matches its own committed default exactly — the usual two pre-existing failures from the user's real, differing `provider_mode` choice only reappear once that live override is reapplied after this commit, same as every other extractor branch). `black`/`ruff` clean (reuses the `D107`/`N802`/`N803` test per-file-ignores added on the Gmail branch, reapplied here independently since these are separate branches off `main`).

**Affects**: `extractors/calendar.py` (new), `config/settings.py`, `config/config.yaml`, `agent/connection_check.py`, `scheduler/daily_batch.py`, `pyproject.toml` (new dependencies + ruff per-file-ignores), `tests/test_extractors/test_calendar.py` (new), `tests/test_agent/test_connection_check.py`, `tests/test_api/test_sources_route.py`

---

## 2026-08-30 — Gmail/GitHub ingestion date-range scoping

**Context**: Gmail and GitHub can pull a large amount of history on first backfill (every message ever, every commit/PR/issue across every accessible repo). Wanted a way to bound that from the Settings screen — both an explicit start/end date range, and (for GitHub, which already supported it at the extractor level via `GITHUB_REPOS` but had no UI/API path to set it) a specific list of repos.

**Decision**: Four new `EnvSettings` fields — `gmail_date_range_start/end`, `github_date_range_start/end` — typed `date | None`, living in `config/.env` alongside `LOCAL_FILES_WATCH_DIRS`/`NOTION_PAGE_IDS`/`GITHUB_REPOS` (the existing precedent for non-secret *scope* settings, as opposed to `config.yaml`'s behavioral settings). Semantics: a start date is a **floor** — `effective_since = max(incremental_since, range_start)` — so it also naturally caps the very first backfill without disabling the normal incremental cursor on later runs. An end date is a **ceiling** applied on every run, not just the first — a real fixed window, since the ask was an explicit start/end range, not just a one-time backfill cap.

`update_source_config()`'s date parameters are plain `str | None` (ISO `"YYYY-MM-DD"` or `""` to clear), not `date | None` — this matches an HTML `<input type="date">`'s value directly (always a string, empty when cleared), so no parsing boundary is needed between the API layer and `.env`. `EnvSettings` itself does the real `date` parsing (pydantic-settings coerces the env string automatically), so extractors get real `date`/`datetime` objects.

**Alternatives considered**: Storing the range in `config.yaml` under `filters` — rejected for consistency: `GITHUB_REPOS` and `NOTION_PAGE_IDS` (identical "scope, not behavior" character) already live in `.env` and are already edited via `update_source_config()`/`PUT /api/settings/sources`; splitting date ranges into `config.yaml` would mean two different edit paths for what's conceptually the same kind of setting.

**Affects**: `config/settings.py` (`EnvSettings`, `update_source_config`), `api/schemas.py`, `api/routes/settings.py`, `extractors/gmail.py`, `extractors/github.py`, `frontend/src/components/SettingsPanel.jsx`.

---

## 2026-08-30 — Ingestion "Started at" timestamp now persists like "Last checked" does

**Context**: `SettingsPanel.jsx`'s "Started at HH:MM:SS" line only ever read `ingestionStatus` (an `App.jsx`-owned, session-only value set when ingestion is triggered/polled) — so a fresh page load, or opening Settings without having triggered a run yet this session, showed nothing at all, even though a real run happened recently. Reported directly: wanted it to "stick" the way "Last checked" (Reverify) already does — that one always shows a real value because it reads `connections.connections[0].checked_at`, fetched fresh on every mount, not session state.

**Decision**: Added a `displayedIngestion` value that prefers the live `ingestionStatus` when present (it's actively polling, and can show `status: "running"` mid-run — something a once-per-mount fetch can't distinguish from a stale row) and falls back to `sources.last_run` (already fetched on mount via `getSourcesStatus()`, backed by the real, persisted `ingestion_runs` table) otherwise. Also switched the timestamp from `toLocaleTimeString()` to `toLocaleString()` (full date, not just time) — a persisted last run could be from a previous day, not just "earlier today," so a bare time would be ambiguous.

**Verified**: real `GET /api/sources/status` against the live backend returns a real `last_run` (`started_at`, `status: "success"`, `items_processed: 108`) from a prior session; confirmed the fallback path renders it.

**Affects**: `frontend/src/components/SettingsPanel.jsx`

---

## 2026-08-30 — Removed the model fields' decorative dropdown chevron

**Context**: The Settings redesign entry above ("screen 2 of 3") added a chevron icon to the three model fields, reasoning at the time that it was "visual only" and harmless since it just matched the design's "Options" box styling. Reported directly as confusing: it reads as a real `<select>` dropdown, but these are (correctly, deliberately) plain free-text `<input>`s — model identifiers are arbitrary strings (any Ollama tag, any OpenRouter model slug), not a fixed enum, so a real dropdown would be wrong here. The chevron was misleading about what the field actually does, which is worse than "harmless."

**Decision**: Removed the chevron from all three model fields (local generation, cloud generation, embedding). The fields themselves are unchanged — still plain text inputs, same handlers, same validation-free behavior as before the redesign entirely.

**Affects**: `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`, `frontend/src/assets/icons/model-select-chevron.svg` (removed)

---

## 2026-08-30 — Sidebar nav icons weren't rendering: switched CSS mask-image for inline SVG

**Context**: The Chat redesign's sidebar nav icons (Chat/Graph/Settings) used `<span>` + CSS `mask-image` (recoloring one exported Figma asset via `background-color: currentColor`, since each icon needed to render in either a muted or active-purple color depending on the current view — see the earlier "Figma redesign, screen 1" entry). Reported directly as not showing up at all.

**Decision**: Replaced with inline SVG React components (`components/icons/NavIcons.jsx`) — the exact same path geometry as the exported assets, `fill="currentColor"` on the `<path>`, matching how this app's original (pre-redesign) stroke icons were themed. This is the same pattern already used elsewhere in the app (`Sidebar.jsx`'s pre-redesign Graph/Settings icons, before this redesign existed at all) and removes any dependency on `mask-image` support/behavior entirely — a plain colored SVG has no browser-compatibility question the way a masked element might. The three now-unused exported SVG files (`nav-chat.svg`, `nav-graph.svg`, `nav-settings.svg`) were deleted; every other icon added during the redesign (chat suggestion chips, settings buttons, etc.) is still a plain `<img>` since those only ever need one fixed color, not a state-dependent recolor — the mask/inline-SVG question only applied here.

**Verified**: `eslint` (zero warnings), production `vite build` (succeeds, three fewer bundled SVG assets).

**Affects**: `frontend/src/components/Sidebar.jsx`, `frontend/src/components/icons/NavIcons.jsx` (new), `frontend/src/index.css`, `frontend/src/assets/icons/nav-{chat,graph,settings}.svg` (removed)

---

## 2026-08-30 — Figma redesign, screen 2 of 3: Settings

**Context**: Requested directly, same Figma file as the Chat redesign — restyle only, functionality unchanged. Branched off `feat/redesign-chat-screen` (not `main` or `feat/relationship-graph-view`) since this screen depends directly on that branch's token palette, Geist font, and icon-asset foundation.

**Two explicit columns, not the earlier multi-column masonry**: the Chat-branch two-column Settings layout (`column-count: 2`, self-balancing by height) predates this redesign and was a reasonable approximation without a real design to match. The actual Figma design groups *specific* sections into *specific* sides — Generation Provider, Models, and Save Changes on the left ("Core Engine Settings"); Local folders, Notion scope, Data Ingestion, and Danger Zone on the right ("Data Sources & Scope") — not just "whichever column is shorter." Replaced with a real two-column CSS Grid and two explicit `.settings-column` containers in the JSX, matching that grouping exactly.

**Data Ingestion and Connected Data Sources merged into one card**: these were two separate `.settings-section` boxes; the design shows them as one card with an internal horizontal divider (a "Run ingestion now" row, then a bordered-off "Connected Data Sources" subsection with its own "Reverify" control). Merged the markup into one `<section>` with an inner `settings-section-header-bordered` divider — no change to either action's handler, state, or behavior, only where the DOM boundary between them falls.

**Save Changes moved into the left column, no longer full-width at the page bottom**: purely a placement change (still the exact same `handleSave()` diff-and-confirm flow) — the design places it directly under the Models card, not spanning the whole page under both columns.

**Radio option restructured as a single `<label>`, not input+label siblings**: needed to swap in a custom purple-dot/checkmark visual instead of the browser's native radio appearance (per the design), which meant visually hiding the native `<input>`. First pass hid the input but left the new dot visual as a sibling *outside* the `<label>`, which regressed a real behavior — clicking the dot didn't toggle the radio, only clicking the text did, unlike the original where clicking the native radio circle itself always worked. Fixed by making `.radio-option` itself the `<label>`, wrapping the (visually hidden) input, the dot, and the text together, so the whole row is one click target again, same as before — see "keep functionality the same" in the request.

**Decorative icons throughout, all exact exported Figma assets** (per the same asset-fidelity approach as the Chat redesign — see the earlier "Figma redesign, screen 1" entry): a checkmark inside the selected radio dot, a chevron on each model field (the fields are still plain free-text `<input>`s, not real dropdowns — the chevron is visual only, matching the design's own "Options" box styling, since model identifiers are arbitrary strings, not a fixed enum, so a real `<select>` would be wrong here), and small icons on the Save/Run-ingestion/Reverify buttons and the Danger Zone heading.

**Skipped the design's footer text** ("Knowledge Agent v2.4.1 • Core Engine Active"): a fabricated version number and status string with no corresponding real data anywhere in the app — adding it would misleadingly imply real version/health tracking that doesn't exist. Omitted rather than inventing fake values to fill it.

**Verified**: `eslint` (zero warnings), production `vite build` (succeeds), and the live dev server against the live backend — every existing Settings behavior (provider/model diffing and confirm-before-save, the embedding-model-change reset+re-ingest flow, Browse…, live ingestion progress, Reverify, Reset all data) exercised unchanged, only the visuals differ.

**Affects**: `frontend/src/index.css`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/assets/icons/*.svg` (6 new files: radio-check, model-select-chevron, save, ingest, reverify, danger)

---

## 2026-08-30 — Figma redesign, screen 1 of 3: Chat interface

**Context**: Requested directly — a Figma file (`H5lylGQQm8W8mo7PdL3Syr`, "Untitled") containing a full visual redesign of all three existing screens (Chat, Graph, Settings): ambient blurred-glow backgrounds, translucent/backdrop-blur "glass" cards, a purple accent palette, the Geist typeface. Confirmed with the user up front: roll out one screen/branch/PR at a time starting with Chat, restyle only (no functional/behavioral changes beyond what the design itself implies), dark-only (the design has no light variant, so inventing one would be guessing), and source the Geist font from the official `geist` npm package rather than a Google Fonts CDN link — this is a local-first app with no other runtime network dependency, and self-hosting keeps it that way.

**Branched off `feat/relationship-graph-view`, not `main`**: that PR (#59) is still open with substantial work not yet merged (the graph view itself, live ingestion progress, two-column Settings, several real-data bug fixes) — branching off `main` would have silently dropped all of it from the redesign's starting point. Caught and corrected before any real work landed on the wrong base (see the branch-juggling in this session's own history) — worth calling out since it's an easy mistake when "branch off latest" is the usual reflex but the actual base for "everything this app currently does" is the open PR, not `main`.

**Token system: replaced, not layered**: rather than inventing a parallel set of "redesign" CSS variables alongside the existing light/adaptive-dark ones (`--text`, `--bg`, `--accent`, etc. in `index.css`), the existing token *names* were kept and their *values* replaced outright with the Figma dark palette, and the `prefers-color-scheme: dark` media query block was deleted entirely (dark-only, decided above). Every component — including Graph and Settings, not yet individually redesigned — already read color exclusively through these tokens, so this one change immediately reads as "further along" everywhere rather than leaving two competing token systems to reconcile later. One real regression this surfaced: `.toast`'s `background: var(--bg)` became the exact same near-black as the page background it floats over, making toasts nearly invisible — fixed by switching it to the same translucent-card-plus-blur treatment (`--bg-elevated` + `backdrop-filter`) already established for the redesign's other cards. Left `.model-select`/`.source-scope-textarea`/`.reverify-button` (Settings, out of scope for this branch) alone — their subtle-border-on-flat-background look is no more (or less) visible than it was under the old light theme, so it isn't a regression, just not yet restyled.

**Icons: exact exported assets, recolored via CSS mask, not two separate Figma exports**: Figma only exports one fixed fill color per icon instance, matching whatever state that instance happens to show in the design (e.g. the Chat nav icon is only shown in its active, purple state). The three nav icons (Chat/Graph/Settings) each need to render in either a muted or an active-purple color depending on which screen is currently open, so each is applied via `mask-image` (the exact exported SVG's shape, unmodified) with `background-color: currentColor` supplying the color from CSS — the same general technique the app's previous inline stroke SVGs already used via `currentColor`. This avoids re-exporting a second color variant per icon from Figma, and the geometry rendered is still the literal exported asset, not a redrawn approximation.

**Added a dedicated "Chat" nav tab**: the design shows three primary nav links (Chat/Graph/Settings); the app previously had no way to return to the *current* chat view without either starting a new conversation ("New chat") or reopening a specific past session from the sidebar list. Wired `Sidebar.jsx`'s new "Chat" link to a new `onOpenChat` prop (`() => setView("chat")` in `App.jsx`) that only switches the view, touching neither `sessionId` nor `chatKey` — purely additive, nothing existing was removed or changed.

**Suggestion chips are real, not decorative**: the design's two example prompts ("Summarize: recent project commits and emails", "Find: that presentation from last week") are rendered as clickable buttons that fill the input with a related prompt (not send it immediately, so it can still be edited) rather than as static illustration.

**Data Source Indicators are real, not a static two-pill mockup**: the design shows exactly two example pills (Notion, Gmail). Hardcoding those two would become actively misleading the moment a different set of sources is actually connected, so `ChatWindow.jsx` fetches `getSourceConnections()` once on mount and renders one pill per source whose live status is `"ok"`, using a small shared `frontend/src/constants/sourceColors.js` color map (kept separate from `GraphView.jsx`'s own `SOURCE_TYPE_COLORS` for now — that screen isn't redesigned yet and shouldn't visually shift as a side effect of this branch).

**Verified**: `eslint` (zero warnings), production `vite build` (succeeds, Geist font and icon assets bundle correctly), and the live dev server against the live backend (real session list, real source-connection status feeding the indicator pills). `prettier --check` flags several files this branch never touched (pre-existing CRLF/line-ending drift, confirmed via `git diff --stat` showing them unmodified) — not a regression from this work.

**Affects**: `frontend/src/index.css`, `frontend/src/App.jsx`, `frontend/src/components/Sidebar.jsx`, `frontend/src/components/ChatWindow.jsx`, `frontend/src/constants/sourceColors.js` (new), `frontend/src/assets/fonts/Geist-Variable.woff2` (new), `frontend/src/assets/icons/*.svg` (new, 7 files), `frontend/package.json`/`package-lock.json` (added `geist`)

---

## 2026-08-30 — Four UI fixes: pending-bubble wrap, full-screen graph, scroll-to-bottom, two-column Settings

**Context**: Requested directly, four separate small UI issues from actually using the app.

**"Thinking…" wrapping character-by-character, then overflowing its box**: `.message-bubble` sits in a `display: flex` row and has `overflow-wrap: break-word`. A flex item with that combination has a very small automatic minimum width (browsers compute it as though the text could wrap at any character), so the short one-word pending placeholder could shrink to a sliver and wrap mid-word instead of sizing to its own content. First fix (`white-space: nowrap` alone) stopped the wrapping but the text still visibly spilled past the box's right edge — `.message-bubble` isn't itself the flex item (`MessageBubble.jsx` wraps it in a plain, unstyled `<div>` that is), so the bubble's own box wasn't reliably shrink-wrapped to its content either. Fixed properly with `display: inline-block` (sizes the bubble to its own content directly, independent of that ambiguity) plus `white-space: nowrap`, scoped to `.message-bubble.pending` only — real (potentially long) agent answers still wrap normally. Also dropped `font-style: italic` from the pending state: a slanted glyph's ink extends past the upright metrics its box was sized from, so even with the box now correctly sized, the last character or two still visibly overhung the padding — muted color alone still reads as "pending" without that overhang.

**Graph view not using the full screen**: `GraphView.jsx` used a fixed `WIDTH=900, HEIGHT=600` for both the SVG `viewBox` and the `d3-force` simulation's center, with the SVG capped at `max-width: 900px` and centered — so on any wider screen there was dead space around a small, fixed-size graph. Replaced with a `ResizeObserver` on the actual canvas element; its measured size now drives both the SVG `viewBox` and the simulation's `forceCenter`, updated on every resize. The simulation itself isn't recreated on resize (that would snap every node back to its start position) — a separate effect just nudges the existing simulation's center force (`alpha(0.3)`, not `.restart()`) so nodes drift toward the new center instead of jumping.

**No way to jump back to the latest message**: `ChatWindow.jsx`'s message list never auto-scrolled and had no way to jump to the bottom once you'd scrolled up. Added a floating "↓ Latest" button, shown once the reader scrolls more than 48px from the bottom, plus auto-scroll-to-newest when a new message arrives *while already at the bottom* (a new question, or the pending bubble resolving into a real answer) — someone who scrolled up to reread something earlier doesn't get yanked back down by an unrelated answer streaming in. The "was at the bottom" check reads a ref updated continuously by the scroll handler, not a fresh measurement taken after the new message is already in the DOM — measuring after the fact would confuse "distance from bottom" with the new message's own height (a long answer would then wrongly look like "the reader had scrolled away" even when they hadn't).

**Settings page using the whole screen looked bad**: A single full-width column meant every section's text/inputs stretched edge-to-edge on a wide screen. Wrapped the settings sections in a CSS multi-column container (`column-count: 2`, `break-inside: avoid` on each section, collapsing to one column under 900px) rather than a grid — sections have uneven heights, and multi-column self-balances content across the two columns instead of pairing sections up strictly row-by-row the way a two-column grid would. The page heading and the final Save button/status stay outside the column container, full width.

**Affects**: `frontend/src/index.css`, `frontend/src/components/GraphView.jsx`, `frontend/src/components/ChatWindow.jsx`, `frontend/src/components/SettingsPanel.jsx`

---

## 2026-08-30 — Graph legend omits `browser_history`

**Context**: Requested directly after noticing browser history never appears on the relationship graph. Verified this is by design, not a bug: `pipeline/relationships.py` excludes `browser_history` from relationship detection entirely, in both directions (as source and as candidate — see the module's `_NO_RELATIONSHIP_DETECTION_SOURCE` filtering, and `CLAUDE.md`'s locked-in decision that browser history gets no relationship detection). `storage/neo4j_store.py::get_full_graph()`'s own docstring confirms a node only exists once it has at least one confirmed relationship — so a `browser_history` node can never exist on this graph, structurally, not just in practice.

**Decision**: `GraphView.jsx`'s legend now filters `browser_history` out of the `SOURCE_TYPE_COLORS` entries it renders, rather than listing a source type that can never actually show up on the graph — showing it would incorrectly suggest browser history items are just absent for now (e.g. not yet ingested) rather than structurally excluded. The color mapping itself is left in place (still used as the node-fill lookup, and cheap to keep around even though that branch can never be hit for this source type).

**Affects**: `frontend/src/components/GraphView.jsx`

---

## 2026-08-30 — `reset_all()`'s Chroma reset never actually cleared the dimension lock

**Context**: While verifying the previous entry's fix live (real reset via `POST /api/admin/reset`, then a real ingestion run), the run failed again with the exact same `Collection expecting embedding with dimension of 384, got 1536` error — on the very first item, right after a confirmed reset. So the earlier "fix" (reset, documented below) had not actually fixed anything; it only looked like it had because the run happened to be re-checked via `GET /api/sources/status` returning zero, which reset does guarantee (SQLite/Neo4j/Chroma document counts all go to zero) — but zero *document count* is not the same as a cleared *dimension lock*.

**Root cause**: `storage/chroma_store.py::reset_all()` fetched every document id and called `collection.delete(ids=...)` — clearing every stored vector, but leaving the collection's underlying HNSW index (and the embedding dimension it locked in on its very first write, ever) completely untouched. Confirmed directly: the collection's on-disk index directory was unchanged across the reset, and a controlled reproduction (upsert at dim 3, `reset_all()`, upsert at dim 10) failed with the same "expecting dimension" error even though `collection.count()` read 0 in between.

**Decision**: `reset_all()` now deletes and recreates the whole collection (`client.delete_collection()` + `client.get_or_create_collection()`) instead of deleting its documents — matching how `get_collection()` creates it in the first place, so a fresh collection has no dimension locked in until the next write. This changes its signature: it now takes a `persist_dir` (defaulting to `settings.env.chroma_persist_dir`, same convention as `get_collection()`) and *returns* the newly created `Collection`, rather than taking an already-open one and mutating it in place — deleting a collection invalidates any `Collection` object a caller already held, so there is no way to "reset in place" safely; every caller holding a long-lived reference must swap it for the return value. `agent/admin.py::reset_all_data()` and `api/routes/admin.py` were updated accordingly: the route now replaces `app.state.collection` with the fresh one after a successful reset — otherwise every later request in that same process (query, ingestion, another reset) would keep using the stale, already-deleted collection object. `api/main.py`'s lifespan now also stores `app.state.chroma_persist_dir` alongside the collection itself, so the route can reset against the exact directory this process opened from without re-deriving it from global settings (which would break test isolation — tests point Chroma at an isolated `tmp_path`, not the real settings-derived directory).

**Verified against real data**: real reset via the live backend; direct Python check against the real persist directory confirmed a 1536-dim write succeeds immediately after (previously failed identically to the original bug report); a real ingestion run's first `local_file` items embedded successfully (3/3, `status: "ok"`) where the prior run had failed on its very first item. New regression test (`test_clears_a_dimension_lock_left_by_the_previous_reset_approach`) reproduces the exact scenario. Full backend suite passes except the same two pre-existing, unrelated failures noted in the entries below.

**Affects**: `storage/chroma_store.py`, `agent/admin.py`, `api/main.py`, `api/routes/admin.py`, `tests/test_storage/test_chroma_store.py`, `tests/test_agent/test_admin.py`, `tests/test_api/conftest.py`, `tests/test_api/test_admin_route.py`

---

## 2026-08-30 — Two real, live bugs found while investigating a stuck ingestion run: Chroma dimension lock, and a trailing-backslash `.env` corruption

**Context**: "Check the embedding mechanism — ingestion started 3:37, still not done at 3:51." Investigated directly against the real `ingestion_runs` table and found two separate, real, currently-live bugs — not a hang.

**Bug 1 — Chroma collection locked to the old embedding dimension**: The most recent real run had `items_processed: 0`, `status: failed`, with every single item across every source failing identically: `Collection expecting embedding with dimension of 384, got 1536`. Root cause: Chroma locks a collection to whatever dimensionality its first-ever write used. The real collection had vectors from before the `dimensions=384` truncation was removed (see the "cloud-only embedding" entries above) — so every new write, now producing native 1536-dim vectors, was rejected outright. This is exactly the scenario the "reset before switching embedding models" guidance was built for, except here the trigger wasn't a user-initiated model change — it was *my own* earlier code change silently leaving old, now-incompatible vectors behind. Fixed live: reset all data via the real `POST /api/admin/reset` (confirmed with the user first), verified via `GET /api/sources/status` returning to a clean zero state.

**Hardening**: `storage/chroma_store.py::upsert_chunks()` now detects this specific Chroma error (`"expecting embedding with dimension" in str(exc)`) and raises a `VectorStoreError` naming the actual fix ("Reset all data", then re-ingest) instead of Chroma's generic message — so a future recurrence (e.g. a user changes `cloud_embedding_model` to a different-dimension model without following the reset prompt) fails with an actionable error instead of a wall of identical per-item failures.

**Bug 2 — a trailing backslash in a watched folder path silently corrupted the rest of `.env`**: Separately, found the real `config/.env` was only parsing 6 of its ~20 keys — `dotenv_values()` reported `"could not parse statement starting at line 23"`, and everything from that line onward, including `OPENROUTER_API_KEY`, was silently dropped. Root cause: `LOCAL_FILES_WATCH_DIRS` had been written as `'D:\...\Agent\'` — a path ending in a bare backslash. `python-dotenv`'s writer wraps values in single quotes and only escapes literal `'` characters, not backslashes, so the trailing `\` lands immediately before the closing quote; its *reader* then treats `\'` as an escaped quote rather than the string terminating, and fails to parse that statement — cascading to drop every line after it too, verified directly (`dotenv_values()` went from 6 keys parsed with the bad file to the full 18 once fixed). This explains why the "stuck" run's actual failures included routine `browser_history`/`notion` items too, not just the new `docs` files, and — more seriously — meant the real `OPENROUTER_API_KEY` silently stopped loading at all, breaking every embedding and cloud-generation call regardless of the dimension bug above.

**Decision**: `update_source_config()` now strips a trailing `\`/`/` from every watch-folder path before writing (`d.rstrip("\\/")`) — a trailing separator is semantically meaningless for a directory path, so this is always safe, and it removes the entire class of corruption at the source rather than only detecting it after the fact. Fixed the live `config/.env` directly (removed the trailing backslash, restored the correct `...\docs` path) and confirmed via `dotenv_values()` that all 18 keys, including the API key, parse again.

**Also added — live ingestion progress, not just a start/stop signal**: Requested directly, alongside a persistent "when did this start" timestamp (the toast alone disappears after 7s). `storage/sqlite_store.py::update_ingestion_run_progress()` is called once per item inside `scheduler/daily_batch.py::_run()`'s processing loop (not just once at the very end via `complete_ingestion_run()`), so `ingestion_runs.items_processed` updates live while a run is still `"running"`. `LastRunResponse` (`GET /api/sources/status`) now includes `items_processed` at the top level, so the frontend's existing polling loop (`App.jsx::pollIngestionCompletion()`, already running every 4s) picks it up for free. `App.jsx` now tracks `ingestionStatus` (`{startedAt, runId, itemsProcessed, status}`) across the whole session — set on trigger, updated on every poll — and passes it to `SettingsPanel.jsx`, which renders it persistently (survives the toast's 7s auto-dismiss, and navigating away and back) as a "Started at HH:MM:SS" line plus a progress bar. The bar is deliberately **indeterminate** (a sliding segment, CSS-only animation) while `status === "running"`, not a fabricated percentage — the real total item count isn't known upfront (extraction discovers items as it streams through each source, especially Notion's paginated full-workspace scan), so a fake percentage would be dishonest; the real, live `items_processed` count is shown as text alongside it instead. Once the run finishes, the bar fills solid green (success) or red (partial failure/failed).

**Verified against real data**: real reset via the live backend, confirmed `GET /api/sources/status` returned all-zero; real `.env` fix confirmed via `dotenv_values()`; new tests for the dimension-mismatch error message, the trailing-backslash corruption (asserting the API key specifically survives), and live progress updates (`GET /api/sources/status` reflecting a `"running"` run's `items_processed` before it completes) all pass; full backend suite passes except the same two pre-existing, unrelated failures noted in the retry-fix entry above (the user's own live `provider_mode: fully_cloud` choice).

**Affects**: `storage/chroma_store.py`, `config/settings.py`, `storage/sqlite_store.py`, `scheduler/daily_batch.py`, `api/schemas.py`, `api/routes/sources.py`, `frontend/src/App.jsx`, `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`, `config/.env` (live fix, not committed — gitignored), `tests/test_storage/test_chroma_store.py`, `tests/test_config/test_settings.py`, `tests/test_storage/test_sqlite_store.py`, `tests/test_api/test_sources_route.py`

---

## 2026-08-30 — Retry on transient Windows `PermissionError` when writing `.env`/`config.yaml`

**Context**: A real, reported error while changing the watch folder through Settings: `Could not write config\.env: [WinError 5] Access is denied: '.tmp_5vxf83gc' -> '.env'`. Root-caused directly: `python-dotenv`'s `set_key()` writes a temp file (`.tmp_...`) next to `.env` and atomically `os.replace()`s it over the real file — a well-known Windows failure mode is that this rename gets transiently denied if another process (a real-time antivirus scanner, the search indexer, an editor's file-watcher) has briefly opened the destination file, even just for reading. Confirmed transient, not deterministic: the exact same write succeeded immediately when retried by hand, both called directly and through the real running API, seconds after the reported failure.

**Decision**: Added `config/settings.py::_retry_on_transient_permission_error()` — retries a zero-argument write callable up to 5 times with a short, linearly-increasing backoff (0.1s, 0.2s, 0.3s, 0.4s) specifically on `PermissionError` (not any other `OSError` — a genuinely-invalid path or a real permissions problem should still fail immediately, not silently retry 5 times first). Wrapped around both `update_llm_config()`'s `config.yaml` write and `update_source_config()`'s `.env` `set_key()` calls — the same underlying Windows quirk can equally hit the direct `path.open("w")` `config.yaml` write, even though that one doesn't do the temp-file-rename dance `.env`'s write does. Both functions' existing `except OSError` (which `PermissionError` is a subclass of) still catches an exhausted-retries failure and wraps it in `ConfigError` exactly as before — this only adds a few quick retries before that path is reached, not a behavior change for a genuinely broken write.

**Also fixed along the way**: my own diagnostic reproduction (a real, non-fixture-isolated call to `set_key()` against the real `config/.env`, to confirm whether the write itself was broken) accidentally overwrote the real `LOCAL_FILES_WATCH_DIRS` value with a placeholder — a mistake, not something the fix required; should have used a temp-path fixture the way the existing test suite already does. No local files had actually been ingested yet in the real database (confirmed directly — zero `local_file` rows), so there was no way to recover the original value from ingested data either; asked the user directly and restored it to the confirmed-correct folder.

**Verified against real data**: real `update_source_config()` call against the real `config/.env` succeeded and was confirmed via `GET /api/settings/sources` against the real running backend after a restart. New tests (`TestRetryOnTransientPermissionError`, plus one `TestUpdateSourceConfig` case that makes `dotenv.set_key()` fail once before succeeding) pass; full backend suite passes except two pre-existing, unrelated failures (`TestLoadConfig::test_parses_the_committed_config_yaml`, `TestGetSettings::test_returns_both_halves`) — both read the *real* `config.yaml` directly rather than a fixture, and now reflect the user's own real `provider_mode: fully_cloud` choice made through Settings rather than the file's committed default; not something this fix touched or should "correct" by overwriting a real, deliberate user setting.

**Affects**: `config/settings.py`, `tests/test_config/test_settings.py`

---

## 2026-08-30 — Replace `window.confirm()` with a custom modal dialog

**Context**: The native browser `confirm()`/`cancel` dialog looks nothing like the rest of the app (unstyled, can't render structured content like a bullet list or a highlighted warning) — requested directly to replace it with something that matches the app's own design.

**Decision**: Added `frontend/src/components/ConfirmDialog.jsx` (a centered modal: backdrop, card, title, body, Cancel/Confirm buttons, styled with the same CSS custom properties — `--bg`, `--border`, `--accent`, `--danger` — every other component already uses) and `frontend/src/hooks/useConfirm.jsx` (a promise-based `confirm(options) -> Promise<boolean>`, mirroring `window.confirm()`'s call shape so existing `if (!(await confirm(...))) return;` call sites needed minimal changes, but returning a `[confirm, dialog]` pair — the caller renders `{dialog}` once in its own JSX and calls `confirm()` from event handlers). Split into two files, not one — co-locating a hook and the component it renders in a single file trips this project's `react-refresh/only-export-components` lint rule (fast refresh only works cleanly when a file exports only components).

`body` accepts a full `ReactNode`, not just a string — `SettingsPanel.jsx`'s save-confirmation dialog renders an actual `<ul>` of changed fields plus a separately-styled `.confirm-dialog-warning` callout when the embedding model is among them, rather than flattening everything into a single string with `\n` line breaks the way the `window.confirm()` version had to.

Escape key and clicking the backdrop both cancel; the Confirm button receives initial focus. Not a full focus trap (tab still escapes the dialog into the page behind it) — acceptable for a local, single-user tool; a complete trap is future work if it ever matters.

**Verified**: frontend build and `eslint` (zero warnings, including the fast-refresh rule that caught the single-file version) both clean.

**Affects**: `frontend/src/components/ConfirmDialog.jsx` (new), `frontend/src/hooks/useConfirm.jsx` (new), `frontend/src/components/SettingsPanel.jsx`, `frontend/src/index.css`

---

## 2026-08-30 — Un-freeze the embedding model; confirm-then-auto-reset-and-reingest on every Settings save; interactive graph

**Context**: Three corrections/requests in one round. First, a genuine miscommunication on my part: "freeze the embedding model" (an earlier entry, below) was meant by the user as "cloud-only, not user-toggleable between local/cloud" — not "the user can never type in a different cloud model name." Second: the "Fully Local"/"Fully Cloud" provider-mode labels no longer matched reality once embedding became cloud-only regardless of mode — those labels implied embedding followed the mode too. Third: the graph view (previous entry) rendered a one-shot static layout — the user wanted an Obsidian-style interactive graph, draggable, with a live physics simulation.

**Decision — embedding model, un-frozen**: `cloud_embedding_model` is back as a genuine `LLMConfig` field (`config.yaml`, `update_llm_config()`, `SettingsUpdateRequest`) — no more `agent/embedding_info.py` thin wrapper (deleted; it existed only because the value used to be a `providers/`-layer constant the API layer wasn't allowed to reach for directly, which no longer applies now that it's a normal `config.llm` field like the generation models). `providers/openrouter_provider.py::create_openrouter_provider()` resolves `embedding_model` from `settings.config.llm.cloud_embedding_model` the same way it already resolved `cloud_generation_model` — freshly on every call, so a saved change takes effect on the very next provider call with no restart needed. Also dropped the forced `dimensions=384` truncation from the embedding call — that existed specifically to keep two embedding models (local + cloud) dimension-compatible with each other; with only one embedding model in the system at all, there's nothing left to match against, so each configured model now embeds at its own native width.

**Decision — confirm before every settings save, stronger warning + auto-reset for the embedding model specifically**: Requested directly — every "Save Changes" click now shows one `window.confirm()` listing exactly which fields changed (diffed against the settings last successfully loaded/saved, tracked in a `useRef` snapshot in `SettingsPanel.jsx`) before writing anything. Cancelling aborts the entire save — no partial writes. When the diff includes `cloud_embedding_model` specifically, the same dialog appends an explicit warning that changing it requires a full reset and re-ingestion (since old vectors won't match the new model's embedding space), and confirming triggers `putSettings()` followed automatically by `onResetAll()` then `onTriggerIngestion()` (the same `App.jsx`-owned flows the Danger Zone and "Run ingestion now" button already use) — the user doesn't need to separately click Reset and then Run Ingestion afterward. Every other settings change (provider mode, generation models, watch folders, Notion scope) still gets the same confirm dialog, but without the extra warning or the automatic reset/reingest — those fields don't invalidate stored data the way the embedding model does.

**Decision — label fix**: Provider-mode radio labels changed from "Fully Local"/"Fully Cloud" to "Local Generation"/"Cloud Generation", and the section header from "LLM Provider" to "Generation Provider" — both now accurately describe that this toggle only ever affects generation (answers, metadata, relationship judging), never embedding (which is always cloud, shown as its own always-editable field below, independent of this toggle).

**Decision — interactive, Obsidian-style graph**: `GraphView.jsx` no longer runs `d3-force` to convergence once and renders a static picture — it keeps the simulation alive (`forceSimulation(...).on("tick", ...)`, no `.stop()`), re-rendering on every tick. Dragging a node (pointer events on each node's `<g>`, using `setPointerCapture`/`releasePointerCapture` so the drag tracks correctly even if the cursor leaves the small circle) sets that node's `fx`/`fy` (d3-force's fixed-position override) to the pointer's position — converted from screen coordinates into the SVG's own `viewBox` coordinate space via `getScreenCTM().inverse()`, so dragging stays accurate regardless of how the SVG is CSS-scaled to fit its container — and calls `simulation.alphaTarget(0.3).restart()` to "reheat" the simulation so neighboring nodes visibly respond to the drag, same as Obsidian's graph view. Releasing clears `fx`/`fy` back to `null` (letting forces act on the node again, rather than pinning it permanently) and cools the simulation back down (`alphaTarget(0)`).

The d3-mutated node/link objects live in `nodesRef`/`linksRef` (needed because drag handlers must mutate the exact objects d3-force's internal tick loop also mutates), but what's actually rendered is a separate `nodes`/`links` **state** pair, shallow-copied from those refs on every tick (`setNodes([...nodesRef.current])`) — reading a ref's `.current` directly during render doesn't trigger a re-render and is flagged by this project's `react-hooks/refs` lint rule; the shallow copy is cheap for a graph this size and keeps the same underlying node *objects* (only the array wrapper is new), so drag mutations via the ref and renders via the state both act on the same real objects the simulation is ticking.

**Verified against real data**: full backend suite (411 tests), `black`, `ruff` all pass. Frontend build and `eslint` (including the `react-hooks/refs` rule, which caught the first ref-during-render draft directly) both clean.

**Affects**: `providers/openrouter_provider.py`, `config/settings.py`, `config/config.yaml`, `api/schemas.py`, `api/routes/settings.py`, `agent/embedding_info.py` (removed), `frontend/src/components/SettingsPanel.jsx`, `frontend/src/components/GraphView.jsx`, `frontend/src/index.css`, `tests/test_pipeline/test_embeddings.py` (dimension now measured via a real call, not hardcoded)

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
