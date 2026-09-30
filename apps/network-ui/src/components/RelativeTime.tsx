import { formatRelative } from "../utils/format";

/** "3m ago" with the exact local timestamp on hover. Recomputed on each render/poll. */
export function RelativeTime({ value }: { value: string | null }) {
  if (!value) return <span className="muted">—</span>;

  const exact = new Date(value).toLocaleString();

  return (
    <time className="relative-time" dateTime={value} title={exact}>
      {formatRelative(value)}
    </time>
  );
}
