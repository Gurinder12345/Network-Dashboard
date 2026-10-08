import type { ReactNode } from "react";

// Lightweight SVG charts (no chart library). Every chart is paired with text: a legend with
// counts/percentages or value labels, plus an aria-label, so no information is conveyed by
// colour or graphics alone. Charts only ever receive real API data.

export interface DonutSegment {
  key: string;
  label: string;
  value: number;
  color: string;
}

interface DonutProps {
  segments: DonutSegment[];
  centerValue: string | number;
  centerLabel: string;
  ariaLabel: string;
  size?: number;
}

export function Donut({ segments, centerValue, centerLabel, ariaLabel, size = 132 }: DonutProps) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  const radius = 52;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  return (
    <div className="donut-chart">
      <svg width={size} height={size} viewBox="0 0 132 132" role="img" aria-label={ariaLabel}>
        <circle cx="66" cy="66" r={radius} className="donut-track" />
        {total > 0 &&
          segments
            .filter((s) => s.value > 0)
            .map((segment) => {
              const length = (segment.value / total) * circumference;
              const dash = `${Math.max(length - (segments.filter((s) => s.value > 0).length > 1 ? 2 : 0), 0.5)} ${circumference}`;
              const element = (
                <circle
                  key={segment.key}
                  cx="66"
                  cy="66"
                  r={radius}
                  fill="none"
                  style={{ stroke: segment.color }}
                  strokeWidth="14"
                  strokeDasharray={dash}
                  strokeDashoffset={-offset}
                  transform="rotate(-90 66 66)"
                >
                  <title>{`${segment.label}: ${segment.value}`}</title>
                </circle>
              );
              offset += length;
              return element;
            })}
        <text x="66" y="64" textAnchor="middle" className="donut-value">
          {centerValue}
        </text>
        <text x="66" y="82" textAnchor="middle" className="donut-label">
          {centerLabel}
        </text>
      </svg>
      <ul className="donut-legend">
        {segments.map((segment) => (
          <li key={segment.key}>
            <span className="legend-swatch" style={{ background: segment.color }} aria-hidden="true" />
            <span className="donut-legend-label">{segment.label}</span>
            <span className="donut-legend-value">{segment.value}</span>
            <span className="donut-legend-pct">{total > 0 ? `${Math.round((segment.value / total) * 100)}%` : "—"}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface BarRow {
  key: string;
  label: string;
  sublabel?: string;
  value: number;
  /** success | warning | danger | info */
  tone: string;
  href?: string;
}

/** Horizontal percentage bars (0-100), value printed as text next to each bar. */
export function BarList({ rows, ariaLabel, renderLabel }: { rows: BarRow[]; ariaLabel: string; renderLabel?: (row: BarRow) => ReactNode }) {
  return (
    <ul className="bar-list" aria-label={ariaLabel}>
      {rows.map((row) => (
        <li key={row.key} className="bar-row">
          <div className="bar-row-label">
            {renderLabel ? renderLabel(row) : <span className="bar-row-name">{row.label}</span>}
            {row.sublabel && <span className="bar-row-sub">{row.sublabel}</span>}
          </div>
          <div className="bar-track" aria-hidden="true">
            <span className={`bar-fill bar-${row.tone}`} style={{ width: `${Math.min(Math.max(row.value, 0), 100)}%` }} />
          </div>
          <span className={`bar-value bar-value-${row.tone}`}>{row.value.toFixed(1)}%</span>
        </li>
      ))}
    </ul>
  );
}

export interface ColumnSeries {
  key: string;
  label: string;
  color: string;
}

export interface ColumnBucket {
  key: string;
  label: string;
  values: Record<string, number>;
}

/** Stacked daily columns with the per-day total printed above each column. */
export function StackedColumns({ buckets, series, ariaLabel }: { buckets: ColumnBucket[]; series: ColumnSeries[]; ariaLabel: string }) {
  const totals = buckets.map((b) => series.reduce((sum, s) => sum + (b.values[s.key] ?? 0), 0));
  const max = Math.max(1, ...totals);
  const width = 300;
  const height = 120;
  const slot = width / Math.max(buckets.length, 1);
  const barWidth = Math.min(26, slot * 0.55);

  return (
    <svg className="stacked-columns" viewBox={`0 0 ${width} ${height + 34}`} role="img" aria-label={ariaLabel}>
      {[0.5, 1].map((f) => (
        <line key={f} x1="0" x2={width} y1={height - height * f * 0.85} y2={height - height * f * 0.85} className="chart-gridline" />
      ))}
      <line x1="0" x2={width} y1={height} y2={height} className="column-baseline" />
      {buckets.map((bucket, index) => {
        let y = height;
        const x = index * slot + (slot - barWidth) / 2;
        return (
          <g key={bucket.key}>
            {series.map((s) => {
              const value = bucket.values[s.key] ?? 0;
              if (!value) return null;
              const h = (value / max) * height * 0.85;
              y -= h;
              return (
                <rect key={s.key} x={x} y={y} width={barWidth} height={Math.max(h - 1, 1)} rx="2" style={{ fill: s.color }}>
                  <title>{`${bucket.label}: ${value} ${s.label.toLowerCase()}`}</title>
                </rect>
              );
            })}
            <text x={x + barWidth / 2} y={y - 4} textAnchor="middle" className="column-total">
              {totals[index] || ""}
            </text>
            <text x={x + barWidth / 2} y={height + 16} textAnchor="middle" className="column-label">
              {bucket.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
