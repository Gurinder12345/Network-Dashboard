import { useEffect, useMemo, useRef, useState } from "react";
import type { MetricSample } from "../api/types";

/**
 * Single-series 0-100% time chart (plain SVG, no chart dependency).
 *
 * - The x-domain is the whole selected window, so missing history shows as empty space.
 * - The line breaks at failed samples (null) and wherever two samples are more than
 *   2.5 collection steps apart: no interpolation across missing data.
 * - Hover/focus snaps a crosshair to the nearest sample; the tooltip lists its value.
 * - No animation: re-polling only re-renders the path.
 */

type ValueKey = "cpu_percent" | "memory_percent";

interface MetricChartProps {
  label: string;
  valueKey: ValueKey;
  samples: MetricSample[];
  windowSeconds: number;
  stepSeconds: number;
  /** Window end (ms); the range ends "now" at the last history fetch. */
  endMs: number;
  thresholds: { warning: number; critical: number };
  /** Extra tooltip line for a sample (e.g. "3.4 GB / 8.0 GB"). */
  detail?: (sample: MetricSample) => string | null;
}

const HEIGHT = 220;
const MARGIN = { top: 12, right: 14, bottom: 26, left: 40 };
const Y_TICKS = [0, 25, 50, 75, 100];

interface Point {
  t: number;
  v: number;
}

function formatTick(ms: number, windowSeconds: number) {
  const date = new Date(ms);
  return windowSeconds > 86400
    ? date.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function formatTooltipTime(ms: number, windowSeconds: number) {
  const date = new Date(ms);
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return windowSeconds > 86400 ? `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}` : time;
}

export function MetricChart({ label, valueKey, samples, windowSeconds, stepSeconds, endMs, thresholds, detail }: MetricChartProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<number | null>(null); // index into `parsed`

  useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(240, Math.floor(entry.contentRect.width))));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const startMs = endMs - windowSeconds * 1000;
  const plotW = width - MARGIN.left - MARGIN.right;
  const plotH = HEIGHT - MARGIN.top - MARGIN.bottom;
  const x = (t: number) => MARGIN.left + ((t - startMs) / (endMs - startMs)) * plotW;
  const y = (v: number) => MARGIN.top + (1 - v / 100) * plotH;

  const parsed = useMemo(
    () =>
      samples
        .map((sample) => ({ sample, t: Date.parse(sample.collected_at), v: sample[valueKey] }))
        .filter((item) => item.t >= startMs && item.t <= endMs)
        .sort((a, b) => a.t - b.t),
    [samples, valueKey, startMs, endMs],
  );

  const segments = useMemo(() => {
    const maxGapMs = stepSeconds * 2.5 * 1000;
    const result: Point[][] = [];
    let current: Point[] = [];
    let previousT: number | null = null;

    for (const item of parsed) {
      const broken = item.v === null || (previousT !== null && item.t - previousT > maxGapMs);
      if (broken && current.length) {
        result.push(current);
        current = [];
      }
      if (item.v !== null) current.push({ t: item.t, v: item.v });
      previousT = item.t;
    }
    if (current.length) result.push(current);
    return result;
  }, [parsed, stepSeconds]);

  const hasData = segments.length > 0;
  const xTicks = useMemo(() => Array.from({ length: 5 }, (_, i) => startMs + ((endMs - startMs) * i) / 4), [startMs, endMs]);

  function onPointer(clientX: number) {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect || parsed.length === 0) return;
    const t = startMs + ((clientX - rect.left - MARGIN.left) / plotW) * (endMs - startMs);
    let best = 0;
    for (let i = 1; i < parsed.length; i++) {
      if (Math.abs(parsed[i].t - t) < Math.abs(parsed[best].t - t)) best = i;
    }
    // Only snap when the pointer is near a real sample (not across a long gap).
    setHover(Math.abs(parsed[best].t - t) <= stepSeconds * 1.5 * 1000 ? best : null);
  }

  function onKey(event: React.KeyboardEvent) {
    if (parsed.length === 0) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const delta = event.key === "ArrowLeft" ? -1 : 1;
      setHover((h) => Math.min(parsed.length - 1, Math.max(0, (h ?? parsed.length - 1) + (h === null ? 0 : delta))));
    } else if (event.key === "Escape") {
      setHover(null);
    }
  }

  const hovered = hover !== null ? parsed[hover] : null;
  const tooltipLeft = hovered ? Math.min(Math.max(x(hovered.t), 70), width - 70) : 0;
  const extra = hovered && hovered.v !== null && detail ? detail(hovered.sample) : null;

  return (
    <div
      className="metric-chart"
      ref={wrapRef}
      onPointerMove={(event) => onPointer(event.clientX)}
      onPointerLeave={() => setHover(null)}
    >
      <svg
        width={width}
        height={HEIGHT}
        role="img"
        aria-label={`${label} usage, ${windowSeconds / 3600 >= 48 ? `${windowSeconds / 86400} days` : `${windowSeconds / 3600} hours`}, 0 to 100 percent`}
        tabIndex={0}
        onKeyDown={onKey}
        onBlur={() => setHover(null)}
      >
        {Y_TICKS.map((tick) => (
          <g key={tick}>
            <line className="chart-gridline" x1={MARGIN.left} x2={width - MARGIN.right} y1={y(tick)} y2={y(tick)} />
            <text className="chart-axis" x={MARGIN.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">
              {tick}%
            </text>
          </g>
        ))}
        {xTicks.map((tick, i) => (
          <text
            key={tick}
            className="chart-axis"
            x={x(tick)}
            y={HEIGHT - 8}
            textAnchor={i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle"}
          >
            {formatTick(tick, windowSeconds)}
          </text>
        ))}

        {(["warning", "critical"] as const).map((level) => (
          <g key={level} className={`chart-threshold ${level}`}>
            <line x1={MARGIN.left} x2={width - MARGIN.right} y1={y(thresholds[level])} y2={y(thresholds[level])} />
            <text x={width - MARGIN.right - 2} y={y(thresholds[level]) - 4} textAnchor="end">
              {level} {thresholds[level]}%
            </text>
          </g>
        ))}

        {segments.map((segment, i) =>
          // A lone sample, or a run too short to see at this range, is drawn as a dot.
          segment.length === 1 || x(segment[segment.length - 1].t) - x(segment[0].t) < 4 ? (
            <circle key={i} className="chart-dot" cx={x(segment[0].t)} cy={y(segment[0].v)} r={3} />
          ) : (
            <g key={i}>
              <path
                className="chart-area"
                d={`M${x(segment[0].t)},${y(0)} ${segment.map((p) => `L${x(p.t)},${y(p.v)}`).join(" ")} L${x(segment[segment.length - 1].t)},${y(0)} Z`}
              />
              <path className="chart-line" d={segment.map((p, j) => `${j ? "L" : "M"}${x(p.t)},${y(p.v)}`).join(" ")} />
            </g>
          ),
        )}

        {hasData && (() => {
          // End-dot on the newest value, so the current reading is always visible.
          const last = segments[segments.length - 1];
          const point = last[last.length - 1];
          return <circle className="chart-dot" cx={x(point.t)} cy={y(point.v)} r={4} />;
        })()}

        {hovered && (
          <g aria-hidden="true">
            <line className="chart-crosshair" x1={x(hovered.t)} x2={x(hovered.t)} y1={MARGIN.top} y2={MARGIN.top + plotH} />
            {hovered.v !== null && <circle className="chart-hover-dot" cx={x(hovered.t)} cy={y(hovered.v)} r={4} />}
          </g>
        )}
      </svg>

      {!hasData && <div className="chart-empty">No {label.toLowerCase()} data in this range.</div>}

      {hovered && (
        <div className="chart-tooltip" style={{ left: tooltipLeft }} role="status">
          <div className="chart-tooltip-time">{formatTooltipTime(hovered.t, windowSeconds)}</div>
          <div>
            {label}: <strong>{hovered.v !== null ? `${hovered.v.toFixed(1)}%` : "unavailable"}</strong>
          </div>
          {extra && <div className="chart-tooltip-extra">{extra}</div>}
        </div>
      )}
    </div>
  );
}
