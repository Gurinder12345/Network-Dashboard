export function formatTimestamp(value: string | null): string {
  if (!value) return "—";

  return new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatTime(value: string | null): string {
  if (!value) return "—";

  return new Date(value).toLocaleTimeString();
}

export function formatResponseTime(ms: number | null): string {
  if (ms === null || ms === undefined) return "—";

  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}

export function formatRelative(value: string | null, now: number = Date.now()): string {
  if (!value) return "—";

  const seconds = Math.round((now - new Date(value).getTime()) / 1000);

  if (seconds < 0) return "just now";
  if (seconds < 45) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  if (seconds < 86400 * 7) return `${Math.round(seconds / 86400)}d ago`;

  return formatTimestamp(value);
}

export function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";

  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";

  const seconds = ms / 1000;
  if (seconds < 1) return `${ms} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;

  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${String(rest).padStart(2, "0")}s`;
}

/** "config_backup" -> "Config backup" */
export function humanize(value: string): string {
  const text = value.replace(/[_-]+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : value;
}

export function truncateId(value: string | null, length = 8): string {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

export function truncateText(value: string, length = 80): string {
  const firstLine = value.split("\n")[0];
  return firstLine.length > length ? `${firstLine.slice(0, length)}…` : firstLine;
}
