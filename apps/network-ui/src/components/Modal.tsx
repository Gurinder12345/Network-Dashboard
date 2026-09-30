import { useEffect, useId, useRef, type ReactNode } from "react";

interface ModalProps {
  kicker?: string;
  title: string;
  badge?: ReactNode;
  busy?: boolean;
  onClose: () => void;
  children: ReactNode;
  actions: ReactNode;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

/**
 * Accessible dialog shell: traps Tab focus inside, closes on Escape / backdrop click
 * (unless busy), and returns focus to the element that opened it.
 */
export function Modal({ kicker, title, badge, busy = false, onClose, children, actions }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // Captured during the first render, before autoFocus moves focus into the dialog.
  const openerRef = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);

  useEffect(() => {
    const previouslyFocused = openerRef.current;
    const dialog = dialogRef.current;

    // Respect an explicit autoFocus field; otherwise focus the dialog itself.
    if (dialog && !dialog.contains(document.activeElement)) {
      dialog.focus();
    }

    return () => previouslyFocused?.focus?.();
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) {
        onClose();
        return;
      }

      if (event.key !== "Tab" || !dialogRef.current) return;

      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  return (
    <div className="modal-backdrop" onClick={() => !busy && onClose()}>
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="panel-header">
          <div className="modal-title">
            <div>
              {kicker && <span className="modal-kicker">{kicker}</span>}
              <h2 id={titleId}>{title}</h2>
            </div>
          </div>
          {badge}
        </div>

        <div className="modal-body">{children}</div>

        <div className="modal-actions">{actions}</div>
      </div>
    </div>
  );
}
