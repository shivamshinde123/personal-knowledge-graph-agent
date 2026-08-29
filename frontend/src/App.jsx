import { useCallback, useEffect, useState } from "react";
import { getSessions } from "./api/client.js";
import ChatWindow from "./components/ChatWindow.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import Sidebar from "./components/Sidebar.jsx";

/**
 * Top-level layout: sidebar plus either the chat window (Screen 1) or the
 * settings panel (Screen 2). Per docs/UIUX_Wireframes.docx.
 *
 * There's no router — just two screens, switched via local state, per the
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
      />
      {view === "chat" ? (
        <ChatWindow
          key={chatKey}
          sessionId={sessionId}
          onTurnCompleted={handleTurnCompleted}
        />
      ) : (
        <SettingsPanel />
      )}
    </div>
  );
}

export default App;
