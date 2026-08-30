import { useCallback, useState } from "react";
import ConfirmDialog from "../components/ConfirmDialog.jsx";

/**
 * A promise-based replacement for `window.confirm()`: `await confirm(...)`
 * resolves to `true`/`false` the same way, but renders this project's own
 * modal (ConfirmDialog.jsx) instead of the browser's native one — see
 * DECISIONS.md. Returns `[confirm, dialog]` — render `{dialog}` once,
 * anywhere in the owning component's JSX (it's `null` until a
 * confirmation is pending), and call `confirm(options)` from any event
 * handler exactly where `window.confirm(message)` used to be called.
 *
 * @returns {[
 *   (options: {title?: string, body: import("react").ReactNode, danger?: boolean, confirmLabel?: string, cancelLabel?: string}) => Promise<boolean>,
 *   import("react").ReactNode,
 * ]}
 */
export function useConfirm() {
  const [pending, setPending] = useState(null);

  const confirm = useCallback((options) => {
    return new Promise((resolve) => {
      setPending({ ...options, resolve });
    });
  }, []);

  const handleConfirm = useCallback(() => {
    pending?.resolve(true);
    setPending(null);
  }, [pending]);

  const handleCancel = useCallback(() => {
    pending?.resolve(false);
    setPending(null);
  }, [pending]);

  const dialog = pending ? (
    <ConfirmDialog
      title={pending.title}
      body={pending.body}
      danger={pending.danger}
      confirmLabel={pending.confirmLabel}
      cancelLabel={pending.cancelLabel}
      onConfirm={handleConfirm}
      onCancel={handleCancel}
    />
  ) : null;

  return [confirm, dialog];
}
