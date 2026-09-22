interface KpiCardProps {
  label: string;
  value: string | number;
  hint?: string;
  dim?: boolean;
}

export function KpiCard({ label, value, hint, dim }: KpiCardProps) {
  return (
    <div className="kpi-card">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value${dim ? " dim" : ""}`}>{value}</div>
      {hint && <div className="kpi-hint">{hint}</div>}
    </div>
  );
}
