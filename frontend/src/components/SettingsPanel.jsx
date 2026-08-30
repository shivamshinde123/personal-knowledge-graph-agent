import { useEffect, useState } from "react";
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
    label: "Fully Local",
    description:
      "Runs entirely through Ollama and a local embedding model — no cost, no network dependency.",
  },
  {
    value: "fully_cloud",
    label: "Fully Cloud",
    description:
      "Routes every generation and embedding call through OpenRouter — highest quality, per-call cost.",
  },
];

/**
 * The Settings screen: LLM provider mode, the two editable generation
 * model fields plus the two fixed (display-only) embedding model fields,
 * and connected data source status. Per docs/UIUX_Wireframes.docx
 * section 3.
 *
 * @param {object} props
 * @param {() => Promise<void>} props.onTriggerIngestion - starts a batch
 *   run and polls for completion; owned by App.jsx so the "done" toast
 *   still shows up if the user has navigated away from Settings by then.
 * @param {() => Promise<void>} props.onResetAll - wipes all data; owned by
 *   App.jsx since it also clears the active chat session.
 * @param {(text: string) => void} props.onError - surfaces a failure from
 *   either action as a toast.
 */
function SettingsPanel({ onTriggerIngestion, onResetAll, onError }) {
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

  useEffect(() => {
    Promise.all([
      getSettings(),
      getSourcesStatus(),
      getSourceConnections(),
      getSourceConfig(),
    ])
      .then(
        ([settingsResult, sourcesResult, connectionsResult, sourceConfig]) => {
          setSettings(settingsResult);
          setSources(sourcesResult);
          setConnections(connectionsResult);
          setWatchDirsText(sourceConfig.local_files_watch_dirs.join("\n"));
          setNotionPageIdsText(sourceConfig.notion_page_ids.join("\n"));
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
    const confirmed = window.confirm(
      "This permanently deletes every ingested item, embedding, and " +
        "relationship — SQLite, Chroma, and Neo4j are all wiped. This " +
        "cannot be undone. Continue?",
    );
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
    setIsSaving(true);
    setSaveStatus(null);
    try {
      const [settingsResult] = await Promise.all([
        putSettings({
          provider_mode: settings.provider_mode,
          local_generation_model: settings.local_generation_model,
          cloud_generation_model: settings.cloud_generation_model,
        }),
        putSourceConfig({
          local_files_watch_dirs: linesToList(watchDirsText),
          notion_page_ids: linesToList(notionPageIdsText),
        }),
      ]);
      setSettings(settingsResult);
      setSaveStatus({ ok: true, text: "Saved." });
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

  return (
    <div className="settings-panel">
      <h1>Settings</h1>

      <section className="settings-section">
        <h2>LLM Provider</h2>
        {PROVIDER_MODES.map((mode) => (
          <div className="radio-option" key={mode.value}>
            <input
              type="radio"
              id={`provider-${mode.value}`}
              name="provider_mode"
              value={mode.value}
              checked={settings.provider_mode === mode.value}
              onChange={() =>
                setSettings((prev) => ({ ...prev, provider_mode: mode.value }))
              }
            />
            <div>
              <label htmlFor={`provider-${mode.value}`}>{mode.label}</label>
              <p>{mode.description}</p>
            </div>
          </div>
        ))}
      </section>

      <section className="settings-section">
        <h2>Models</h2>
        <p className="settings-field-hint">
          Generation models: only the one matching the provider mode above is
          actually used — both are kept here so switching modes doesn't lose the
          other side's value. Embedding models are fixed and can't be edited:
          they're matched to the same vector size (384 dimensions) on both
          sides, so switching provider mode never breaks the vector store with a
          size mismatch. The two are still different embedding spaces though, so
          switching modes still calls for "Reset all data" below and a
          re-ingestion run afterward.
        </p>

        <div className="model-field">
          <label htmlFor="local-generation-model">Local generation model</label>
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
          <label htmlFor="local-embedding-model">
            Local embedding model{" "}
            <span className="model-field-fixed">(fixed)</span>
          </label>
          <input
            id="local-embedding-model"
            className="model-select"
            type="text"
            disabled
            value={settings.local_embedding_model}
            readOnly
          />
        </div>

        <div className="model-field">
          <label htmlFor="cloud-generation-model">Cloud generation model</label>
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
          <label htmlFor="cloud-embedding-model">
            Cloud embedding model{" "}
            <span className="model-field-fixed">(fixed)</span>
          </label>
          <input
            id="cloud-embedding-model"
            className="model-select"
            type="text"
            disabled
            value={settings.cloud_embedding_model}
            readOnly
          />
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-section-header">
          <h2>Local folders to watch</h2>
          <button
            type="button"
            className="reverify-button"
            onClick={handleBrowseFolder}
            disabled={isBrowsing}
          >
            {isBrowsing ? "Browsing…" : "Browse…"}
          </button>
        </div>
        <p className="settings-field-hint">
          One folder path per line — paste a path, or use "Browse…" to pick one.
          Files added or changed in these folders are picked up on the next
          ingestion run.
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
          One page or database ID per line. Leave empty to ingest every page the
          Notion integration can see.
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
        <div className="settings-section-header">
          <h2>Data Ingestion</h2>
          <button
            type="button"
            className="reverify-button"
            onClick={handleTriggerIngestion}
            disabled={isTriggeringIngest}
          >
            {isTriggeringIngest ? "Starting…" : "Run ingestion now"}
          </button>
        </div>
        <p className="settings-field-hint">
          Normally runs once a day on a schedule. This starts a run immediately,
          outside the schedule — useful right after changing the folders/pages
          above. A notification appears once it finishes, even from another
          screen.
        </p>
      </section>

      <section className="settings-section">
        <div className="settings-section-header">
          <h2>Connected Data Sources</h2>
          <button
            type="button"
            className="reverify-button"
            onClick={handleReverify}
            disabled={isVerifying}
          >
            {isVerifying ? "Checking…" : "Reverify"}
          </button>
        </div>
        {connections.connections.length > 0 && (
          <p className="connections-checked-at">
            Last checked:{" "}
            {new Date(connections.connections[0].checked_at).toLocaleString()}
          </p>
        )}
        <div className="sources-grid">
          {connections.connections.map((connection) => {
            const sourceCounts = sources.sources.find(
              (source) => source.source_type === connection.source_type,
            );
            return (
              <div
                className="source-status-card"
                key={connection.source_type}
                title={connection.detail ?? undefined}
              >
                <span>{connection.source_type.replace("_", " ")}</span>
                <span className="source-status-value">
                  <span className={`status-dot ${connection.status}`} />
                  {CONNECTION_LABELS[connection.status] ?? connection.status}
                  {connection.status === "ok" && sourceCounts !== undefined && (
                    <span className="source-item-count">
                      ({sourceCounts.total_items} total
                      {sourceCounts.items_processed > 0
                        ? `, ${sourceCounts.items_processed} new`
                        : ""}
                      )
                    </span>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      </section>

      <section className="settings-section danger-zone">
        <h2>Danger Zone</h2>
        <p className="settings-field-hint">
          Permanently deletes every ingested item, embedding, and relationship.
          Use this to recover from a bad ingestion run or to restart from a
          clean slate — there is no undo.
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

      <button
        type="button"
        className="save-button"
        onClick={handleSave}
        disabled={isSaving}
      >
        {isSaving ? "Saving…" : "Save Changes"}
      </button>
      {saveStatus && (
        <span className={`save-status ${saveStatus.ok ? "" : "error"}`}>
          {saveStatus.text}
        </span>
      )}
    </div>
  );
}

export default SettingsPanel;
