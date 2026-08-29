import { useRef, useState } from "react";
import { postQuery } from "../api/client.js";
import MessageBubble from "./MessageBubble.jsx";

/**
 * The active conversation: message history plus the input box pinned to
 * the bottom. Per docs/UIUX_Wireframes.docx section 2 (Screen 1) and
 * section 4 (Interaction Flow).
 *
 * @param {object} props
 * @param {string | null} props.sessionId - current session, or null for a
 *   not-yet-started one (the first question in it starts a new session)
 * @param {(sessionId: string) => void} props.onSessionStarted - called
 *   once the first response in a new session comes back, with the
 *   session_id the backend assigned
 */
function ChatWindow({ sessionId, onSessionStarted }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const nextMessageId = useRef(0);

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
      if (!sessionId) {
        onSessionStarted(result.session_id);
      }
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
        {messages.length === 0 && (
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
