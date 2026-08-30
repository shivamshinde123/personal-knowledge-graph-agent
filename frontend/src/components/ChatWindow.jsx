import { useEffect, useRef, useState } from "react";
import { getSessionHistory, postQuery } from "../api/client.js";
import MessageBubble from "./MessageBubble.jsx";

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
  const nextMessageId = useRef(0);
  const messageListRef = useRef(null);
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
          <p className="message-list-empty">
            Ask a question about your notes, emails, commits, calendar, files,
            or browsing history.
          </p>
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
      <form className="chat-input-form" onSubmit={handleSubmit}>
        <textarea
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
        </button>
      </form>
    </div>
  );
}

export default ChatWindow;
