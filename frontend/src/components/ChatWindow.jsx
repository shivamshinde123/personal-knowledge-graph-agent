import { useEffect, useRef, useState } from "react";
import {
  getSessionHistory,
  getSourceConnections,
  postQuery,
} from "../api/client.js";
import { SOURCE_TYPE_COLORS } from "../constants/sourceColors.js";
import MessageBubble from "./MessageBubble.jsx";
import emptyStateIcon from "../assets/icons/empty-state-icon.svg";
import summarizeIcon from "../assets/icons/chip-summarize.svg";
import findIcon from "../assets/icons/chip-find.svg";
import sendIcon from "../assets/icons/send-icon.svg";

const SOURCE_TYPE_LABELS = {
  local_file: "Local files",
  notion: "Notion",
  gmail: "Gmail",
  github: "GitHub",
  google_calendar: "Calendar",
  browser_history: "Browser history",
};

// Example starter prompts, per the Figma "Suggestion Chips" — clicking one
// fills the input rather than sending immediately, so the reader can edit
// it first.
const SUGGESTION_CHIPS = [
  {
    icon: summarizeIcon,
    label: "Summarize",
    text: "Recent project commits and emails",
    prompt: "Summarize my recent project commits and emails.",
  },
  {
    icon: findIcon,
    label: "Find",
    text: "That presentation from last week",
    prompt: "Find that presentation from last week.",
  },
];

/**
 * The active conversation: message history plus the input box pinned to
 * the bottom. Per docs/UIUX_Wireframes.docx section 2 (Screen 1) and
 * section 4 (Interaction Flow).
 *
 * Remounted (via a `key` change in App.jsx) whenever the user starts a new
 * chat or picks a different session from the sidebar, so `sessionId` here
 * is only ever read at mount time to decide whether to load prior history
 * — it isn't watched for later changes.
 *
 * @param {object} props
 * @param {string | null} props.sessionId - an existing session to load and
 *   continue, or null to start a new one
 * @param {(sessionId: string) => void} props.onTurnCompleted - called
 *   after every successful answer, with the session's id (new or
 *   existing), so the sidebar can refresh its list/ordering
 */
function ChatWindow({ sessionId: initialSessionId, onTurnCompleted }) {
  const [sessionId, setSessionId] = useState(initialSessionId);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(!!initialSessionId);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  // Which sources are actually connected right now, for the input area's
  // "Data Source Indicators" pills (Figma). Fetched once — this is a
  // lightweight, already-cached status check (see agent/connection_check.py),
  // not worth re-fetching on every keystroke or render.
  const [connectedSources, setConnectedSources] = useState([]);
  const nextMessageId = useRef(0);
  const messageListRef = useRef(null);
  const textareaRef = useRef(null);
  // Tracks whether the reader was at the bottom *before* the message list's
  // content just changed — updated continuously by the scroll handler, so
  // it reflects the scroll position from before a new message was appended,
  // not after. Reading scroll metrics fresh inside the messages-changed
  // effect below would be too late: by then the DOM already includes the
  // new (possibly tall) message, so "distance from bottom" would measure
  // that message's own height instead of whether the reader had scrolled
  // away — a long answer would then wrongly look like "not at the bottom"
  // even when the reader was.
  const wasAtBottomRef = useRef(true);

  // How far (px) from the true bottom still counts as "at the bottom" —
  // small enough that ordinary sub-pixel scroll rounding doesn't spuriously
  // show the button, large enough to tolerate the last message's own height
  // settling in (e.g. markdown images loading).
  const NEAR_BOTTOM_THRESHOLD = 48;

  function isNearBottom() {
    const el = messageListRef.current;
    if (!el) return true;
    return (
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_THRESHOLD
    );
  }

  function scrollToBottom(behavior = "smooth") {
    const el = messageListRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    wasAtBottomRef.current = true;
    setShowScrollToBottom(false);
  }

  function handleMessageListScroll() {
    const atBottom = isNearBottom();
    wasAtBottomRef.current = atBottom;
    setShowScrollToBottom(!atBottom);
  }

  // Keeps the reader pinned to the newest message as the conversation
  // grows (a new question, or the pending "Thinking…" bubble resolving
  // into the real answer) -- but only while they were already at the
  // bottom; someone scrolled up to reread something earlier shouldn't get
  // yanked back down by an unrelated answer streaming in. Also covers the
  // initial history load finishing, so an existing conversation opens
  // already scrolled to its latest message rather than its first.
  useEffect(() => {
    if (wasAtBottomRef.current) scrollToBottom("auto");
    else setShowScrollToBottom(true);
  }, [messages, isLoadingHistory]);

  useEffect(() => {
    let cancelled = false;
    getSourceConnections()
      .then((result) => {
        if (cancelled) return;
        setConnectedSources(
          result.connections
            .filter((connection) => connection.status === "ok")
            .map((connection) => connection.source_type),
        );
      })
      .catch(() => {
        // Not worth its own error state -- the pills just stay empty.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!initialSessionId) {
      return;
    }
    getSessionHistory(initialSessionId)
      .then((result) => {
        setMessages(
          result.messages.map((message) => ({
            id: nextMessageId.current++,
            role: message.role,
            text: message.text,
            sources: message.sources ?? undefined,
            status: "done",
          })),
        );
      })
      .catch(() => {
        setMessages([
          {
            id: nextMessageId.current++,
            role: "agent",
            text: "Couldn't load this conversation.",
            status: "error",
          },
        ]);
      })
      .finally(() => setIsLoadingHistory(false));
    // Runs once, at mount, using this instance's initial sessionId only —
    // see the component docstring above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSubmit(event) {
    event.preventDefault();
    const question = input.trim();
    if (!question || isSending) {
      return;
    }

    const userMessageId = nextMessageId.current++;
    const pendingMessageId = nextMessageId.current++;
    setMessages((prev) => [
      ...prev,
      { id: userMessageId, role: "user", text: question },
      {
        id: pendingMessageId,
        role: "agent",
        text: "Thinking…",
        status: "pending",
      },
    ]);
    setInput("");
    setIsSending(true);

    try {
      const result = await postQuery(question, sessionId);
      setSessionId(result.session_id);
      setMessages((prev) =>
        prev.map((message) =>
          message.id === pendingMessageId
            ? {
                ...message,
                text: result.answer,
                sources: result.sources,
                status: "done",
              }
            : message,
        ),
      );
      onTurnCompleted(result.session_id);
    } catch (error) {
      setMessages((prev) =>
        prev.map((message) =>
          message.id === pendingMessageId
            ? {
                ...message,
                text: error.message || "Something went wrong.",
                status: "error",
              }
            : message,
        ),
      );
    } finally {
      setIsSending(false);
    }
  }

  return (
    <div className="chat-window">
      <div
        className="message-list"
        ref={messageListRef}
        onScroll={handleMessageListScroll}
      >
        {isLoadingHistory && <p className="message-list-empty">Loading…</p>}
        {!isLoadingHistory && messages.length === 0 && (
          <div className="empty-state">
            <div className="empty-state-icon-box">
              <img src={emptyStateIcon} alt="" className="empty-state-icon" />
            </div>
            <h2 className="empty-state-heading">How can I help you today?</h2>
            <p className="empty-state-subtext">
              Ask a question about your notes, emails, commits, calendar, files,
              or browsing history.
            </p>
            <div className="suggestion-chips">
              {SUGGESTION_CHIPS.map((chip) => (
                <button
                  key={chip.label}
                  type="button"
                  className="suggestion-chip"
                  onClick={() => {
                    setInput(chip.prompt);
                    textareaRef.current?.focus();
                  }}
                >
                  <span className="suggestion-chip-label">
                    <img
                      src={chip.icon}
                      alt=""
                      className="suggestion-chip-icon"
                    />
                    {chip.label}
                  </span>
                  <span className="suggestion-chip-text">{chip.text}</span>
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
      </div>
      {showScrollToBottom && (
        <button
          type="button"
          className="scroll-to-bottom-button"
          onClick={() => scrollToBottom("smooth")}
          aria-label="Scroll to the latest message"
        >
          ↓ Latest
        </button>
      )}
      <div className="chat-input-area">
        <div className="chat-input-area-inner">
          {connectedSources.length > 0 && (
            <div className="data-source-indicators">
              {connectedSources.map((sourceType) => (
                <span key={sourceType} className="data-source-pill">
                  <span
                    className="data-source-dot"
                    style={{ backgroundColor: SOURCE_TYPE_COLORS[sourceType] }}
                  />
                  {SOURCE_TYPE_LABELS[sourceType] ?? sourceType}
                </span>
              ))}
            </div>
          )}
          <form className="chat-input-form" onSubmit={handleSubmit}>
            <textarea
              ref={textareaRef}
              className="chat-input"
              rows={1}
              placeholder="Ask a question…"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  handleSubmit(event);
                }
              }}
            />
            <button
              type="submit"
              className="send-button"
              disabled={isSending || !input.trim()}
            >
              Send
              <img src={sendIcon} alt="" className="send-button-icon" />
            </button>
          </form>
          <p className="chat-input-disclaimer">
            Knowledge Agent may produce inaccurate information.
          </p>
        </div>
      </div>
    </div>
  );
}

export default ChatWindow;
