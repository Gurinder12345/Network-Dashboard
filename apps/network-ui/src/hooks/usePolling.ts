import { useEffect, useRef } from "react";

/**
 * Calls `callback` every `intervalMs` while the tab is visible, and once immediately when
 * the tab becomes visible again. Only re-reads API data; it never starts switch checks.
 */
export function usePolling(callback: () => void | Promise<void>, intervalMs: number) {
  const saved = useRef(callback);

  useEffect(() => {
    saved.current = callback;
  }, [callback]);

  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") {
        void saved.current();
      }
    };

    const timer = window.setInterval(tick, intervalMs);
    document.addEventListener("visibilitychange", tick);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [intervalMs]);
}
