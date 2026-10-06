export type Tone = "success" | "warning" | "danger" | "info" | "neutral";

// One mapping for every status in the product, so a color always means the same thing:
// green = good/done, amber = attention/awaiting a person, red = failed/down, blue = queued/approved/in progress,
// gray = unknown/cancelled/inactive.
const STATUS_TONES: Record<string, Tone> = {
  healthy: "success",
  applied: "success",
  success: "success",
  succeeded: "success",
  completed: "success",
  passed: "success",

  degraded: "warning",
  pending: "warning",
  warning: "warning",
  review: "warning",

  down: "danger",
  failed: "danger",
  error: "danger",
  blocked: "danger",

  queued: "info",
  approved: "info",
  applying: "info",
  running: "info",
  submitting: "info",

  unknown: "neutral",
  cancelled: "neutral",
  disabled: "neutral",
};

// States that are actively progressing get a gently pulsing dot.
const ACTIVE_STATUSES = new Set(["applying", "running", "submitting"]);

export function statusTone(status: string): Tone {
  return STATUS_TONES[status.toLowerCase()] ?? "neutral";
}

interface StatusBadgeProps {
  status: string;
  // Optional display text; the status word itself is shown by default so color is never
  // the only signal.
  label?: string;
  tone?: Tone;
  title?: string;
}

export function StatusBadge({ status, label, tone, title }: StatusBadgeProps) {
  const normalized = status.toLowerCase();
  const resolvedTone = tone ?? statusTone(normalized);
  const active = ACTIVE_STATUSES.has(normalized) ? " is-active" : "";

  return (
    <span className={`badge tone-${resolvedTone} badge-${normalized}${active}`} title={title}>
      <span className="badge-dot" aria-hidden="true" />
      <span className="badge-label">{label ?? status.replace(/_/g, " ")}</span>
    </span>
  );
}
