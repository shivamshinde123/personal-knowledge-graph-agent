import { useEffect, useRef } from "react";

/**
 * A centered modal confirm/cancel dialog, styled to match the rest of the
 * app — replaces the native `window.confirm()`, which looks nothing like
 * the rest of the frontend and can't render structured content (a bullet
 * list of changes, a highlighted warning) the way this can. See
 * DECISIONS.md.
 *
 * Not used directly — see `useConfirm()` (../hooks/useConfirm.jsx), which
 * owns showing and dismissing it.
 *
 * @param {object} props
 * @param {string} [props.title]
 * @param {import("react").ReactNode} props.body - plain text or richer
 *   JSX (e.g. a bullet list, a highlighted warning paragraph)
 * @param {boolean} [props.danger] - styles the confirm button red instead
 *   of the accent color, for destructive/high-consequence actions
 * @param {boolean} [props.critical] - the most severe visual treatment in
 *   the app: the whole dialog (not just the confirm button) renders on a
 *   solid red background, for actions more consequential than a normal
 *   "danger" prompt — currently only the provider-mode switch (Local <->
 *   Cloud), which silently invalidates every existing embedding and
 *   triggers an automatic full data wipe. Implies `danger`-equivalent
 *   button styling. See DECISIONS.md.
 * @param {string} [props.confirmLabel]
 * @param {string} [props.cancelLabel]
 * @param {() => void} props.onConfirm
 * @param {() => void} props.onCancel
 */
function ConfirmDialog({
  title,
  body,
  danger,
  critical,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  onConfirm,
  onCancel,
}) {
  const confirmButtonRef = useRef(null);

  useEffect(() => {
    confirmButtonRef.current?.focus();
    function handleKeyDown(event) {
      if (event.key === "Escape") onCancel();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onCancel]);

  return (
    <div
      className={`confirm-backdrop${critical ? " critical" : ""}`}
      onClick={onCancel}
    >
      <div
        className={`confirm-dialog${critical ? " critical" : ""}`}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={title ? "confirm-dialog-title" : undefined}
        onClick={(event) => event.stopPropagation()}
      >
        {title && (
          <h2 id="confirm-dialog-title" className="confirm-dialog-title">
            {title}
          </h2>
        )}
        <div className="confirm-dialog-body">{body}</div>
        <div className="confirm-dialog-actions">
          <button
            type="button"
            className="confirm-dialog-cancel"
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            ref={confirmButtonRef}
            className={`confirm-dialog-confirm${danger || critical ? " danger" : ""}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ConfirmDialog;
