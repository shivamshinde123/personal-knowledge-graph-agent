import chatIcon from "../assets/icons/nav-chat.svg";
import graphIcon from "../assets/icons/nav-graph.svg";
import settingsIcon from "../assets/icons/nav-settings.svg";

/**
 * Session list, "New chat" button, and primary navigation (Chat / Graph /
 * Settings). Per docs/UIUX_Wireframes.docx section 2.1, restyled per the
 * Figma redesign (see DECISIONS.md) — a fixed dark theme with a
 * translucent, blurred panel and a purple "active" pill on the current
 * nav tab.
 *
 * @param {object} props
 * @param {"chat" | "settings" | "graph"} props.view
 * @param {Array<{session_id: string, title: string | null, updated_at: string}>} props.sessions
 * @param {string | null} props.activeSessionId
 * @param {() => void} props.onNewChat
 * @param {(sessionId: string) => void} props.onSelectSession
 * @param {() => void} props.onOpenChat - switches to the chat view without
 *   starting a new conversation (the current session, if any, stays
 *   active) -- the design's "Chat" nav tab is a separate action from "New
 *   chat", which the app previously had no dedicated way to do.
 * @param {() => void} props.onOpenSettings
 * @param {() => void} props.onOpenGraph
 */
function Sidebar({
  view,
  sessions,
  activeSessionId,
  onNewChat,
  onSelectSession,
  onOpenChat,
  onOpenSettings,
  onOpenGraph,
}) {
  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <h1 className="sidebar-title">Knowledge Agent</h1>
        <p className="sidebar-subtitle">Ambient Intelligence</p>
      </div>
      <button type="button" className="new-chat-button" onClick={onNewChat}>
        New chat
      </button>
      <div className="session-list">
        <p className="session-list-label">Recent</p>
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
              title={session.title || "Untitled conversation"}
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
          onClick={onOpenChat}
          aria-current={view === "chat" ? "page" : undefined}
        >
          <span
            className="nav-link-icon"
            style={{
              maskImage: `url(${chatIcon})`,
              WebkitMaskImage: `url(${chatIcon})`,
            }}
            aria-hidden="true"
          />
          Chat
        </button>
        <button
          type="button"
          className="nav-link"
          onClick={onOpenGraph}
          aria-current={view === "graph" ? "page" : undefined}
        >
          <span
            className="nav-link-icon"
            style={{
              maskImage: `url(${graphIcon})`,
              WebkitMaskImage: `url(${graphIcon})`,
            }}
            aria-hidden="true"
          />
          Graph
        </button>
        <button
          type="button"
          className="nav-link"
          onClick={onOpenSettings}
          aria-current={view === "settings" ? "page" : undefined}
        >
          <span
            className="nav-link-icon"
            style={{
              maskImage: `url(${settingsIcon})`,
              WebkitMaskImage: `url(${settingsIcon})`,
            }}
            aria-hidden="true"
          />
          Settings
        </button>
      </div>
    </nav>
  );
}

export default Sidebar;
