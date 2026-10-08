import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type {
  Approval,
  AuditEvent,
  Backup,
  Device,
  DeviceHealthEntry,
  DeviceTelemetry,
  FleetHealth,
  Job,
  TopologyGraph,
} from "../../api/types";
import { formatRelative, formatUptime, platformLabel, truncateText } from "../../utils/format";
import { METRIC_THRESHOLDS, metricLevel, type MetricKind } from "../../utils/thresholds";
import { BarList, Donut, StackedColumns, type BarRow } from "../charts";
import { EmptyState, TableSkeleton } from "../Feedback";
import { Icon, type IconName } from "../Icon";
import { OperationTag } from "../OperationTag";
import { RelativeTime } from "../RelativeTime";
import { StatusBadge } from "../StatusBadge";

/** Data for one widget: loaded independently so one failing source never blanks the page. */
export interface Loadable<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

// Status colours (tokens). Shared by charts and legends.
export const COLORS = {
  healthy: "var(--green)",
  degraded: "var(--amber)",
  down: "var(--red)",
  unknown: "var(--gray-status)",
  blue: "var(--blue)",
  purple: "var(--purple)",
};

export function WidgetCard({
  title,
  icon,
  meta,
  children,
  className,
}: {
  title: string;
  icon: IconName;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel widget ${className ?? ""}`}>
      <div className="panel-header">
        <h2>
          <Icon name={icon} size={16} className="panel-title-icon" />
          {title}
        </h2>
        {meta && <div className="panel-header-meta">{meta}</div>}
      </div>
      <div className="widget-body">{children}</div>
    </section>
  );
}

function WidgetState<T>({ state, empty, children }: { state: Loadable<T>; empty?: string; children: (data: T) => ReactNode }) {
  if (state.loading && state.data === null) return <TableSkeleton rows={4} columns={2} />;
  if (state.error && state.data === null) {
    return <EmptyState title="Data unavailable" hint={state.error} />;
  }
  if (state.data === null) return <EmptyState title={empty ?? "No data"} />;
  return <>{children(state.data)}</>;
}

// ---- Health trend: no history API exists -----------------------------------------------------
export function HealthTrendCard() {
  return (
    <WidgetCard title="Health Trend" icon="pulse" meta="last 24 hours">
      <div className="widget-empty">
        <Icon name="clock" size={22} />
        <strong>Historical fleet health not yet available</strong>
        <span>
          The platform stores each device's current health only. A trend appears here once health history is
          recorded; nothing is estimated.
        </span>
      </div>
    </WidgetCard>
  );
}

// ---- Platform distribution ---------------------------------------------------------------------
const PLATFORM_COLORS = ["var(--blue)", "var(--purple)", "var(--green)", "var(--amber)", "var(--gray-status)"];

export function PlatformDistribution({
  devices,
  health,
  loading,
}: {
  devices: Device[];
  health: Map<number, DeviceHealthEntry>;
  loading: boolean;
}) {
  const groups = new Map<string, Device[]>();
  devices.forEach((d) => groups.set(d.platform, [...(groups.get(d.platform) ?? []), d]));
  const platforms = Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));

  return (
    <WidgetCard title="Platform Distribution" icon="layers" meta={<span className="count-tag">{platforms.length}</span>}>
      {loading && devices.length === 0 ? (
        <TableSkeleton rows={3} columns={2} />
      ) : platforms.length === 0 ? (
        <EmptyState title="No devices" />
      ) : (
        <>
          <Donut
            segments={platforms.map(([platform, list], i) => ({
              key: platform,
              label: platformLabel(platform),
              value: list.length,
              color: PLATFORM_COLORS[i % PLATFORM_COLORS.length],
            }))}
            centerValue={devices.length}
            centerLabel="devices"
            ariaLabel={platforms.map(([p, l]) => `${platformLabel(p)} ${l.length}`).join(", ")}
          />
          <div className="platform-mini">
            {platforms.map(([platform, list]) => {
              const healthy = list.filter((d) => health.get(d.id)?.status === "healthy").length;
              return (
                <Link key={platform} to="/devices" className="platform-mini-row" title={`Open Devices (${platformLabel(platform)})`}>
                  <span className="platform-tag">{platform}</span>
                  <span className="muted">
                    {list.filter((d) => d.enabled).length} enabled · {healthy} healthy
                  </span>
                  <Icon name="chevronRight" size={14} className="row-chevron" />
                </Link>
              );
            })}
          </div>
        </>
      )}
    </WidgetCard>
  );
}

// ---- CPU / memory: latest stored sample per device ---------------------------------------------
const LEVEL_BAR_TONE = { normal: "info", warning: "warning", critical: "danger" } as const;

export function UtilizationCard({
  kind,
  devices,
  metrics,
}: {
  kind: MetricKind;
  devices: Device[];
  metrics: Loadable<Map<number, DeviceTelemetry>>;
}) {
  const field = kind === "cpu" ? "cpu_percent" : "memory_percent";
  const title = kind === "cpu" ? "CPU Utilization" : "Memory Utilization";
  const thresholds = METRIC_THRESHOLDS[kind];

  return (
    <WidgetCard
      title={title}
      icon={kind === "cpu" ? "cpu" : "memory"}
      meta={<span title={`Display thresholds: warning ${thresholds.warning}%, critical ${thresholds.critical}%`}>top 5 · latest sample</span>}
    >
      <WidgetState state={metrics} empty="No telemetry collected yet">
        {(map) => {
          const rows: BarRow[] = devices
            .map((device) => ({ device, sample: map.get(device.id) }))
            .filter(({ sample }) => sample && sample[field] !== null && sample[field] !== undefined)
            .sort((a, b) => (b.sample![field] as number) - (a.sample![field] as number))
            .slice(0, 5)
            .map(({ device, sample }) => {
              const value = sample![field] as number;
              const level = metricLevel(kind, value) ?? "normal";
              return {
                key: String(device.id),
                label: device.hostname,
                sublabel: `${platformLabel(device.platform)}${sample!.stale ? " · stale" : ""}`,
                value,
                tone: LEVEL_BAR_TONE[level],
                href: `/devices/${device.id}`,
              };
            });
          if (rows.length === 0) return <EmptyState title="No telemetry collected yet" hint="CPU/memory samples appear after the next collection." />;
          return (
            <BarList
              rows={rows}
              ariaLabel={`${title}, top ${rows.length} devices`}
              renderLabel={(row) => (
                <Link className="bar-row-name device-link" to={row.href!}>
                  {row.label}
                </Link>
              )}
            />
          );
        }}
      </WidgetState>
    </WidgetCard>
  );
}

// ---- Change activity: real approvals, last 7 days by request date --------------------------------
const CHANGE_SERIES = [
  { key: "applied", label: "Applied", color: "var(--green)" },
  { key: "failed", label: "Failed", color: "var(--red)" },
  { key: "open", label: "Open", color: "var(--amber)" },
  { key: "cancelled", label: "Cancelled", color: "var(--gray-status)" },
];

function changeBucket(status: string): string {
  if (status === "applied" || status === "failed" || status === "cancelled") return status;
  return "open"; // pending / approved / applying
}

export function ChangeActivity({ approvals, loading }: { approvals: Approval[]; loading: boolean }) {
  const days = Array.from({ length: 7 }, (_, i) => {
    const date = new Date();
    date.setHours(0, 0, 0, 0);
    date.setDate(date.getDate() - (6 - i));
    return date;
  });
  const buckets = days.map((day) => ({
    key: day.toISOString(),
    label: day.toLocaleDateString(undefined, { weekday: "short" }),
    values: {} as Record<string, number>,
  }));
  approvals.forEach((approval) => {
    if (!approval.created_at) return;
    const created = new Date(approval.created_at);
    created.setHours(0, 0, 0, 0);
    const index = days.findIndex((d) => d.getTime() === created.getTime());
    if (index < 0) return;
    const key = changeBucket(approval.status);
    buckets[index].values[key] = (buckets[index].values[key] ?? 0) + 1;
  });
  const totals = CHANGE_SERIES.map((s) => ({ ...s, value: buckets.reduce((sum, b) => sum + (b.values[s.key] ?? 0), 0) }));
  const total = totals.reduce((sum, s) => sum + s.value, 0);

  return (
    <WidgetCard
      title="Change Activity"
      icon="wrench"
      meta={
        <Link to="/approvals" className="panel-header-link">
          Approvals <Icon name="arrowRight" size={13} />
        </Link>
      }
    >
      {loading && approvals.length === 0 ? (
        <TableSkeleton rows={3} columns={2} />
      ) : total === 0 ? (
        <EmptyState title="No change requests in the last 7 days" />
      ) : (
        <>
          <StackedColumns
            buckets={buckets}
            series={CHANGE_SERIES}
            ariaLabel={`Change requests in the last 7 days by current status: ${totals.map((t) => `${t.value} ${t.label.toLowerCase()}`).join(", ")}`}
          />
          <ul className="inline-legend">
            {totals.map((s) => (
              <li key={s.key}>
                <span className="legend-swatch" style={{ background: s.color }} aria-hidden="true" />
                {s.label} <strong>{s.value}</strong>
              </li>
            ))}
          </ul>
          <div className="widget-note">Change requests by request date, coloured by their current status.</div>
        </>
      )}
    </WidgetCard>
  );
}

// ---- Recent incidents: current health problems + failure audit events ----------------------------
const INCIDENT_EVENTS: Record<string, { label: string; status: string }> = {
  apply_failed: { label: "Change apply failed", status: "failed" },
  apply_block_failed: { label: "Change block failed", status: "failed" },
  apply_deferred: { label: "Change apply deferred (device busy)", status: "warning" },
  precheck_failed: { label: "Precheck failed", status: "failed" },
  backup_failed: { label: "Backup failed", status: "failed" },
  post_change_health_warning: { label: "Post-change health warning", status: "warning" },
  topology_discovery_failed: { label: "Topology discovery failed", status: "warning" },
};

interface Incident {
  key: string;
  time: string | null;
  device: string;
  deviceId: number | null;
  summary: string;
  status: string;
  label: string;
}

export function RecentIncidents({
  fleet,
  audit,
  deviceName,
}: {
  fleet: FleetHealth | null;
  audit: Loadable<AuditEvent[]>;
  deviceName: (id: number | null) => string;
}) {
  const current: Incident[] = (fleet?.devices ?? [])
    .filter((d) => d.status === "down" || d.status === "degraded")
    .map((d) => ({
      key: `health-${d.device_id}`,
      time: d.last_status_change_at ?? d.last_check_at,
      device: d.hostname,
      deviceId: d.device_id,
      summary: d.last_error ?? `Device ${d.status}`,
      status: d.status,
      label: d.status === "down" ? "Device down" : "Device degraded",
    }));
  const events: Incident[] = (audit.data ?? [])
    .filter((e) => e.event_type in INCIDENT_EVENTS)
    .map((e) => ({
      key: `audit-${e.id}`,
      time: e.created_at,
      device: deviceName(e.device_id),
      deviceId: e.device_id,
      summary: e.message ?? "",
      status: INCIDENT_EVENTS[e.event_type].status,
      label: INCIDENT_EVENTS[e.event_type].label,
    }));
  const incidents = [...current, ...events]
    .sort((a, b) => (b.time ?? "").localeCompare(a.time ?? ""))
    .slice(0, 8);

  return (
    <WidgetCard
      title="Recent Incidents"
      icon="alert"
      meta={
        <Link to="/audit" className="panel-header-link">
          Audit <Icon name="arrowRight" size={13} />
        </Link>
      }
    >
      {audit.loading && !audit.data && current.length === 0 ? (
        <TableSkeleton rows={4} columns={2} />
      ) : incidents.length === 0 ? (
        <EmptyState
          title="No recent incidents"
          hint={audit.error ? `Audit events unavailable: ${audit.error}` : "No device is degraded or down and no failures were recorded."}
        />
      ) : (
        <ul className="incident-list">
          {incidents.map((incident) => (
            <li key={incident.key} className="incident-row">
              <StatusBadge status={incident.status} label={incident.status === "warning" ? "warning" : incident.status} />
              <div className="incident-main">
                <div className="incident-title">
                  {incident.deviceId ? (
                    <Link className="device-link" to={`/devices/${incident.deviceId}`}>
                      {incident.device}
                    </Link>
                  ) : (
                    <span>{incident.device}</span>
                  )}
                  <span className="muted">· {incident.label}</span>
                </div>
                <div className="incident-summary" title={incident.summary}>
                  {truncateText(incident.summary, 110)}
                </div>
              </div>
              <span className="incident-time">
                <RelativeTime value={incident.time} />
              </span>
            </li>
          ))}
        </ul>
      )}
      {audit.error && incidents.length > 0 && <div className="widget-note">Audit events unavailable; showing current health only.</div>}
    </WidgetCard>
  );
}

// ---- Topology preview: managed switches only ------------------------------------------------------
export function TopologyPreview({ topology }: { topology: Loadable<TopologyGraph> }) {
  return (
    <WidgetCard
      title="Topology Preview"
      icon="topology"
      meta={
        <Link to="/topology" className="panel-header-link">
          View full topology <Icon name="arrowRight" size={13} />
        </Link>
      }
    >
      <WidgetState state={topology} empty="Topology data unavailable">
        {(graph) => <TopologyMiniMap graph={graph} />}
      </WidgetState>
    </WidgetCard>
  );
}

function TopologyMiniMap({ graph }: { graph: TopologyGraph }) {
  const managed = graph.nodes.filter((n) => n.managed);
  const ids = new Set(managed.map((n) => n.id));
  const links = graph.links.filter((l) => l.active && l.relationship === "managed" && ids.has(l.source) && ids.has(l.target));
  if (managed.length === 0) {
    return <EmptyState title="No managed switches discovered yet" hint="Run topology discovery from the Topology page." />;
  }

  // Tiers: OS10 cores, then switches linked to a core, then the rest (layout only; no data invented).
  const cores = managed.filter((n) => n.platform === "dell_os10");
  const coreIds = new Set(cores.map((n) => n.id));
  const linkedToCore = new Set(
    links.flatMap((l) => (coreIds.has(l.source) ? [l.target] : coreIds.has(l.target) ? [l.source] : [])),
  );
  const tier1 = managed.filter((n) => !coreIds.has(n.id) && linkedToCore.has(n.id));
  const tier2 = managed.filter((n) => !coreIds.has(n.id) && !linkedToCore.has(n.id));
  const tiers = [cores, tier1, tier2].filter((t) => t.length > 0);
  const width = 420;
  const rowHeight = 72;
  const height = tiers.length * rowHeight + 10;
  const position = new Map<string, { x: number; y: number }>();
  tiers.forEach((tier, row) => {
    tier.forEach((node, i) => {
      position.set(node.id, { x: ((i + 0.5) * width) / tier.length, y: 30 + row * rowHeight });
    });
  });
  const shortName = (name: string) => truncateText(name.replace(/^Kenda-/i, ""), 13);

  return (
    <>
      <svg className="topology-mini" viewBox={`0 0 ${width} ${height}`} role="img"
        aria-label={`${managed.length} managed switches, ${links.length} links between them`}>
        {links.map((link) => {
          const a = position.get(link.source)!;
          const b = position.get(link.target)!;
          return <line key={link.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="mini-link" />;
        })}
        {managed.map((node) => {
          const p = position.get(node.id)!;
          // Crowded tiers alternate labels above/below the node so names never overlap.
          const tier = tiers.find((t) => t.includes(node))!;
          const above = tier.length > 5 && tier.indexOf(node) % 2 === 1;
          return (
            <g key={node.id} transform={`translate(${p.x} ${p.y})`}>
              <title>{`${node.hostname} · ${node.health_status}`}</title>
              <rect x="-9" y="-9" width="18" height="18" rx="4" className={`mini-node mini-${node.health_status}`} />
              <text y={above ? -14 : 23} textAnchor="middle" className="mini-label">
                {shortName(node.hostname)}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="widget-note">
        {managed.length} managed switches · {links.length} links between them · endpoints and unmanaged LLDP neighbors
        hidden
        {graph.last_discovery_at ? ` · discovered ${formatRelative(graph.last_discovery_at)}` : ""}
      </div>
    </>
  );
}

// ---- Backup status: latest backup per device + latest backup job outcome --------------------------
export function BackupStatusCard({
  devices,
  backups,
  jobs,
}: {
  devices: Device[];
  backups: Loadable<Backup[]>;
  jobs: Job[];
}) {
  return (
    <WidgetCard
      title="Backup Status"
      icon="archive"
      meta={
        <Link to="/backups" className="panel-header-link">
          Backups <Icon name="arrowRight" size={13} />
        </Link>
      }
    >
      <WidgetState state={backups} empty="No backup found">
        {(list) => {
          if (devices.length === 0) return <EmptyState title="No devices" />;
          const rows = devices.map((device) => {
            const latest = list
              .filter((b) => b.device_id === device.id && b.file_available !== false)
              .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""))[0];
            const lastJob = jobs
              .filter((j) => j.device_id === device.id && (j.job_type === "manual_backup" || j.job_type === "config_backup"))
              .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""))[0];
            const failedSince = lastJob?.status === "failed" && (!latest || (lastJob.started_at ?? "") > (latest.created_at ?? ""));
            const state = failedSince ? "failed" : latest ? "success" : "never";
            return { device, latest, state, rank: state === "failed" ? 0 : state === "never" ? 1 : 2 };
          });
          rows.sort((a, b) => a.rank - b.rank || (a.latest?.created_at ?? "").localeCompare(b.latest?.created_at ?? ""));
          const counts = { success: 0, failed: 0, never: 0 } as Record<string, number>;
          rows.forEach((r) => (counts[r.state] += 1));
          return (
            <>
              <div className="backup-summary">
                <span><StatusBadge status="success" label={`${counts.success} backed up`} /></span>
                <span><StatusBadge status="failed" label={`${counts.failed} failed`} /></span>
                <span><StatusBadge status="unknown" label={`${counts.never} never`} /></span>
              </div>
              <ul className="backup-list">
                {rows.map(({ device, latest, state }) => (
                  <li key={device.id}>
                    <Link className="device-link" to={`/devices/${device.id}`}>
                      {device.hostname}
                    </Link>
                    <span className="muted">{latest ? <RelativeTime value={latest.created_at} /> : "no backup"}</span>
                    <StatusBadge
                      status={state === "success" ? "success" : state === "failed" ? "failed" : "unknown"}
                      label={state === "success" ? "Success" : state === "failed" ? "Failed" : "Never"}
                    />
                  </li>
                ))}
              </ul>
            </>
          );
        }}
      </WidgetState>
    </WidgetCard>
  );
}

// ---- Device status table ---------------------------------------------------------------------------
function MetricCell({ kind, value }: { kind: MetricKind; value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  const level = metricLevel(kind, value) ?? "normal";
  return (
    <span className={`metric-cell metric-${level}`} title={`${kind === "cpu" ? "CPU" : "Memory"} ${value.toFixed(1)}% (${level})`}>
      <span className="metric-cell-bar" aria-hidden="true">
        <span style={{ width: `${Math.min(value, 100)}%` }} />
      </span>
      {value.toFixed(0)}%
    </span>
  );
}

const SEVERITY: Record<string, number> = { down: 0, degraded: 1, unknown: 2, healthy: 3 };

export function DeviceStatusTable({
  devices,
  health,
  metrics,
  loading,
  accessTag,
  responseTime,
}: {
  devices: Device[];
  health: Map<number, DeviceHealthEntry>;
  metrics: Loadable<Map<number, DeviceTelemetry>>;
  loading: boolean;
  accessTag: (platform: string) => ReactNode;
  responseTime: (ms: number | null) => string;
}) {
  const sorted = [...devices].sort((a, b) => {
    const sa = SEVERITY[health.get(a.id)?.status ?? a.health_status ?? "unknown"] ?? 9;
    const sb = SEVERITY[health.get(b.id)?.status ?? b.health_status ?? "unknown"] ?? 9;
    return sa - sb || a.hostname.localeCompare(b.hostname);
  });

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>
          <Icon name="server" size={16} className="panel-title-icon" />
          Device Status
        </h2>
        <span className="panel-header-meta">Most urgent first · telemetry is the latest stored sample</span>
      </div>
      <div className="table-wrap device-status-wrap">
        {loading ? (
          <TableSkeleton rows={5} columns={8} />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Hostname</th>
                <th>Management IP</th>
                <th>Platform</th>
                <th>Access</th>
                <th>Health</th>
                <th>CPU</th>
                <th>Memory</th>
                <th>Uptime</th>
                <th>Response</th>
                <th>Last Seen</th>
                <th>Last Check</th>
                <th className="actions-cell">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((device) => {
                const h = health.get(device.id);
                const status = h?.status ?? device.health_status ?? "unknown";
                const sample = metrics.data?.get(device.id);
                return (
                  <tr key={device.id} className={status === "down" ? "row-alert" : status === "degraded" ? "row-warn" : undefined}>
                    <td className="cell-primary">
                      <Link className="device-link" to={`/devices/${device.id}`}>
                        {device.hostname}
                      </Link>
                    </td>
                    <td className="mono secondary">{device.management_ip}</td>
                    <td>
                      <span className="platform-tag">{device.platform}</span>
                    </td>
                    <td>{accessTag(device.platform)}</td>
                    <td title={h?.last_error ?? undefined}>
                      <StatusBadge status={status} />
                      <OperationTag operation={h ?? device} />
                    </td>
                    <td><MetricCell kind="cpu" value={sample?.cpu_percent} /></td>
                    <td><MetricCell kind="memory" value={sample?.memory_percent} /></td>
                    <td className="cell-num">{sample?.uptime_seconds != null ? formatUptime(sample.uptime_seconds) : <span className="muted">—</span>}</td>
                    <td className="cell-num">{responseTime(h?.response_time_ms ?? device.response_time_ms ?? null)}</td>
                    <td><RelativeTime value={h?.last_success_at ?? device.last_success_at ?? null} /></td>
                    <td><RelativeTime value={h?.last_check_at ?? device.last_check_at ?? null} /></td>
                    <td className="actions-cell">
                      <Link className="secondary-button small-button" to={`/devices/${device.id}`} aria-label={`Open ${device.hostname}`}>
                        View
                      </Link>
                    </td>
                  </tr>
                );
              })}
              {devices.length === 0 && (
                <tr>
                  <td colSpan={12}>
                    <EmptyState title="No devices found." />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
