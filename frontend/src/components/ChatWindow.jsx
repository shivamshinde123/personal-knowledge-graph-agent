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
  const nextMessageId = useRef(0);

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
      <div className="message-list">
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
