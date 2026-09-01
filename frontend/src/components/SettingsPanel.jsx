import { useEffect, useRef, useState } from "react";
import {
  getSettings,
  getSourceConfig,
  getSourceConnections,
  getSourcesStatus,
  postBrowseFolder,
  putSettings,
  putSourceConfig,
  verifySourceConnections,
} from "../api/client.js";
import { useConfirm } from "../hooks/useConfirm.jsx";
import radioCheckIcon from "../assets/icons/radio-check-icon.svg";
import ingestIcon from "../assets/icons/ingest-icon.svg";
import reverifyIcon from "../assets/icons/reverify-icon.svg";
import dangerIcon from "../assets/icons/danger-icon.svg";

/** "a\nb\n\nc" -> ["a", "b", "c"] — one entry per non-blank line. */
function linesToList(text) {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

const CONNECTION_LABELS = {
  ok: "Connected",
  error: "Error",
  not_configured: "Not configured",
};

const PROVIDER_MODES = [
  {
    value: "fully_local",
    label: "Local Generation",
    description:
      "Answers, metadata, and relationship judging run through Ollama — no cost. This only affects generation: embedding always uses the cloud model below regardless, so an OpenRouter API key is still required.",
  },
  {
    value: "fully_cloud",
    label: "Cloud Generation",
    description:
      "Answers, metadata, and relationship judging route through OpenRouter — highest quality, per-call cost.",
  },
];

/**
 * The Settings screen: generation provider, the two generation model
 * fields (gated by provider), the one embedding model field (always
 * cloud, editable but changing it triggers a confirm + auto reset +
 * re-ingest), and connected data source status. Per
 * docs/UIUX_Wireframes.docx section 3.
 *
 * Every field saves itself the moment it's committed (a radio pick, or a
 * text/textarea/date field losing focus after an actual change) — there
 * is no separate "Save Changes" step. This is simpler for the user at
 * the cost of a save round-trip per field rather than one batched call;
 * acceptable for a local, single-user system with no meaningful latency
 * to the backend. See DECISIONS.md.
 *
 * @param {object} props
 * @param {() => Promise<void>} props.onTriggerIngestion - starts a batch
 *   run and polls for completion; owned by App.jsx so the "done" toast
 *   still shows up if the user has navigated away from Settings by then.
 * @param {(dbRunId: string) => Promise<void>} props.onCancelIngestion - asks
 *   a running batch to stop; owned by App.jsx alongside onTriggerIngestion
 *   so the same poll loop picks up the "cancelled" outcome.
 * @param {() => Promise<void>} props.onResetAll - wipes all data; owned by
 *   App.jsx since it also clears the active chat session.
 * @param {(text: string) => void} props.onError - surfaces a failure from
 *   either action as a toast.
 * @param {{startedAt: Date, runId: string, dbRunId: string | null, itemsProcessed: number, status: string} | null} props.ingestionStatus -
 *   the most recently triggered run's live progress, owned by App.jsx so
 *   it survives navigating away and back; null before anything's been
 *   triggered this session.
 */
function SettingsPanel({
  onTriggerIngestion,
  onCancelIngestion,
  onResetAll,
  onError,
  ingestionStatus,
}) {
  const [settings, setSettings] = useState(null);
  const [sources, setSources] = useState(null);
  const [connections, setConnections] = useState(null);
  const [watchDirsText, setWatchDirsText] = useState("");
  const [notionPageIdsText, setNotionPageIdsText] = useState("");
  const [githubReposText, setGithubReposText] = useState("");
  const [gmailDateRangeStart, setGmailDateRangeStart] = useState("");
  const [gmailDateRangeEnd, setGmailDateRangeEnd] = useState("");
  const [githubDateRangeStart, setGithubDateRangeStart] = useState("");
  const [githubDateRangeEnd, setGithubDateRangeEnd] = useState("");
  const [calendarDateRangeStart, setCalendarDateRangeStart] = useState("");
  const [calendarDateRangeEnd, setCalendarDateRangeEnd] = useState("");
  const [loadError, setLoadError] = useState(null);
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState(null);
  const [isVerifying, setIsVerifying] = useState(false);
  const [isTriggeringIngest, setIsTriggeringIngest] = useState(false);
  const [isCancellingIngest, setIsCancellingIngest] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [isBrowsing, setIsBrowsing] = useState(false);
  // The last-saved value of every editable field, so a field's onBlur can
  // tell "actually changed" from "focused and clicked away again" before
  // firing a save — a ref, not state, since it's only ever read/written
  // around a save, never rendered directly.
  const lastSavedRef = useRef(null);
  // Serializes every auto-save call (saveLlmField/saveScopeTextarea/
  // saveDateField/saveEmbeddingModel) so two blur/change events firing
  // close together (e.g. tabbing quickly through fields, or a field's
  // save still in flight when another fires) can never send overlapping
  // PUT /settings or PUT /settings/sources requests. Both endpoints do a
  // read-the-whole-file, mutate, write-the-whole-file-back update on
  // config.yaml/.env — a second request reading before the first's write
  // lands would silently clobber it (a lost update). Queuing them through
  // one shared "tail" promise means the second request only starts
  // building its own payload once the first has actually completed. See
  // DECISIONS.md.
  const saveQueueRef = useRef(Promise.resolve());
  const [confirm, confirmDialog] = useConfirm();

  /** Runs `work` only after every previously enqueued save has settled. */
  function enqueueSave(work) {
    const run = saveQueueRef.current.then(work, work);
    saveQueueRef.current = run;
    return run;
  }

  useEffect(() => {
    Promise.all([
      getSettings(),
      getSourcesStatus(),
      getSourceConnections(),
      getSourceConfig(),
    ])
      .then(
        ([settingsResult, sourcesResult, connectionsResult, sourceConfig]) => {
          const watchDirs = sourceConfig.local_files_watch_dirs.join("\n");
          const notionPageIds = sourceConfig.notion_page_ids.join("\n");
          const githubRepos = sourceConfig.github_repos.join("\n");
          const gmailStart = sourceConfig.gmail_date_range_start ?? "";
          const gmailEnd = sourceConfig.gmail_date_range_end ?? "";
          const githubStart = sourceConfig.github_date_range_start ?? "";
          const githubEnd = sourceConfig.github_date_range_end ?? "";
          const calendarStart = sourceConfig.calendar_date_range_start ?? "";
          const calendarEnd = sourceConfig.calendar_date_range_end ?? "";
          setSettings(settingsResult);
          setSources(sourcesResult);
          setConnections(connectionsResult);
          setWatchDirsText(watchDirs);
          setNotionPageIdsText(notionPageIds);
          setGithubReposText(githubRepos);
          setGmailDateRangeStart(gmailStart);
          setGmailDateRangeEnd(gmailEnd);
          setGithubDateRangeStart(githubStart);
          setGithubDateRangeEnd(githubEnd);
          setCalendarDateRangeStart(calendarStart);
          setCalendarDateRangeEnd(calendarEnd);
          lastSavedRef.current = {
            ...settingsResult,
            watchDirsText: watchDirs,
            notionPageIdsText: notionPageIds,
            githubReposText: githubRepos,
            gmailDateRangeStart: gmailStart,
            gmailDateRangeEnd: gmailEnd,
            githubDateRangeStart: githubStart,
            githubDateRangeEnd: githubEnd,
            calendarDateRangeStart: calendarStart,
            calendarDateRangeEnd: calendarEnd,
          };
        },
      )
      .catch((error) => setLoadError(error.message));
  }, []);

  async function handleBrowseFolder() {
    setIsBrowsing(true);
    try {
      const { path } = await postBrowseFolder();
      if (path === null) return; // user cancelled the dialog
      const existing = linesToList(watchDirsText);
      if (existing.includes(path)) return; // already added
      const updated = [...existing, path].join("\n");
      setWatchDirsText(updated);
      // No blur event fires here — the picked path lands in the textarea
      // without the user ever focusing it, so the usual onBlur save never
      // triggers on its own; save explicitly instead.
      await saveScopeTextarea(
        "local_files_watch_dirs",
        "watchDirsText",
        setWatchDirsText,
        updated,
        [...existing, path],
      );
    } catch (error) {
      onError(`Could not open the folder picker: ${error.message}`);
    } finally {
      setIsBrowsing(false);
    }
  }

  async function handleReverify() {
    setIsVerifying(true);
    try {
      setConnections(await verifySourceConnections());
    } catch (error) {
      setLoadError(error.message);
    } finally {
      setIsVerifying(false);
    }
  }

  async function handleTriggerIngestion() {
    setIsTriggeringIngest(true);
    try {
      await onTriggerIngestion();
    } catch (error) {
      onError(`Could not start ingestion: ${error.message}`);
    } finally {
      setIsTriggeringIngest(false);
    }
  }

  async function handleCancelIngestion(dbRunId) {
    const confirmed = await confirm({
      title: "Stop ingestion?",
      body: "Items already processed will be kept — nothing is rolled back, but anything not yet reached this run will need a later run to pick up.",
      confirmLabel: "Stop ingestion",
    });
    if (!confirmed) return;

    setIsCancellingIngest(true);
    try {
      await onCancelIngestion(dbRunId);
    } catch (error) {
      onError(`Could not stop ingestion: ${error.message}`);
    } finally {
      setIsCancellingIngest(false);
    }
  }

  async function handleResetAll() {
    const confirmed = await confirm({
      title: "Reset all data?",
      body: "This permanently deletes every ingested item, embedding, and relationship — SQLite, Chroma, and Neo4j are all wiped. This cannot be undone.",
      danger: true,
      confirmLabel: "Reset everything",
    });
    if (!confirmed) return;

    setIsResetting(true);
    try {
      await onResetAll();
      const [sourcesResult, connectionsResult] = await Promise.all([
        getSourcesStatus(),
        getSourceConnections(),
      ]);
      setSources(sourcesResult);
      setConnections(connectionsResult);
    } catch (error) {
      onError(`Could not reset data: ${error.message}`);
    } finally {
      setIsResetting(false);
    }
  }

  /** Save one LLM-settings field the moment it's committed (radio pick, or blur). */
  function saveLlmField(apiKey, value) {
    return enqueueSave(async () => {
      if (lastSavedRef.current[apiKey] === value) return; // no real change
      setIsSaving(true);
      setSaveStatus(null);
      try {
        const result = await putSettings({ [apiKey]: value });
        setSettings(result);
        lastSavedRef.current = { ...lastSavedRef.current, ...result };
        setSaveStatus({ ok: true, text: "Saved." });
      } catch (error) {
        setSaveStatus({ ok: false, text: error.message });
      } finally {
        setIsSaving(false);
      }
    });
  }

  /**
   * The embedding model is the one field that can't just auto-save
   * silently — changing it strands every existing embedding (a different
   * model means a different vector space), so it still needs a confirm
   * plus an automatic reset + re-ingest. Declining reverts the field to
   * its last-saved value rather than leaving an unsaved edit sitting in
   * the input with no "Save" button left to abandon it via.
   */
  function saveEmbeddingModel(value) {
    return enqueueSave(async () => {
      if (lastSavedRef.current.cloud_embedding_model === value) return;
      const confirmed = await confirm({
        title: "Change embedding model?",
        body: (
          <p className="confirm-dialog-warning">
            Changing the embedding model requires a full data reset and
            re-ingestion — your existing embeddings won't match the new model.
            Confirming will save this change, then automatically reset all data
            and start a fresh ingestion run.
          </p>
        ),
        danger: true,
        confirmLabel: "Save and reset",
      });
      if (!confirmed) {
        setSettings((prev) => ({
          ...prev,
          cloud_embedding_model: lastSavedRef.current.cloud_embedding_model,
        }));
        return;
      }

      setIsSaving(true);
      setSaveStatus({ ok: true, text: "Saving…" });
      try {
        const result = await putSettings({ cloud_embedding_model: value });
        setSettings(result);
        lastSavedRef.current = { ...lastSavedRef.current, ...result };
        setSaveStatus({
          ok: true,
          text: "Saved. Resetting data and re-ingesting…",
        });
        await onResetAll();
        await onTriggerIngestion();
        const [sourcesResult, connectionsResult] = await Promise.all([
          getSourcesStatus(),
          getSourceConnections(),
        ]);
        setSources(sourcesResult);
        setConnections(connectionsResult);
        setSaveStatus({ ok: true, text: "Saved. Re-ingestion started." });
      } catch (error) {
        setSaveStatus({ ok: false, text: error.message });
      } finally {
        setIsSaving(false);
      }
    });
  }

  /** Save one of the three source-scope textareas (watch dirs, Notion, GitHub). */
  function saveScopeTextarea(
    apiKey,
    lastSavedKey,
    setText,
    text,
    listOverride,
  ) {
    return enqueueSave(async () => {
      if (lastSavedRef.current[lastSavedKey] === text) return;
      setIsSaving(true);
      setSaveStatus(null);
      try {
        const result = await putSourceConfig({
          [apiKey]: listOverride ?? linesToList(text),
        });
        const joined = result[apiKey].join("\n");
        setText(joined);
        lastSavedRef.current[lastSavedKey] = joined;
        setSaveStatus({ ok: true, text: "Saved." });
      } catch (error) {
        setSaveStatus({ ok: false, text: error.message });
      } finally {
        setIsSaving(false);
      }
    });
  }

  /** Save one of the six Gmail/GitHub/Calendar date-range fields. */
  function saveDateField(apiKey, lastSavedKey, setValue, value) {
    return enqueueSave(async () => {
      if (lastSavedRef.current[lastSavedKey] === value) return;
      setIsSaving(true);
      setSaveStatus(null);
      try {
        const result = await putSourceConfig({ [apiKey]: value });
        const newValue = result[apiKey] ?? "";
        setValue(newValue);
        lastSavedRef.current[lastSavedKey] = newValue;
        setSaveStatus({ ok: true, text: "Saved." });
      } catch (error) {
        setSaveStatus({ ok: false, text: error.message });
      } finally {
        setIsSaving(false);
      }
    });
  }

  if (loadError) {
    return (
      <div className="settings-panel">
        <h1>Settings</h1>
        <p className="save-status error">{loadError}</p>
      </div>
    );
  }

  if (!settings || !sources || !connections) {
    return (
      <div className="settings-panel">
        <h1>Settings</h1>
        <p>Loading…</p>
      </div>
    );
  }

  const isLocal = settings.provider_mode === "fully_local";
  const isCloud = settings.provider_mode === "fully_cloud";

  // `ingestionStatus` (App.jsx) is only ever set once something is
  // triggered/polled *this session* — on a fresh page load (or after
  // navigating back to Settings in a new tab) it's null even though a
  // real run happened yesterday. `sources.last_run`, fetched on mount
  // from the real, persisted `ingestion_runs` table, doesn't have that
  // gap — same idea as "Last checked" below always showing a real value
  // rather than only appearing once you click Reverify this session.
  // Prefer the live `ingestionStatus` when present (it's actively
  // polling and reflects "status: running" mid-run, which the persisted
  // row alone can't distinguish from a stale/stuck run), and fall back
  // to the persisted last run otherwise.
  const displayedIngestion =
    ingestionStatus ??
    (sources.last_run && {
      startedAt: new Date(sources.last_run.started_at),
      dbRunId: sources.last_run.run_id,
      itemsProcessed: sources.last_run.items_processed,
      status: sources.last_run.status,
      currentItem: sources.last_run.current_item,
    });

  return (
    <div className="settings-panel">
      {confirmDialog}
      <div className="settings-header">
        <div>
          <h1>Settings</h1>
          <p className="settings-subtitle">
            Configure your ambient intelligence environment, models, and data
            sources. Every change saves itself automatically.
          </p>
        </div>
        <div className="save-action">
          {isSaving && <span className="save-status">Saving…</span>}
          {!isSaving && saveStatus && (
            <span className={`save-status ${saveStatus.ok ? "" : "error"}`}>
              {saveStatus.text}
            </span>
          )}
        </div>
      </div>

      <div className="settings-columns">
        <div className="settings-column">
          <section className="settings-section">
            <h2>Generation Provider</h2>
            <p className="settings-field-hint">
              Select where processing occurs. Cloud offers higher quality, local
              ensures privacy.
            </p>
            {PROVIDER_MODES.map((mode) => {
              const selected = settings.provider_mode === mode.value;
              return (
                <label
                  className={`radio-option ${selected ? "selected" : ""}`}
                  key={mode.value}
                >
                  <input
                    type="radio"
                    name="provider_mode"
                    value={mode.value}
                    checked={selected}
                    onChange={() => {
                      setSettings((prev) => ({
                        ...prev,
                        provider_mode: mode.value,
                      }));
                      saveLlmField("provider_mode", mode.value);
                    }}
                  />
                  <span className="radio-option-dot">
                    {selected && (
                      <img src={radioCheckIcon} alt="" aria-hidden="true" />
                    )}
                  </span>
                  <span className="radio-option-body">
                    <span className="radio-option-label">{mode.label}</span>
                    <p>{mode.description}</p>
                  </span>
                </label>
              );
            })}
          </section>

          <section className="settings-section">
            <h2>Models</h2>
            <p className="settings-field-hint">
              Generation: only the model matching the provider above is actually
              used — both are kept here so switching doesn't lose the other
              side's value. Embedding is separate and always cloud, regardless
              of which generation provider is selected — there's no local
              embedding option, so an OpenRouter API key is required either way.
              Changing the embedding model needs a full data reset and
              re-ingestion, since existing embeddings won't match the new model
              — leaving this field after a change will ask you to confirm that
              first.
            </p>

            <div className="model-field">
              <label htmlFor="local-generation-model">
                Local generation model
              </label>
              <input
                id="local-generation-model"
                className="model-select"
                type="text"
                disabled={!isLocal}
                value={settings.local_generation_model}
                onChange={(event) =>
                  setSettings((prev) => ({
                    ...prev,
                    local_generation_model: event.target.value,
                  }))
                }
                onBlur={(event) =>
                  saveLlmField("local_generation_model", event.target.value)
                }
                placeholder="e.g. llama3:8b"
              />
            </div>

            <div className="model-field">
              <label htmlFor="cloud-generation-model">
                Cloud generation model
              </label>
              <input
                id="cloud-generation-model"
                className="model-select"
                type="text"
                disabled={!isCloud}
                value={settings.cloud_generation_model}
                onChange={(event) =>
                  setSettings((prev) => ({
                    ...prev,
                    cloud_generation_model: event.target.value,
                  }))
                }
                onBlur={(event) =>
                  saveLlmField("cloud_generation_model", event.target.value)
                }
                placeholder="e.g. anthropic/claude-sonnet-4"
              />
            </div>

            <div className="model-field">
              <div className="model-field-label-row">
                <label htmlFor="cloud-embedding-model">Embedding model</label>
                <span className="model-field-fixed">Always cloud</span>
              </div>
              <input
                id="cloud-embedding-model"
                className="model-select"
                type="text"
                value={settings.cloud_embedding_model}
                onChange={(event) =>
                  setSettings((prev) => ({
                    ...prev,
                    cloud_embedding_model: event.target.value,
                  }))
                }
                onBlur={(event) => saveEmbeddingModel(event.target.value)}
                placeholder="e.g. openai/text-embedding-3-small"
              />
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section-header">
              <h2>Local folders to watch</h2>
              <button
                type="button"
                className="settings-ghost-button"
                onClick={handleBrowseFolder}
                disabled={isBrowsing}
              >
                {isBrowsing ? "Browsing…" : "Browse…"}
              </button>
            </div>
            <p className="settings-field-hint">
              One folder path per line — paste a path, or use "Browse…" to pick
              one. Files added or changed in these folders are picked up on the
              next ingestion run.
            </p>
            <textarea
              className="source-scope-textarea"
              rows={3}
              value={watchDirsText}
              onChange={(event) => setWatchDirsText(event.target.value)}
              onBlur={(event) =>
                saveScopeTextarea(
                  "local_files_watch_dirs",
                  "watchDirsText",
                  setWatchDirsText,
                  event.target.value,
                )
              }
              placeholder={"C:\\Users\\you\\Documents\\Notes"}
            />
          </section>

          <section className="settings-section">
            <h2>Notion page scope</h2>
            <p className="settings-field-hint">
              One page or database ID per line. Leave empty to ingest every page
              the Notion integration can see.
            </p>
            <textarea
              className="source-scope-textarea"
              rows={3}
              value={notionPageIdsText}
              onChange={(event) => setNotionPageIdsText(event.target.value)}
              onBlur={(event) =>
                saveScopeTextarea(
                  "notion_page_ids",
                  "notionPageIdsText",
                  setNotionPageIdsText,
                  event.target.value,
                )
              }
              placeholder="e.g. 1a2b3c4d5e6f7890abcd1234ef567890"
            />
          </section>

          <section className="settings-section">
            <h2>GitHub repository scope</h2>
            <p className="settings-field-hint">
              One <code>owner/repo</code> per line. Leave empty to ingest every
              repository the token can access.
            </p>
            <textarea
              className="source-scope-textarea"
              rows={3}
              value={githubReposText}
              onChange={(event) => setGithubReposText(event.target.value)}
              onBlur={(event) =>
                saveScopeTextarea(
                  "github_repos",
                  "githubReposText",
                  setGithubReposText,
                  event.target.value,
                )
              }
              placeholder="e.g. octocat/hello-world"
            />
          </section>

          <section className="settings-section danger-zone">
            <h2>
              <img src={dangerIcon} alt="" aria-hidden="true" />
              Danger Zone
            </h2>
            <p className="settings-field-hint">
              Permanently deletes every ingested item, embedding, and
              relationship. Use this to recover from a bad ingestion run or to
              restart from a clean slate — there is no undo.
            </p>
            <button
              type="button"
              className="danger-button"
              onClick={handleResetAll}
              disabled={isResetting}
            >
              {isResetting ? "Resetting…" : "Reset all data"}
            </button>
          </section>
        </div>

        <div className="settings-column">
          <section className="settings-section">
            <h2>Gmail date range</h2>
            <p className="settings-field-hint">
              Only ingest messages within this range — defaults to the last 15
              days when left empty, not unbounded.
            </p>
            <div className="date-range-fields">
              <label>
                From
                <input
                  type="date"
                  value={gmailDateRangeStart}
                  onChange={(event) =>
                    setGmailDateRangeStart(event.target.value)
                  }
                  onBlur={(event) =>
                    saveDateField(
                      "gmail_date_range_start",
                      "gmailDateRangeStart",
                      setGmailDateRangeStart,
                      event.target.value,
                    )
                  }
                />
              </label>
              <label>
                To
                <input
                  type="date"
                  value={gmailDateRangeEnd}
                  onChange={(event) => setGmailDateRangeEnd(event.target.value)}
                  onBlur={(event) =>
                    saveDateField(
                      "gmail_date_range_end",
                      "gmailDateRangeEnd",
                      setGmailDateRangeEnd,
                      event.target.value,
                    )
                  }
                />
              </label>
            </div>
          </section>

          <section className="settings-section">
            <h2>GitHub date range</h2>
            <p className="settings-field-hint">
              Only ingest commits, PRs, issues, and stars within this range.
              Leave either side empty for no floor/ceiling.
            </p>
            <div className="date-range-fields">
              <label>
                From
                <input
                  type="date"
                  value={githubDateRangeStart}
                  onChange={(event) =>
                    setGithubDateRangeStart(event.target.value)
                  }
                  onBlur={(event) =>
                    saveDateField(
                      "github_date_range_start",
                      "githubDateRangeStart",
                      setGithubDateRangeStart,
                      event.target.value,
                    )
                  }
                />
              </label>
              <label>
                To
                <input
                  type="date"
                  value={githubDateRangeEnd}
                  onChange={(event) =>
                    setGithubDateRangeEnd(event.target.value)
                  }
                  onBlur={(event) =>
                    saveDateField(
                      "github_date_range_end",
                      "githubDateRangeEnd",
                      setGithubDateRangeEnd,
                      event.target.value,
                    )
                  }
                />
              </label>
            </div>
          </section>

          <section className="settings-section">
            <h2>Calendar date range</h2>
            <p className="settings-field-hint">
              Only ingest events whose own start time falls in this range —
              defaults to today through 30 days out (upcoming events), unlike
              the ranges above.
            </p>
            <div className="date-range-fields">
              <label>
                From
                <input
                  type="date"
                  value={calendarDateRangeStart}
                  onChange={(event) =>
                    setCalendarDateRangeStart(event.target.value)
                  }
                  onBlur={(event) =>
                    saveDateField(
                      "calendar_date_range_start",
                      "calendarDateRangeStart",
                      setCalendarDateRangeStart,
                      event.target.value,
                    )
                  }
                />
              </label>
              <label>
                To
                <input
                  type="date"
                  value={calendarDateRangeEnd}
                  onChange={(event) =>
                    setCalendarDateRangeEnd(event.target.value)
                  }
                  onBlur={(event) =>
                    saveDateField(
                      "calendar_date_range_end",
                      "calendarDateRangeEnd",
                      setCalendarDateRangeEnd,
                      event.target.value,
                    )
                  }
                />
              </label>
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section-header settings-section-header-bordered">
              <div>
                <h2>Data Ingestion</h2>
                <p className="settings-field-hint">
                  Normally runs once a day on a schedule.
                </p>
              </div>
              <div className="settings-ghost-button-group">
                <button
                  type="button"
                  className="settings-ghost-button"
                  onClick={handleTriggerIngestion}
                  disabled={isTriggeringIngest}
                >
                  <img src={ingestIcon} alt="" aria-hidden="true" />
                  {isTriggeringIngest ? "Starting…" : "Run ingestion now"}
                </button>
                {displayedIngestion?.status === "running" &&
                  displayedIngestion.dbRunId && (
                    <button
                      type="button"
                      className="settings-ghost-button settings-ghost-button-danger"
                      onClick={() =>
                        handleCancelIngestion(displayedIngestion.dbRunId)
                      }
                      disabled={isCancellingIngest}
                    >
                      {isCancellingIngest ? "Stopping…" : "Stop"}
                    </button>
                  )}
              </div>
            </div>
            {displayedIngestion && (
              <div className="ingestion-progress">
                <div className="ingestion-progress-header">
                  <span>
                    Started at {displayedIngestion.startedAt.toLocaleString()}
                    {displayedIngestion.status !== "running" && (
                      <>
                        {" "}
                        ·{" "}
                        {displayedIngestion.status === "success"
                          ? "Completed"
                          : `Finished (${displayedIngestion.status})`}
                      </>
                    )}
                  </span>
                  <span>
                    {displayedIngestion.itemsProcessed} item(s) processed
                  </span>
                </div>
                <div
                  className={`ingestion-progress-bar ${displayedIngestion.status === "running" ? "running" : displayedIngestion.status}`}
                >
                  <div className="ingestion-progress-bar-fill" />
                </div>
                {displayedIngestion.status === "running" &&
                  displayedIngestion.currentItem && (
                    <p className="ingestion-current-item">
                      {displayedIngestion.currentItem}
                    </p>
                  )}
              </div>
            )}

            <div className="settings-section-header">
              <h3>Connected Data Sources</h3>
              <button
                type="button"
                className="settings-text-button"
                onClick={handleReverify}
                disabled={isVerifying}
              >
                <img src={reverifyIcon} alt="" aria-hidden="true" />
                {isVerifying ? "Checking…" : "Reverify"}
              </button>
            </div>
            {connections.connections.length > 0 && (
              <p className="connections-checked-at">
                Last checked:{" "}
                {new Date(
                  connections.connections[0].checked_at,
                ).toLocaleString()}
              </p>
            )}
            <div className="sources-grid">
              {connections.connections.map((connection) => {
                const sourceCounts = sources.sources.find(
                  (source) => source.source_type === connection.source_type,
                );
                const connected = connection.status === "ok";
                return (
                  <div
                    className={`source-status-card ${connected ? "" : "dim"}`}
                    key={connection.source_type}
                    title={connection.detail ?? undefined}
                  >
                    <span>{connection.source_type.replace("_", " ")}</span>
                    <span className="source-status-value">
                      <span className={`status-dot ${connection.status}`} />
                      <span>
                        {CONNECTION_LABELS[connection.status] ??
                          connection.status}
                        {connected && sourceCounts !== undefined && (
                          <span className="source-item-count">
                            ({sourceCounts.total_items} total
                            {sourceCounts.items_processed > 0
                              ? `, ${sourceCounts.items_processed} new`
                              : ""}
                            )
                          </span>
                        )}
                      </span>
                    </span>
                  </div>
                );
              })}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

export default SettingsPanel;
