/**
 * Session list, "New chat" button, and the Settings link.
 * Per docs/UIUX_Wireframes.docx section 2.1.
 *
 * The session list is intentionally always empty for now — there is no
 * GET /api/sessions endpoint yet (session persistence is a separate,
 * later unit of work), so this honestly shows an empty state rather than
 * fabricating session history.
 *
 * @param {object} props
 * @param {"chat" | "settings"} props.view
 * @param {() => void} props.onNewChat
 * @param {() => void} props.onOpenSettings
 */
function Sidebar({ view, onNewChat, onOpenSettings }) {
  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <h1 className="sidebar-title">Knowledge Agent</h1>
      </div>
      <button type="button" className="new-chat-button" onClick={onNewChat}>
        + New chat
      </button>
      <div className="session-list">
        <p className="session-list-empty">No past conversations yet.</p>
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
