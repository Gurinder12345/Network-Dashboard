import type { ReactNode } from "react";

type BannerTone = "danger" | "success" | "warning" | "info";

interface BannerProps {
  tone: BannerTone;
  title?: string;
  children?: ReactNode;
  onDismiss?: () => void;
}

export function Banner({ tone, title, children, onDismiss }: BannerProps) {
  return (
    <div className={`banner banner-${tone}`} role={tone === "danger" ? "alert" : "status"}>
      <div className="banner-body">
        {title && <span className="banner-title">{title}</span>}
        {children}
      </div>
      {onDismiss && (
        <button type="button" className="copy-button" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </div>
  );
}

/** Shown when polling keeps failing but older data is still on screen. */
export function StaleDataWarning({ since, error }: { since: Date | null; error: string | null }) {
  return (
    <Banner tone="warning" title="Showing cached data.">
      Refresh is failing{error ? ` (${error})` : ""}. Data on screen is from{" "}
      {since ? since.toLocaleTimeString() : "an earlier load"} and may be out of date.
    </Banner>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <span className="empty-state-title">{title}</span>
      {hint}
    </div>
  );
}

const SKELETON_WIDTHS = ["70%", "45%", "60%", "35%", "55%", "40%", "65%", "50%"];

/** Placeholder rows while a table loads (no spinners). */
export function TableSkeleton({ rows = 6, columns = 5 }: { rows?: number; columns?: number }) {
  return (
    <table className="skeleton-table" aria-busy="true" aria-label="Loading">
      <tbody>
        {Array.from({ length: rows }, (_, row) => (
          <tr key={row}>
            {Array.from({ length: columns }, (_, col) => (
              <td key={col}>
                <span
                  className="skeleton"
                  style={{ width: SKELETON_WIDTHS[(row + col) % SKELETON_WIDTHS.length] }}
                />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
