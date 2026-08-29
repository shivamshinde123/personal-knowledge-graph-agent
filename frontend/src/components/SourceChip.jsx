/**
 * One cited source. Clicking opens the original item in a new tab.
 * Per docs/UIUX_Wireframes.docx section 2.1 ("Source chips").
 */
function SourceChip({ source }) {
  const { source_type: sourceType, title, url } = source;

  const content = (
    <>
      <span className="source-chip-type">{sourceType.replace("_", " ")}</span>
      <span>{title || "Untitled"}</span>
    </>
  );

  if (!url) {
    return <span className="source-chip">{content}</span>;
  }

  return (
    <a
      className="source-chip"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
    >
      {content}
    </a>
  );
}

export default SourceChip;
