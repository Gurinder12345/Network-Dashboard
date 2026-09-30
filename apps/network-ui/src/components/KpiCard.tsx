import type { Tone } from "./StatusBadge";

interface KpiCardProps {
  label: string;
  value: string | number;
  hint?: string;
  dim?: boolean;
  tone?: Tone;
  // Color the number itself (only when the value needs attention, e.g. Down > 0).
  emphasize?: boolean;
}

export function KpiCard({ label, value, hint, dim, tone, emphasize }: KpiCardProps) {
  const classes = ["kpi-card", tone ? `tone-${tone}` : "", emphasize ? "emphasize" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={classes}>
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value${dim ? " dim" : ""}`}>{value}</div>
      {hint && <div className="kpi-hint">{hint}</div>}
    </div>
  );
}
