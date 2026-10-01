/**
 * Presentation-only telemetry thresholds. They label CPU/memory values and never change
 * device health or raise alerts. Kept in one place so they can become configurable.
 */
export type MetricLevel = "normal" | "warning" | "critical";

export const METRIC_THRESHOLDS = {
  cpu: { warning: 70, critical: 90 },
  memory: { warning: 75, critical: 90 },
} as const;

export type MetricKind = keyof typeof METRIC_THRESHOLDS;

export function metricLevel(kind: MetricKind, value: number | null): MetricLevel | null {
  if (value === null || value === undefined) return null;
  const { warning, critical } = METRIC_THRESHOLDS[kind];
  if (value >= critical) return "critical";
  if (value >= warning) return "warning";
  return "normal";
}

export const LEVEL_TONE = { normal: "success", warning: "warning", critical: "danger" } as const;
