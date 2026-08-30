import { useCallback, useEffect, useState } from "react";
import { getSessions } from "./api/client.js";
import ChatWindow from "./components/ChatWindow.jsx";
import GraphView from "./components/GraphView.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import Sidebar from "./components/Sidebar.jsx";

/**
 * Top-level layout: sidebar plus one of the chat window (Screen 1), the
 * settings panel (Screen 2), or the relationship graph view (Screen 3 —
 * extension beyond docs/UIUX_Wireframes.docx, see DECISIONS.md).
 *
 * There's no router — just screens switched via local state, per the
 * wireframe's "Settings link" navigation (no deep-linking requirement).
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
      <Sidebar
        view={view}
        sessions={sessions}
        activeSessionId={sessionId}
        onNewChat={handleNewChat}
        onSelectSession={handleSelectSession}
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
      {view === "settings" && <SettingsPanel />}
      {view === "graph" && <GraphView />}
    </div>
  );
}

export default App;
