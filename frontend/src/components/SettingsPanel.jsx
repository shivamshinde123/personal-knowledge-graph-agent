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
import saveIcon from "../assets/icons/save-icon.svg";
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
 * @param {object} props
 * @param {() => Promise<void>} props.onTriggerIngestion - starts a batch
 *   run and polls for completion; owned by App.jsx so the "done" toast
 *   still shows up if the user has navigated away from Settings by then.
 * @param {() => Promise<void>} props.onResetAll - wipes all data; owned by
 *   App.jsx since it also clears the active chat session.
 * @param {(text: string) => void} props.onError - surfaces a failure from
 *   either action as a toast.
 * @param {{startedAt: Date, runId: string, itemsProcessed: number, status: string} | null} props.ingestionStatus -
 *   the most recently triggered run's live progress, owned by App.jsx so
 *   it survives navigating away and back; null before anything's been
 *   triggered this session.
 */
function SettingsPanel({
  onTriggerIngestion,
  onResetAll,
  onError,
  ingestionStatus,
}) {
  const [settings, setSettings] = useState(null);
  const [sources, setSources] = useState(null);
  const [connections, setConnections] = useState(null);
  const [watchDirsText, setWatchDirsText] = useState("");
  const [notionPageIdsText, setNotionPageIdsText] = useState("");
  const [loadError, setLoadError] = useState(null);
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState(null);
  const [isVerifying, setIsVerifying] = useState(false);
  const [isTriggeringIngest, setIsTriggeringIngest] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [isBrowsing, setIsBrowsing] = useState(false);
  // The last-saved snapshot of every editable field, for diffing at save
  // time — a ref, not state, since it's only ever read/written on load and
  // on a successful save, never rendered directly.
  const originalRef = useRef(null);
  const [confirm, confirmDialog] = useConfirm();

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
          setSettings(settingsResult);
          setSources(sourcesResult);
          setConnections(connectionsResult);
          setWatchDirsText(watchDirs);
          setNotionPageIdsText(notionPageIds);
          originalRef.current = {
            ...settingsResult,
            watchDirsText: watchDirs,
            notionPageIdsText: notionPageIds,
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
      setWatchDirsText((prev) => {
        const existing = linesToList(prev);
        if (existing.includes(path)) return prev; // already added
        return [...existing, path].join("\n");
      });
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

  async function handleSave() {
    const original = originalRef.current;
    const changes = [];
    if (settings.provider_mode !== original.provider_mode) {
      changes.push(`Generation provider → ${settings.provider_mode}`);
    }
    if (settings.local_generation_model !== original.local_generation_model) {
      changes.push(
        `Local generation model → ${settings.local_generation_model}`,
      );
    }
    if (settings.cloud_generation_model !== original.cloud_generation_model) {
      changes.push(
        `Cloud generation model → ${settings.cloud_generation_model}`,
      );
    }
    const embeddingModelChanged =
      settings.cloud_embedding_model !== original.cloud_embedding_model;
    if (embeddingModelChanged) {
      changes.push(`Embedding model → ${settings.cloud_embedding_model}`);
    }
    if (watchDirsText !== original.watchDirsText) {
      changes.push("Local watch folders");
    }
    if (notionPageIdsText !== original.notionPageIdsText) {
      changes.push("Notion page scope");
    }

    if (changes.length === 0) {
      setSaveStatus({ ok: true, text: "Nothing changed." });
      return;
    }

    const confirmed = await confirm({
      title: "Save these changes?",
      body: (
        <>
          <ul className="confirm-dialog-list">
            {changes.map((change) => (
              <li key={change}>{change}</li>
            ))}
          </ul>
          {embeddingModelChanged && (
            <p className="confirm-dialog-warning">
              Changing the embedding model requires a full data reset and
              re-ingestion — your existing embeddings won't match the new model.
              Confirming will save this change, then automatically reset all
              data and start a fresh ingestion run.
            </p>
          )}
        </>
      ),
      danger: embeddingModelChanged,
      confirmLabel: embeddingModelChanged ? "Save and reset" : "Save",
    });
    if (!confirmed) return;

    setIsSaving(true);
    setSaveStatus(null);
    try {
      const [settingsResult] = await Promise.all([
        putSettings({
          provider_mode: settings.provider_mode,
          local_generation_model: settings.local_generation_model,
          cloud_generation_model: settings.cloud_generation_model,
          cloud_embedding_model: settings.cloud_embedding_model,
        }),
        putSourceConfig({
          local_files_watch_dirs: linesToList(watchDirsText),
          notion_page_ids: linesToList(notionPageIdsText),
        }),
      ]);
      setSettings(settingsResult);
      originalRef.current = {
        ...settingsResult,
        watchDirsText,
        notionPageIdsText,
      };
      setSaveStatus({ ok: true, text: "Saved." });

      if (embeddingModelChanged) {
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
      }
    } catch (error) {
      setSaveStatus({ ok: false, text: error.message });
    } finally {
      setIsSaving(false);
    }
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
      itemsProcessed: sources.last_run.items_processed,
      status: sources.last_run.status,
    });

  return (
    <div className="settings-panel">
      {confirmDialog}
      <div className="settings-header">
        <div>
          <h1>Settings</h1>
          <p className="settings-subtitle">
            Configure your ambient intelligence environment, models, and data
            sources.
          </p>
        </div>
        <div className="save-action">
          <button
            type="button"
            className="save-button"
            onClick={handleSave}
            disabled={isSaving}
          >
            {isSaving ? "Saving…" : "Save Changes"}
            <img src={saveIcon} alt="" aria-hidden="true" />
          </button>
          {saveStatus && (
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
                    onChange={() =>
                      setSettings((prev) => ({
                        ...prev,
                        provider_mode: mode.value,
                      }))
                    }
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
              — saving a change here will ask you to confirm that first.
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
                placeholder="e.g. openai/text-embedding-3-small"
              />
            </div>
          </section>
        </div>

        <div className="settings-column">
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
              placeholder="e.g. 1a2b3c4d5e6f7890abcd1234ef567890"
            />
          </section>

          <section className="settings-section">
            <div className="settings-section-header settings-section-header-bordered">
              <div>
                <h2>Data Ingestion</h2>
                <p className="settings-field-hint">
                  Normally runs once a day on a schedule.
                </p>
              </div>
              <button
                type="button"
                className="settings-ghost-button"
                onClick={handleTriggerIngestion}
                disabled={isTriggeringIngest}
              >
                <img src={ingestIcon} alt="" aria-hidden="true" />
                {isTriggeringIngest ? "Starting…" : "Run ingestion now"}
              </button>
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
      </div>
    </div>
  );
}

export default SettingsPanel;
