import SourceChip from "./SourceChip.jsx";

/**
 * One message in the conversation: a user question (right-aligned) or an
 * agent answer (left-aligned), with source chips for a cited agent answer.
 * Per docs/UIUX_Wireframes.docx section 2.1.
 */
function MessageBubble({ message }) {
  const { role, text, sources, status } = message;
  const isUser = role === "user";

  return (
    <div className={`message-row ${isUser ? "user" : "agent"}`}>
      <div>
        <div
          className={`message-bubble ${isUser ? "user" : "agent"} ${
            status === "pending" ? "pending" : ""
          } ${status === "error" ? "error" : ""}`}
        >
          {text}
        </div>
        {!isUser && sources && sources.length > 0 && (
          <div className="message-sources">
            {sources.map((source) => (
              <SourceChip key={source.item_id} source={source} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default MessageBubble;
