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

export function truncateId(value: string | null, length = 8): string {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

export function truncateText(value: string, length = 80): string {
  const firstLine = value.split("\n")[0];
  return firstLine.length > length ? `${firstLine.slice(0, length)}…` : firstLine;
}
