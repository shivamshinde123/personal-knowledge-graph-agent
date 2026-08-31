import { useCallback, useEffect, useRef, useState } from "react";
import {
  getSessions,
  getSourcesStatus,
  postAdminReset,
  postIngestCancel,
  postIngestTrigger,
} from "./api/client.js";
import ChatWindow from "./components/ChatWindow.jsx";
import GraphView from "./components/GraphView.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import Sidebar from "./components/Sidebar.jsx";
import Toasts from "./components/Toasts.jsx";

const INGEST_POLL_INTERVAL_MS = 4000;
// ~6 minutes — a local, single-item-at-a-time ingestion run should finish
// well before this; past it we stop polling and tell the user to check
// back, rather than polling forever in the background.
const INGEST_POLL_MAX_ATTEMPTS = 90;

let nextToastId = 1;

/**
 * Top-level layout: sidebar plus one of the chat window (Screen 1), the
 * settings panel (Screen 2), or the relationship graph view (Screen 3 —
 * extension beyond docs/UIUX_Wireframes.docx, see DECISIONS.md).
 *
 * There's no router — just screens switched via local state, per the
 * wireframe's "Settings link" navigation (no deep-linking requirement).
 *
 * Owns the ingestion-trigger/reset-all flows (rather than SettingsPanel
 * owning them itself) for two reasons: a triggered ingestion run finishes
 * in the background, well after the request that started it returns, so
 * something that outlives SettingsPanel's own mount needs to poll for
 * completion and toast the result even if the user has since navigated
 * away — and a reset wipes conversation history too (sessions/messages,
 * not just ingested items), which only App.jsx's own session state can
 * react to. See DECISIONS.md.
 */
function App() {
  const [view, setView] = useState("chat");
  const [sessionId, setSessionId] = useState(null);
  const [sessions, setSessions] = useState([]);
  // Bumped whenever the "active session" changes (new chat, or a
  // different session picked from the sidebar) to force ChatWindow to
  // remount with fresh state, since it decides once, at mount, whether to
  // load prior history — see ChatWindow's own docstring.
  const [chatKey, setChatKey] = useState(0);
  const [toasts, setToasts] = useState([]);
  // { startedAt: Date, runId: string, dbRunId: string | null,
  // itemsProcessed: number, status: "running" | "success" |
  // "partial_failure" | "failed" | "cancelled" } | null — shown persistently
  // in Settings (not just the transient toast), so "when did this start" and
  // "how far along is it" survive longer than the 7s a toast stays up.
  // `runId` is the display label postIngestTrigger() returns; `dbRunId` is
  // the real ingestion_runs.id (only known once the first poll finds the
  // matching row) — needed for postIngestCancel(), which looks a run up by
  // its actual id, not the display label. See DECISIONS.md.
  const [ingestionStatus, setIngestionStatus] = useState(null);
  const ingestPollTimeout = useRef(null);

  const refreshSessions = useCallback(() => {
    getSessions()
      .then((result) => setSessions(result.sessions))
      .catch(() => {
        // Sidebar just keeps its last-known list; not worth surfacing an
        // error for a background refresh.
      });
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  useEffect(() => {
    // Only cleanup on unmount — App.jsx itself never unmounts in practice,
    // but this keeps a stray poll from outliving a hot-reload in dev.
    return () => clearTimeout(ingestPollTimeout.current);
  }, []);

  const addToast = useCallback((kind, text) => {
    const id = nextToastId++;
    setToasts((prev) => [...prev, { id, kind, text }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((toast) => toast.id !== id));
    }, 7000);
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  // Not wrapped in useCallback: it recurses via setTimeout (not a direct
  // self-call), so memoizing it buys nothing and the lint rule for
  // self-referencing useCallback would need to be turned off instead. A
  // plain function recreated each render is fine here — it's called from
  // event handlers and timeouts, never as a render-time dependency.
  function pollIngestionCompletion(triggeredAt, runId, attempt = 0) {
    getSourcesStatus()
      .then((result) => {
        const lastRun = result.last_run;
        // A couple of seconds of slack for clock skew between this
        // browser and the machine running the batch — both are the same
        // machine in this local, single-user system, so skew is minimal,
        // but the comparison is otherwise exact.
        const isThisRun =
          lastRun &&
          new Date(lastRun.started_at).getTime() >= triggeredAt - 2000;

        if (isThisRun) {
          setIngestionStatus({
            startedAt: new Date(triggeredAt),
            runId,
            dbRunId: lastRun.run_id,
            itemsProcessed: lastRun.items_processed,
            status: lastRun.status,
            currentItem: lastRun.current_item,
          });
        }

        if (isThisRun && lastRun.status !== "running") {
          if (lastRun.status === "cancelled") {
            addToast(
              "info",
              `Ingestion stopped — ${lastRun.items_processed} item(s) processed.`,
            );
            return;
          }
          const succeeded = lastRun.status === "success";
          addToast(
            succeeded ? "success" : "error",
            succeeded
              ? `Ingestion completed — ${lastRun.items_processed} item(s) processed.`
              : `Ingestion finished with errors (${lastRun.status}) — ${lastRun.items_processed} item(s) processed.`,
          );
          return;
        }
        if (attempt >= INGEST_POLL_MAX_ATTEMPTS) {
          addToast(
            "info",
            "Ingestion is taking longer than expected — check Settings for status.",
          );
          return;
        }
        ingestPollTimeout.current = setTimeout(
          () => pollIngestionCompletion(triggeredAt, runId, attempt + 1),
          INGEST_POLL_INTERVAL_MS,
        );
      })
      .catch(() => {
        // A transient network error while polling isn't worth its own
        // toast — just retry on the same schedule.
        if (attempt < INGEST_POLL_MAX_ATTEMPTS) {
          ingestPollTimeout.current = setTimeout(
            () => pollIngestionCompletion(triggeredAt, runId, attempt + 1),
            INGEST_POLL_INTERVAL_MS,
          );
        }
      });
  }

  async function handleTriggerIngestion() {
    const triggeredAt = Date.now();
    const result = await postIngestTrigger();
    setIngestionStatus({
      startedAt: new Date(triggeredAt),
      runId: result.run_id,
      dbRunId: null,
      itemsProcessed: 0,
      status: "running",
      currentItem: null,
    });
    addToast("info", `Ingestion started (${result.run_id}).`);
    clearTimeout(ingestPollTimeout.current);
    pollIngestionCompletion(triggeredAt, result.run_id);
  }

  async function handleCancelIngestion(dbRunId) {
    await postIngestCancel(dbRunId);
    addToast("info", "Stopping ingestion…");
  }

  const handleResetAll = useCallback(async () => {
    await postAdminReset();
    // Reset wipes sessions/messages along with everything else — clear
    // the active chat and refresh the (now-empty) sidebar list so the UI
    // doesn't keep pointing at a session that no longer exists.
    setSessionId(null);
    setChatKey((key) => key + 1);
    refreshSessions();
    addToast("success", "All data has been reset.");
  }, [addToast, refreshSessions]);

  function handleNewChat() {
    setSessionId(null);
    setChatKey((key) => key + 1);
    setView("chat");
  }

  function handleSelectSession(id) {
    setSessionId(id);
    setChatKey((key) => key + 1);
    setView("chat");
  }

  function handleTurnCompleted(id) {
    setSessionId(id);
    refreshSessions();
  }

  return (
    <div className="app">
      <Toasts toasts={toasts} onDismiss={dismissToast} />
      <Sidebar
        view={view}
        sessions={sessions}
        activeSessionId={sessionId}
        onNewChat={handleNewChat}
        onSelectSession={handleSelectSession}
        onOpenChat={() => setView("chat")}
        onOpenSettings={() => setView("settings")}
        onOpenGraph={() => setView("graph")}
      />
      {view === "chat" && (
        <ChatWindow
          key={chatKey}
          sessionId={sessionId}
          onTurnCompleted={handleTurnCompleted}
        />
      )}
      {view === "settings" && (
        <SettingsPanel
          onTriggerIngestion={handleTriggerIngestion}
          onCancelIngestion={handleCancelIngestion}
          onResetAll={handleResetAll}
          onError={(text) => addToast("error", text)}
          ingestionStatus={ingestionStatus}
        />
      )}
      {view === "graph" && <GraphView />}
    </div>
  );
}

export default App;
