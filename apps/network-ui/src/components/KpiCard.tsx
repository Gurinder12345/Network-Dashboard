import { IconTile, type IconName } from "./Icon";
import type { Tone } from "./StatusBadge";

interface KpiCardProps {
  label: string;
  value: string | number;
  hint?: string;
  dim?: boolean;
  tone?: Tone;
  // Color the number itself (only when the value needs attention, e.g. Down > 0).
  emphasize?: boolean;
  icon?: IconName;
}

// The colored strip along the bottom edge is decoration in the card's tone only; it does
// not encode any data (there is no history series behind these counts).
export function KpiCard({ label, value, hint, dim, tone, emphasize, icon }: KpiCardProps) {
  const classes = ["kpi-card", tone ? `tone-${tone}` : "", emphasize ? "emphasize" : "", icon ? "has-icon" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={classes}>
      {icon && <IconTile name={icon} tone={tone ?? "neutral"} />}
      <div className="kpi-text">
        <div className="kpi-label">{label}</div>
        <div className={`kpi-value${dim ? " dim" : ""}`}>{value}</div>
        {hint && <div className="kpi-hint">{hint}</div>}
      </div>
    </div>
  );
}
