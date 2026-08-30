/**
 * A small stack of dismissible, auto-expiring notifications — pinned to
 * the top-right corner, rendered above whichever screen is active so a
 * background action (ingestion, a reset) can report its outcome no matter
 * which view the user has since navigated to. Owned by App.jsx, since
 * that's the one component that never unmounts. See DECISIONS.md.
 *
 * @param {object} props
 * @param {Array<{id: number, kind: "success" | "error" | "info", text: string}>} props.toasts
 * @param {(id: number) => void} props.onDismiss
 */
function Toasts({ toasts, onDismiss }) {
  if (toasts.length === 0) return null;

  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast-${toast.kind}`}>
          <span>{toast.text}</span>
          <button
            type="button"
            className="toast-dismiss"
            onClick={() => onDismiss(toast.id)}
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export default Toasts;
