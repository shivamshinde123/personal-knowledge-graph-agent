import { useEffect, useState } from "react";
import { getSettings, getSourcesStatus, putSettings } from "../api/client.js";

const PROVIDER_MODES = [
  {
    value: "fully_local",
    label: "Fully Local",
    description:
      "Runs entirely through Ollama — no cost, no network dependency.",
  },
  {
    value: "fully_cloud",
    label: "Fully Cloud",
    description:
      "Routes every call through OpenRouter — highest quality, per-call cost.",
  },
  {
    value: "mixed",
    label: "Mixed",
    description:
      "Cheap ingestion tasks run locally; answers are synthesized via OpenRouter.",
  },
];

/**
 * The Settings screen: LLM provider mode, cloud model, and connected data
 * source status. Per docs/UIUX_Wireframes.docx section 3.
 */
function SettingsPanel() {
  const [settings, setSettings] = useState(null);
  const [sources, setSources] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [isSaving, setIsSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState(null);

  useEffect(() => {
    Promise.all([getSettings(), getSourcesStatus()])
      .then(([settingsResult, sourcesResult]) => {
        setSettings(settingsResult);
        setSources(sourcesResult);
      })
      .catch((error) => setLoadError(error.message));
  }, []);

  async function handleSave() {
    setIsSaving(true);
    setSaveStatus(null);
    try {
      const result = await putSettings({
        provider_mode: settings.provider_mode,
        cloud_model: settings.cloud_model,
      });
      setSettings(result);
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

  if (!settings || !sources) {
    return (
      <div className="settings-panel">
        <h1>Settings</h1>
        <p>Loading…</p>
      </div>
    );
  }

  const cloudModelEnabled =
    settings.provider_mode === "fully_cloud" ||
    settings.provider_mode === "mixed";

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
        <h2>Cloud model</h2>
        <input
          className="model-select"
          type="text"
          disabled={!cloudModelEnabled}
          value={settings.cloud_model}
          onChange={(event) =>
            setSettings((prev) => ({
              ...prev,
              cloud_model: event.target.value,
            }))
          }
          placeholder="e.g. anthropic/claude-sonnet-4"
        />
      </section>

      <section className="settings-section">
        <h2>Connected Data Sources</h2>
        <div className="sources-grid">
          {sources.sources.map((source) => (
            <div className="source-status-card" key={source.source_type}>
              <span>{source.source_type.replace("_", " ")}</span>
              <span>
                <span className={`status-dot ${source.status}`} />
                {source.status === "ok" ? "OK" : "Error"}
              </span>
            </div>
          ))}
        </div>
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
