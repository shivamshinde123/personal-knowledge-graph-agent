import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import SourceChip from "./SourceChip.jsx";

/**
 * One message in the conversation: a user question (right-aligned) or an
 * agent answer (left-aligned), with source chips for a cited agent answer.
 * Per docs/UIUX_Wireframes.docx section 2.1.
 *
 * The user's own question is shown as plain text (it's exactly what they
 * typed, not markdown). The agent's answer is rendered as markdown — the
 * synthesis prompt produces headings/bullets/bold/citations, and showing
 * that raw (literal `**`/`##` characters) was hard to read; a link opens
 * in a new tab since it's leaving the app.
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
          {isUser || status === "error" ? (
            text
          ) : (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: (props) => (
                  <a {...props} target="_blank" rel="noreferrer" />
                ),
              }}
            >
              {text}
            </ReactMarkdown>
          )}
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
