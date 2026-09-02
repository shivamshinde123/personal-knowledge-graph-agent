import { useEffect, useRef, useState } from "react";
import {
  getGoogleOAuthStatus,
  getSettings,
  getSetupHostDataFiles,
  getSourceConfig,
  postBrowseFolder,
  postGoogleOAuthStart,
  postSetupCredentials,
  postSetupValidate,
  putSettings,
  putSourceConfig,
} from "../api/client.js";
import { useConfirm } from "../hooks/useConfirm.jsx";

// "openrouter" only appears when Cloud mode is picked on the provider
// step — see visibleSteps() below. Every step after "provider" is
// individually optional (no source is required — see CLAUDE.md); "Skip"
// on any of them just moves on without saving anything for that step.
const ALL_STEP_IDS = [
  "welcome",
  "provider",
  "openrouter",
  "google",
  "notion",
  "github",
  "local_files",
  "browser_history",
  "done",
];

function visibleSteps(providerMode) {
  return providerMode === "fully_cloud"
    ? ALL_STEP_IDS
    : ALL_STEP_IDS.filter((id) => id !== "openrouter");
}

const STEP_TITLES = {
  welcome: "Welcome",
  provider: "Provider Mode",
  openrouter: "OpenRouter",
  google: "Gmail & Calendar",
  notion: "Notion",
  github: "GitHub",
  local_files: "Local Files",
  browser_history: "Browser History",
  done: "All Set",
};

const CREDENTIAL_STEP_CONFIG = {
  openrouter: {
    apiField: "openrouter_api_key",
    label: "OpenRouter API key",
    placeholder: "sk-or-v1-...",
    help: (
      <>
        Cloud mode routes generation and embedding through OpenRouter. Create
        a key at{" "}
        <a href="https://openrouter.ai/keys" target="_blank" rel="noreferrer">
          openrouter.ai/keys
        </a>
        .
      </>
    ),
  },
  notion: {
    apiField: "notion_api_key",
    label: "Notion integration secret",
    placeholder: "ntn_...",
    help: (
      <>
        Create an internal integration at{" "}
        <a
          href="https://www.notion.so/my-integrations"
          target="_blank"
          rel="noreferrer"
        >
          notion.so/my-integrations
        </a>{" "}
        and share the pages you want ingested with it.
      </>
    ),
  },
  github: {
    apiField: "github_token",
    label: "GitHub personal access token",
    placeholder: "ghp_...",
    help: (
      <>
        A fine-grained or classic token with read access to the repos you
        want ingested. Create one at{" "}
        <a
          href="https://github.com/settings/tokens"
          target="_blank"
          rel="noreferrer"
        >
          github.com/settings/tokens
        </a>
        .
      </>
    ),
  },
};

/**
 * Guided first-run setup wizard — the last remaining piece of issue #92.
 * Ties together, as a single linear flow, capability that already existed
 * as scattered Settings-screen fields plus the three genuinely new
 * endpoints issue #92 added (Google OAuth, validate-before-save
 * credentials, the mounted-files pick lists): provider mode, Google
 * (Gmail + Calendar) OAuth, Notion/GitHub/OpenRouter credentials, local
 * files, and browser history.
 *
 * Launched from a button in SettingsPanel (not shown automatically on
 * first run — there's no "is this a first run" signal on the backend to
 * key that off, and an unprompted modal on every load of an
 * already-configured install would be worse than a discoverable button).
 * See DECISIONS.md.
 *
 * Every step past "provider" is individually skippable — no data source
 * is required (per CLAUDE.md, "only sources with fully automatable
 * ingestion are included" and none of the six is mandatory). Revisiting
 * the wizard on an already-configured install pre-fills each step from
 * whatever's already saved, so it doubles as "review my setup," not just
 * a one-time flow.
 *
 * @param {object} props
 * @param {() => void} props.onClose
 * @param {() => Promise<void>} props.onTriggerIngestion - same handler
 *   App.jsx passes to SettingsPanel, reused here for the "Run ingestion
 *   now" button on the final step.
 * @param {() => Promise<void>} props.onResetAll - same handler App.jsx
 *   passes to SettingsPanel; only invoked here if the provider mode step
 *   actually changes the mode, since that strands existing embeddings.
 * @param {(text: string) => void} props.onError
 */
function SetupWizard({ onClose, onTriggerIngestion, onResetAll, onError }) {
  const [loaded, setLoaded] = useState(false);
  const [currentStepId, setCurrentStepId] = useState("welcome");

  const [initialProviderMode, setInitialProviderMode] = useState("fully_local");
  const [providerMode, setProviderMode] = useState("fully_local");

  const [credentials, setCredentials] = useState({
    openrouter: { value: "", status: "idle", detail: "" },
    notion: { value: "", status: "idle", detail: "" },
    github: { value: "", status: "idle", detail: "" },
  });

  const [google, setGoogle] = useState({
    clientId: "",
    clientSecret: "",
    connected: false,
    connecting: false,
    error: null,
  });
  const googlePollTimeout = useRef(null);

  const [sourceConfig, setSourceConfig] = useState(null);
  const [watchDirs, setWatchDirs] = useState([]);
  const [isBrowsing, setIsBrowsing] = useState(false);
  const [browserHistoryPath, setBrowserHistoryPath] = useState("");
  const [hostDataFiles, setHostDataFiles] = useState([]);

  const [isFinishing, setIsFinishing] = useState(false);
  const [confirm, confirmDialog] = useConfirm();

  useEffect(() => {
    Promise.all([
      getSettings(),
      getSourceConfig(),
      getGoogleOAuthStatus(),
    ])
      .then(([settingsResult, sourceConfigResult, googleStatus]) => {
        setInitialProviderMode(settingsResult.provider_mode);
        setProviderMode(settingsResult.provider_mode);
        setSourceConfig(sourceConfigResult);
        setWatchDirs(sourceConfigResult.local_files_watch_dirs);
        setBrowserHistoryPath(sourceConfigResult.browser_history_path ?? "");
        setGoogle((g) => ({ ...g, connected: googleStatus.connected }));
        if (sourceConfigResult.running_in_docker) {
          getSetupHostDataFiles()
            .then((result) => setHostDataFiles(result.files))
            .catch(() => setHostDataFiles([]));
        }
      })
      .catch((error) => onError(`Could not load current setup: ${error.message}`))
      .finally(() => setLoaded(true));
  }, [onError]);

  useEffect(() => () => clearTimeout(googlePollTimeout.current), []);

  const steps = visibleSteps(providerMode);
  const currentIndex = steps.indexOf(currentStepId);

  function goTo(stepId) {
    setCurrentStepId(stepId);
  }

  function goNext() {
    goTo(steps[Math.min(currentIndex + 1, steps.length - 1)]);
  }

  function goBack() {
    goTo(steps[Math.max(currentIndex - 1, 0)]);
  }

  async function handleProviderNext() {
    if (providerMode === initialProviderMode) {
      goNext();
      return;
    }
    const confirmed = await confirm({
      title: "Switch provider mode?",
      body: (
        <p className="confirm-dialog-warning">
          Local and Cloud use different embedding models — switching wipes
          all ingested data (SQLite, Chroma, and Neo4j) since existing
          embeddings won't match the new model. Re-ingestion won't start
          automatically; run it yourself once you've finished this wizard.
        </p>
      ),
      danger: true,
      confirmLabel: "Switch and continue",
    });
    if (!confirmed) {
      setProviderMode(initialProviderMode);
      return;
    }
    try {
      await putSettings({ provider_mode: providerMode });
      await onResetAll();
      setInitialProviderMode(providerMode);
      goNext();
    } catch (error) {
      onError(`Could not switch provider mode: ${error.message}`);
    }
  }

  function updateCredentialValue(source, value) {
    setCredentials((prev) => ({
      ...prev,
      [source]: { ...prev[source], value, status: "idle", detail: "" },
    }));
  }

  async function handleValidateAndSaveCredential(source) {
    const { apiField } = CREDENTIAL_STEP_CONFIG[source];
    const value = credentials[source].value.trim();
    if (!value) return;
    setCredentials((prev) => ({
      ...prev,
      [source]: { ...prev[source], status: "checking", detail: "" },
    }));
    try {
      const result = await postSetupValidate(source, value);
      if (result.status !== "ok") {
        setCredentials((prev) => ({
          ...prev,
          [source]: { ...prev[source], status: "error", detail: result.detail },
        }));
        return;
      }
      await postSetupCredentials({ [apiField]: value });
      setCredentials((prev) => ({
        ...prev,
        [source]: { ...prev[source], status: "ok", detail: result.detail },
      }));
    } catch (error) {
      setCredentials((prev) => ({
        ...prev,
        [source]: { ...prev[source], status: "error", detail: error.message },
      }));
    }
  }

  function pollGoogleStatus(popup, attempt) {
    getGoogleOAuthStatus()
      .then((result) => {
        if (result.connected) {
          setGoogle((g) => ({ ...g, connected: true, connecting: false }));
          popup?.close();
          return;
        }
        if (popup && popup.closed) {
          setGoogle((g) => ({ ...g, connecting: false }));
          return;
        }
        // ~2 minutes at 2s intervals — a real consent flow finishes in
        // seconds; past this, the user likely abandoned the popup.
        if (attempt >= 60) {
          setGoogle((g) => ({
            ...g,
            connecting: false,
            error: "Timed out waiting for Google sign-in.",
          }));
          return;
        }
        googlePollTimeout.current = setTimeout(
          () => pollGoogleStatus(popup, attempt + 1),
          2000,
        );
      })
      .catch(() => {
        if (attempt < 60) {
          googlePollTimeout.current = setTimeout(
            () => pollGoogleStatus(popup, attempt + 1),
            2000,
          );
        }
      });
  }

  async function handleConnectGoogle() {
    setGoogle((g) => ({ ...g, connecting: true, error: null }));
    try {
      const result = await postGoogleOAuthStart(
        google.clientId.trim(),
        google.clientSecret.trim(),
      );
      const popup = window.open(
        result.authorization_url,
        "google-oauth",
        "width=520,height=640",
      );
      pollGoogleStatus(popup, 0);
    } catch (error) {
      setGoogle((g) => ({ ...g, connecting: false, error: error.message }));
    }
  }

  async function saveWatchDirs(updated) {
    try {
      const result = await putSourceConfig({ local_files_watch_dirs: updated });
      setWatchDirs(result.local_files_watch_dirs);
    } catch (error) {
      onError(`Could not save watched folders: ${error.message}`);
    }
  }

  async function handleBrowseFolder() {
    setIsBrowsing(true);
    try {
      const { path } = await postBrowseFolder();
      if (path === null || watchDirs.includes(path)) return;
      await saveWatchDirs([...watchDirs, path]);
    } catch (error) {
      onError(`Could not open the folder picker: ${error.message}`);
    } finally {
      setIsBrowsing(false);
    }
  }

  function handleRemoveWatchDir(dir) {
    saveWatchDirs(watchDirs.filter((d) => d !== dir));
  }

  function handleToggleWatchDir(dir, checked) {
    saveWatchDirs(
      checked ? [...watchDirs, dir] : watchDirs.filter((d) => d !== dir),
    );
  }

  async function handleSaveBrowserHistoryPath(value) {
    setBrowserHistoryPath(value);
    if (value === (sourceConfig?.browser_history_path ?? "")) return;
    try {
      const result = await putSourceConfig({ browser_history_path: value });
      setBrowserHistoryPath(result.browser_history_path ?? "");
    } catch (error) {
      onError(`Could not save browser history path: ${error.message}`);
    }
  }

  async function handleFinish(runIngestion) {
    setIsFinishing(true);
    try {
      if (runIngestion) await onTriggerIngestion();
      onClose();
    } catch (error) {
      onError(`Could not start ingestion: ${error.message}`);
      setIsFinishing(false);
    }
  }

  function renderCredentialStep(source) {
    const config = CREDENTIAL_STEP_CONFIG[source];
    const state = credentials[source];
    return (
      <div className="wizard-step-body">
        <p className="settings-field-hint">{config.help}</p>
        <label className="wizard-field-label" htmlFor={`wizard-${source}`}>
          {config.label}
        </label>
        <input
          id={`wizard-${source}`}
          type="password"
          className="model-select"
          value={state.value}
          placeholder={config.placeholder}
          onChange={(event) => updateCredentialValue(source, event.target.value)}
        />
        <button
          type="button"
          className="settings-ghost-button wizard-inline-button"
          onClick={() => handleValidateAndSaveCredential(source)}
          disabled={!state.value.trim() || state.status === "checking"}
        >
          {state.status === "checking" ? "Checking…" : "Validate & Save"}
        </button>
        {state.status === "ok" && (
          <p className="save-status">Connected — {state.detail}</p>
        )}
        {state.status === "error" && (
          <p className="save-status error">{state.detail}</p>
        )}
      </div>
    );
  }

  function renderStepBody() {
    switch (currentStepId) {
      case "welcome":
        return (
          <div className="wizard-step-body">
            <p>
              This walks you through connecting the data sources the agent
              can draw from — Google (Gmail + Calendar), Notion, GitHub,
              local files, and browser history — plus which LLM provider
              powers it. Every step is optional; skip anything you don't
              use, and revisit this wizard from Settings any time to add
              more later.
            </p>
          </div>
        );

      case "provider":
        return (
          <div className="wizard-step-body">
            <p className="settings-field-hint">
              Selects both the generation and embedding model used
              everywhere. You can change this later in Settings.
            </p>
            {[
              {
                value: "fully_local",
                label: "Local",
                description:
                  "Runs entirely through Ollama — no cost, no network calls.",
              },
              {
                value: "fully_cloud",
                label: "Cloud",
                description:
                  "Routes through OpenRouter — highest quality, per-call cost.",
              },
            ].map((mode) => {
              const selected = providerMode === mode.value;
              return (
                <label
                  className={`radio-option ${selected ? "selected" : ""}`}
                  key={mode.value}
                >
                  <input
                    type="radio"
                    name="wizard-provider-mode"
                    value={mode.value}
                    checked={selected}
                    onChange={() => setProviderMode(mode.value)}
                  />
                  <span className="radio-option-dot" />
                  <span className="radio-option-body">
                    <span className="radio-option-label">{mode.label}</span>
                    <p>{mode.description}</p>
                  </span>
                </label>
              );
            })}
          </div>
        );

      case "openrouter":
      case "notion":
      case "github":
        return renderCredentialStep(currentStepId);

      case "google":
        return (
          <div className="wizard-step-body">
            <p className="settings-field-hint">
              Create a Desktop-type OAuth client in the{" "}
              <a
                href="https://console.cloud.google.com/apis/credentials"
                target="_blank"
                rel="noreferrer"
              >
                Google Cloud Console
              </a>{" "}
              with the Gmail and Calendar APIs enabled, then paste its
              Client ID and Secret below.
            </p>
            {google.connected ? (
              <p className="save-status">
                Gmail and Google Calendar are connected.
              </p>
            ) : (
              <>
                <label className="wizard-field-label" htmlFor="wizard-google-client-id">
                  Client ID
                </label>
                <input
                  id="wizard-google-client-id"
                  type="text"
                  className="model-select"
                  value={google.clientId}
                  onChange={(event) =>
                    setGoogle((g) => ({ ...g, clientId: event.target.value }))
                  }
                  placeholder="....apps.googleusercontent.com"
                />
                <label className="wizard-field-label" htmlFor="wizard-google-client-secret">
                  Client Secret
                </label>
                <input
                  id="wizard-google-client-secret"
                  type="password"
                  className="model-select"
                  value={google.clientSecret}
                  onChange={(event) =>
                    setGoogle((g) => ({ ...g, clientSecret: event.target.value }))
                  }
                  placeholder="GOCSPX-..."
                />
                <button
                  type="button"
                  className="settings-ghost-button wizard-inline-button"
                  onClick={handleConnectGoogle}
                  disabled={
                    !google.clientId.trim() ||
                    !google.clientSecret.trim() ||
                    google.connecting
                  }
                >
                  {google.connecting ? "Waiting for sign-in…" : "Connect Google"}
                </button>
                {google.error && (
                  <p className="save-status error">{google.error}</p>
                )}
              </>
            )}
          </div>
        );

      case "local_files": {
        if (!sourceConfig) return <p>Loading…</p>;
        if (sourceConfig.running_in_docker) {
          return (
            <div className="wizard-step-body">
              <p className="settings-field-hint">
                Pick which of the folders mounted via{" "}
                <code>HOST_WATCH_DIR</code> to watch.
              </p>
              {sourceConfig.available_watch_directories.length === 0 ? (
                <p className="settings-field-hint">
                  No folder is mounted — <code>HOST_WATCH_DIR</code> is unset.
                </p>
              ) : (
                <div className="watch-dir-picker">
                  {sourceConfig.available_watch_directories.map((dir) => (
                    <label className="watch-dir-picker-option" key={dir}>
                      <input
                        type="checkbox"
                        checked={watchDirs.includes(dir)}
                        onChange={(event) =>
                          handleToggleWatchDir(dir, event.target.checked)
                        }
                      />
                      <span>{dir}</span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          );
        }
        return (
          <div className="wizard-step-body">
            <p className="settings-field-hint">
              Pick one or more folders to watch. Files added or changed in
              them are picked up on the next ingestion run.
            </p>
            <button
              type="button"
              className="settings-ghost-button wizard-inline-button"
              onClick={handleBrowseFolder}
              disabled={isBrowsing}
            >
              {isBrowsing ? "Browsing…" : "Browse…"}
            </button>
            {watchDirs.length > 0 && (
              <ul className="wizard-picked-list">
                {watchDirs.map((dir) => (
                  <li key={dir}>
                    <span>{dir}</span>
                    <button
                      type="button"
                      className="settings-text-button"
                      onClick={() => handleRemoveWatchDir(dir)}
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      }

      case "browser_history": {
        if (!sourceConfig) return <p>Loading…</p>;
        if (sourceConfig.running_in_docker) {
          return (
            <div className="wizard-step-body">
              <p className="settings-field-hint">
                Pick the browser history file reachable under the mounted{" "}
                <code>HOST_DATA_DIR</code>.
              </p>
              {hostDataFiles.length === 0 ? (
                <p className="settings-field-hint">
                  No files found — <code>HOST_DATA_DIR</code> is unset, or
                  doesn't contain a browser profile.
                </p>
              ) : (
                <div className="watch-dir-picker">
                  {hostDataFiles.map((file) => (
                    <label className="watch-dir-picker-option" key={file}>
                      <input
                        type="radio"
                        name="wizard-browser-history-file"
                        checked={browserHistoryPath === file}
                        onChange={() => handleSaveBrowserHistoryPath(file)}
                      />
                      <span>{file}</span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          );
        }
        return (
          <div className="wizard-step-body">
            <p className="settings-field-hint">
              Path to your browser's history file (e.g. Chrome's{" "}
              <code>History</code> SQLite file). Only titles, URLs, and
              visit metadata are read — no page content.
            </p>
            <input
              type="text"
              className="model-select"
              value={browserHistoryPath}
              onChange={(event) => setBrowserHistoryPath(event.target.value)}
              onBlur={(event) => handleSaveBrowserHistoryPath(event.target.value)}
              placeholder={"C:\\Users\\you\\AppData\\...\\History"}
            />
          </div>
        );
      }

      case "done":
        return (
          <div className="wizard-step-body">
            <p>
              Setup's complete — you can revisit this wizard from Settings
              any time to add more sources or change how the agent's
              powered.
            </p>
          </div>
        );

      default:
        return null;
    }
  }

  if (!loaded) {
    return (
      <div className="wizard-backdrop">
        <div className="wizard-dialog">
          <p>Loading…</p>
        </div>
      </div>
    );
  }

  const isFirst = currentIndex === 0;
  const isLast = currentStepId === "done";
  const isSkippable = !["welcome", "provider", "done"].includes(currentStepId);

  return (
    <div className="wizard-backdrop">
      {confirmDialog}
      <div className="wizard-dialog" role="dialog" aria-modal="true">
        <div className="wizard-header">
          <div>
            <p className="wizard-step-count">
              Step {currentIndex + 1} of {steps.length}
            </p>
            <h2>{STEP_TITLES[currentStepId]}</h2>
          </div>
          <button
            type="button"
            className="settings-text-button"
            onClick={onClose}
          >
            Close
          </button>
        </div>

        {renderStepBody()}

        <div className="wizard-footer">
          <button
            type="button"
            className="confirm-dialog-cancel"
            onClick={goBack}
            disabled={isFirst}
          >
            Back
          </button>
          <div className="wizard-footer-right">
            {isSkippable && (
              <button
                type="button"
                className="confirm-dialog-cancel"
                onClick={goNext}
              >
                Skip
              </button>
            )}
            {currentStepId === "provider" && (
              <button
                type="button"
                className="confirm-dialog-confirm"
                onClick={handleProviderNext}
              >
                Next
              </button>
            )}
            {currentStepId === "welcome" && (
              <button
                type="button"
                className="confirm-dialog-confirm"
                onClick={goNext}
              >
                Get started
              </button>
            )}
            {isSkippable && (
              <button
                type="button"
                className="confirm-dialog-confirm"
                onClick={goNext}
              >
                Next
              </button>
            )}
            {isLast && (
              <>
                <button
                  type="button"
                  className="confirm-dialog-cancel"
                  onClick={() => handleFinish(false)}
                  disabled={isFinishing}
                >
                  Finish
                </button>
                <button
                  type="button"
                  className="confirm-dialog-confirm"
                  onClick={() => handleFinish(true)}
                  disabled={isFinishing}
                >
                  {isFinishing ? "Starting…" : "Run ingestion now"}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default SetupWizard;
