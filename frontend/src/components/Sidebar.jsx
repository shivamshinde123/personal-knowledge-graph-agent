/**
 * Session list, "New chat" button, and the Settings link.
 * Per docs/UIUX_Wireframes.docx section 2.1.
 *
 * @param {object} props
 * @param {"chat" | "settings"} props.view
 * @param {Array<{session_id: string, title: string | null, updated_at: string}>} props.sessions
 * @param {string | null} props.activeSessionId
 * @param {() => void} props.onNewChat
 * @param {(sessionId: string) => void} props.onSelectSession
 * @param {() => void} props.onOpenSettings
 */
function Sidebar({
  view,
  sessions,
  activeSessionId,
  onNewChat,
  onSelectSession,
  onOpenSettings,
}) {
  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <h1 className="sidebar-title">Knowledge Agent</h1>
      </div>
      <button type="button" className="new-chat-button" onClick={onNewChat}>
        + New chat
      </button>
      <div className="session-list">
        {sessions.length === 0 ? (
          <p className="session-list-empty">No past conversations yet.</p>
        ) : (
          sessions.map((session) => (
            <button
              key={session.session_id}
              type="button"
              className={`session-item ${
                session.session_id === activeSessionId ? "active" : ""
              }`}
              onClick={() => onSelectSession(session.session_id)}
            >
              {session.title || "Untitled conversation"}
            </button>
          ))
        )}
      </div>
      <button
        type="button"
        className="settings-link"
        onClick={onOpenSettings}
        aria-current={view === "settings" ? "page" : undefined}
      >
        Settings
      </button>
    </nav>
  );
}

export default Sidebar;
