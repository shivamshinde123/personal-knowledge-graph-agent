/**
 * Session list, "New chat" button, and the Settings link.
 * Per docs/UIUX_Wireframes.docx section 2.1.
 *
 * @param {object} props
 * @param {"chat" | "settings" | "graph"} props.view
 * @param {Array<{session_id: string, title: string | null, updated_at: string}>} props.sessions
 * @param {string | null} props.activeSessionId
 * @param {() => void} props.onNewChat
 * @param {(sessionId: string) => void} props.onSelectSession
 * @param {() => void} props.onOpenSettings
 * @param {() => void} props.onOpenGraph
 */
function Sidebar({
  view,
  sessions,
  activeSessionId,
  onNewChat,
  onSelectSession,
  onOpenSettings,
  onOpenGraph,
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
      <div className="sidebar-footer">
        <button
          type="button"
          className="nav-link"
          onClick={onOpenGraph}
          aria-current={view === "graph" ? "page" : undefined}
        >
          <svg
            className="nav-link-icon"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <circle cx="5" cy="6" r="3" />
            <circle cx="19" cy="6" r="3" />
            <circle cx="12" cy="18" r="3" />
            <path d="M7.5 7.5 10 16" />
            <path d="M16.5 7.5 14 16" />
          </svg>
          Graph
        </button>
        <button
          type="button"
          className="nav-link"
          onClick={onOpenSettings}
          aria-current={view === "settings" ? "page" : undefined}
        >
          <svg
            className="nav-link-icon"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
          Settings
        </button>
      </div>
    </nav>
  );
}

export default Sidebar;
