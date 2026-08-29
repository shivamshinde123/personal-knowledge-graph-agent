import { useState } from "react";
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
  // Bumped on "New chat" to force ChatWindow to remount with fresh state,
  // since its own message history is local component state.
  const [chatKey, setChatKey] = useState(0);

  function handleNewChat() {
    setSessionId(null);
    setChatKey((key) => key + 1);
    setView("chat");
  }

  return (
    <div className="app">
      <Sidebar
        view={view}
        onNewChat={handleNewChat}
        onOpenSettings={() => setView("settings")}
      />
      {view === "chat" ? (
        <ChatWindow
          key={chatKey}
          sessionId={sessionId}
          onSessionStarted={setSessionId}
        />
      ) : (
        <SettingsPanel />
      )}
    </div>
  );
}

export default App;
