import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getApprovals, getDevices, getJobs, requestHealthCheck } from "../api/client";
import { OS10_PLATFORM, OS6_PLATFORM } from "../api/constants";
import type { Approval, Device, DeviceHealthEntry, FleetHealth, HealthStatus, Job } from "../api/types";
import { Banner, EmptyState, StaleDataWarning, TableSkeleton } from "../components/Feedback";
import { KpiCard } from "../components/KpiCard";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge, type Tone } from "../components/StatusBadge";
import { isHealthStale, useFleetHealth } from "../hooks/FleetHealthContext";
import { formatRelative, formatResponseTime, humanize, truncateText } from "../utils/format";

const CHECK_NOW_COOLDOWN_MS = 20000;

const HEALTH_ORDER: HealthStatus[] = ["healthy", "degraded", "down", "unknown"];
const HEALTH_LABELS: Record<HealthStatus, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  down: "Down",
  unknown: "Unknown",
};
// Most urgent first in device lists.
const SEVERITY: Record<string, number> = { down: 0, degraded: 1, unknown: 2, healthy: 3 };

function summarizeConfig(approval: Approval): string {
  const items = [...(approval.config_parents ?? []), ...(approval.config_lines ?? [])];
  return items.length === 0 ? "—" : items.join(" › ");
}

function FleetHealthPanel({
  fleet,
  onCheckNow,
  checkBusy,
  checkMessage,
}: {
  fleet: FleetHealth | null;
  onCheckNow: () => void;
  checkBusy: boolean;
  checkMessage: string | null;
}) {
  const total = fleet?.total ?? 0;

  return (
    <div className="panel fleet-panel">
      <div className="panel-header">
        <h2>Fleet Health</h2>
        <div className="fleet-panel-actions">
          {fleet?.check_running && <StatusBadge status="running" label="Check running" />}
          <button
            type="button"
            className="secondary-button small-button"
            onClick={onCheckNow}
            disabled={checkBusy || fleet?.check_running}
            title="Queue one read-only health sweep of all enabled switches"
          >
            Check now
          </button>
        </div>
      </div>

      <div className="fleet-body">
        {!fleet ? (
          <>
            <span className="skeleton" style={{ height: 26, width: 160 }} />
            <span className="skeleton" style={{ height: 12 }} />
          </>
        ) : (
          <>
            <div className="fleet-headline">
              <span className="fleet-headline-value">
                {fleet.healthy}
                <span className="muted"> / {total}</span>
              </span>
              <span className="fleet-headline-label">enabled devices healthy</span>
            </div>

            <div
              className="health-bar"
              role="img"
              aria-label={HEALTH_ORDER.map((s) => `${fleet[s]} ${s}`).join(", ")}
            >
              {total > 0 &&
                HEALTH_ORDER.filter((status) => fleet[status] > 0).map((status) => (
                  <span
                    key={status}
                    className={`health-bar-seg seg-${status}`}
                    style={{ width: `${(fleet[status] / total) * 100}%` }}
                    title={`${HEALTH_LABELS[status]}: ${fleet[status]}`}
                  />
                ))}
            </div>

            <div className="health-legend">
              {HEALTH_ORDER.map((status) => (
                <div className="legend-item" key={status}>
                  <span className="legend-label">
                    <span className={`tone-dot dot-${status}`} aria-hidden="true" />
                    {HEALTH_LABELS[status]}
                  </span>
                  <span className="legend-value">{fleet[status]}</span>
                  <span className="legend-pct">
                    {total > 0 ? `${Math.round((fleet[status] / total) * 100)}%` : "—"}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}

        <div className="fleet-footer">
          <span title={fleet?.last_updated ?? undefined}>
            Last health update:{" "}
            {fleet?.last_updated
              ? `${new Date(fleet.last_updated).toLocaleTimeString()} (${formatRelative(fleet.last_updated)})`
              : "—"}
          </span>
          {fleet && !fleet.last_updated && <span>No health checks have completed yet.</span>}
          {checkMessage && <span className="fleet-message">{checkMessage}</span>}
        </div>
      </div>
    </div>
  );
}

function PlatformSummary({ devices, health }: { devices: Device[]; health: Map<number, DeviceHealthEntry> }) {
  const platforms = useMemo(() => {
    const groups = new Map<string, Device[]>();
    devices.forEach((device) => groups.set(device.platform, [...(groups.get(device.platform) ?? []), device]));
    return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [devices]);

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Platforms</h2>
        <span className="count-tag">{platforms.length}</span>
      </div>
      <div className="platform-list">
        {platforms.length === 0 && <EmptyState title="No devices" />}
        {platforms.map(([platform, list]) => {
          const healthy = list.filter((d) => health.get(d.id)?.status === "healthy").length;
          const enabled = list.filter((d) => d.enabled).length;
          return (
            <div className="platform-row" key={platform}>
              <div>
                <span className="platform-tag">{platform}</span>{" "}
                {platform === OS6_PLATFORM ? (
                  <span className="access-tag actionable">Actionable</span>
                ) : platform === OS10_PLATFORM ? (
                  <span className="access-tag actionable" title="Dell OS10 safe L2 changes on eligible ethernet interfaces">
                    Actionable · safe L2
                  </span>
                ) : null}
              </div>
              <span className="platform-row-count">{list.length}</span>
              <div className="platform-row-meta">
                <span>{enabled} enabled</span>
                <span>{healthy} healthy</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface ComponentStatus {
  name: string;
  tone: Tone;
  detail: string;
  title: string;
}

// Every state below is derived from responses the dashboard already receives; nothing
// is reported for components the API does not expose.
function deriveComponents(
  fleet: FleetHealth | null,
  apiOk: boolean,
  dbOk: boolean | null,
): ComponentStatus[] {
  const lastUpdated = fleet?.last_updated ?? null;
  const stale = isHealthStale(lastUpdated);
  const recentLogin = fleet?.devices.some(
    (d) => d.cli_reachable && d.last_success_at && !isHealthStale(d.last_success_at),
  );

  return [
    {
      name: "API",
      tone: apiOk ? "success" : "danger",
      detail: apiOk ? "Responding" : "Not responding",
      title: "Result of the dashboard's latest API poll",
    },
    {
      name: "PostgreSQL",
      tone: dbOk === null ? "neutral" : dbOk ? "success" : "warning",
      detail: dbOk === null ? "Not confirmed yet" : dbOk ? "Reachable via API" : "Jobs/approvals query failed",
      title: "Inferred: the jobs and approvals lists are read directly from PostgreSQL",
    },
    {
      name: "Beat · Redis · Worker",
      tone: !lastUpdated ? "neutral" : stale ? "warning" : "success",
      detail: !lastUpdated
        ? "No health sweep completed yet"
        : stale
          ? `No sweep since ${formatRelative(lastUpdated)}`
          : `Sweeps running · ${formatRelative(lastUpdated)}`,
      title: "Inferred: scheduled health sweeps need Beat, the Redis broker and a worker",
    },
    {
      name: "Vault",
      tone: recentLogin ? "success" : "neutral",
      detail: recentLogin ? "Credentials in use (SSH login OK)" : "No recent device login to confirm",
      title: "Inferred: a successful health-check SSH login required Vault credentials",
    },
  ];
}

export function Overview() {
  const { fleet, error: fleetError, lastSuccessAt, refresh: refreshHealth } = useFleetHealth();
  const [devices, setDevices] = useState<Device[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dbOk, setDbOk] = useState<boolean | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
  const [checkBusy, setCheckBusy] = useState(false);
  const [checkMessage, setCheckMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);

    try {
      const [devicesData, jobsData, approvalsData] = await Promise.all([
        getDevices(),
        getJobs(),
        getApprovals(),
        refreshHealth(),
      ]);

      setDevices(devicesData);
      setJobs(jobsData);
      setApprovals(approvalsData);
      setError(null);
      setDbOk(true);
      setLastRefreshedAt(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard data");
      setDbOk((previous) => (previous === null ? null : false));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [refreshHealth]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleCheckNow() {
    setCheckBusy(true);

    try {
      await requestHealthCheck();
      setCheckMessage("Health check requested. Results appear within about a minute.");
    } catch (err) {
      setCheckMessage(err instanceof Error ? err.message : "Health check request failed");
    }

    // Keep the button disabled briefly so repeated clicks don't queue more sweeps.
    window.setTimeout(() => setCheckBusy(false), CHECK_NOW_COOLDOWN_MS);
    await refreshHealth();
  }

  const healthByDevice = useMemo(() => {
    const map = new Map<number, DeviceHealthEntry>();
    fleet?.devices.forEach((entry) => map.set(entry.device_id, entry));
    return map;
  }, [fleet]);

  const deviceHostnameById = useMemo(() => {
    const map = new Map<number, string>();
    devices.forEach((device) => map.set(device.id, device.hostname));
    return map;
  }, [devices]);

  const pendingApprovals = useMemo(() => approvals.filter((a) => a.status === "pending"), [approvals]);
  const failedJobs = useMemo(() => jobs.filter((job) => job.status === "failed"), [jobs]);

  const sortedDevices = useMemo(
    () =>
      [...devices].sort((a, b) => {
        const sa = SEVERITY[healthByDevice.get(a.id)?.status ?? a.health_status ?? "unknown"] ?? 9;
        const sb = SEVERITY[healthByDevice.get(b.id)?.status ?? b.health_status ?? "unknown"] ?? 9;
        return sa - sb || a.hostname.localeCompare(b.hostname);
      }),
    [devices, healthByDevice],
  );

  const components = deriveComponents(fleet, fleetError === null && lastSuccessAt !== null, dbOk);
  const healthStale = fleet ? isHealthStale(fleet.last_updated) : false;
  const hasChecks = Boolean(fleet?.last_updated);

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Fleet health, change control and recent activity."
        lastUpdated={lastRefreshedAt}
        actions={
          <button type="button" className="refresh-button" onClick={() => load()} disabled={refreshing}>
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        }
      />

      {error && lastRefreshedAt && <StaleDataWarning since={lastRefreshedAt} error={error} />}
      {error && !lastRefreshedAt && (
        <Banner tone="danger" title="Failed to load dashboard data.">
          {error}
        </Banner>
      )}
      {fleetError && fleet && <StaleDataWarning since={lastSuccessAt} error={fleetError} />}
      {healthStale && fleet?.last_updated && (
        <Banner tone="warning" title="Health data is stale.">
          The last health sweep finished {formatRelative(fleet.last_updated)}. Sweeps run every
          60 s; check the network-beat and network-worker pods.
        </Banner>
      )}

      <div className="kpi-grid">
        <KpiCard label="Total Devices" value={loading ? "—" : devices.length} tone="info" />
        <KpiCard
          label="Healthy"
          value={fleet ? fleet.healthy : "—"}
          tone="success"
          dim={!hasChecks}
          hint={fleet && !hasChecks ? "Awaiting first check" : undefined}
        />
        <KpiCard
          label="Degraded"
          value={fleet ? fleet.degraded : "—"}
          tone="warning"
          emphasize={(fleet?.degraded ?? 0) > 0}
          dim={!hasChecks}
        />
        <KpiCard
          label="Down"
          value={fleet ? fleet.down : "—"}
          tone="danger"
          emphasize={(fleet?.down ?? 0) > 0}
          dim={!hasChecks}
        />
        <KpiCard label="Unknown" value={fleet ? fleet.unknown : "—"} tone="neutral" dim={!hasChecks} />
        <KpiCard
          label="Pending Approvals"
          value={loading ? "—" : pendingApprovals.length}
          tone="warning"
          emphasize={pendingApprovals.length > 0}
        />
        <KpiCard
          label="Failed Jobs"
          value={loading ? "—" : failedJobs.length}
          tone="danger"
          hint="All recorded jobs"
        />
      </div>

      <div className="components-strip" aria-label="System components">
        {components.map((component) => (
          <div className="component-chip" key={component.name} title={component.title}>
            <span className="component-chip-name">
              <span className={`tone-dot tone-${component.tone}`} aria-hidden="true" />
              {component.name}
            </span>
            <span className="component-chip-detail">{component.detail}</span>
          </div>
        ))}
      </div>

      <div className="overview-grid">
        <FleetHealthPanel
          fleet={fleet}
          onCheckNow={handleCheckNow}
          checkBusy={checkBusy}
          checkMessage={checkMessage}
        />
        <PlatformSummary devices={devices} health={healthByDevice} />
      </div>

      <div className="panel-grid">
        <div className="panel">
          <div className="panel-header">
            <h2>Recent Changes</h2>
            <Link to="/approvals" className="panel-header-meta">
              View approvals →
            </Link>
          </div>
          <div className="panel-body">
            {loading ? (
              <TableSkeleton rows={4} columns={2} />
            ) : approvals.length === 0 ? (
              <EmptyState title="No changes recorded yet." hint="Run a precheck from Changes to start one." />
            ) : (
              approvals.slice(0, 6).map((approval) => (
                <div className="list-row" key={approval.id}>
                  <div className="list-row-top">
                    <span className="list-row-title">
                      {deviceHostnameById.get(approval.device_id) ?? `Device #${approval.device_id}`}
                    </span>
                    <StatusBadge status={approval.status} />
                  </div>
                  <div className="list-row-config" title={summarizeConfig(approval)}>
                    {summarizeConfig(approval)}
                  </div>
                  <div className="list-row-meta">
                    requested by {approval.requested_by} · <RelativeTime value={approval.created_at} />
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <h2>Recent Failed Jobs</h2>
            <Link to="/jobs" className="panel-header-meta">
              View jobs →
            </Link>
          </div>
          <div className="panel-body">
            {loading ? (
              <TableSkeleton rows={4} columns={2} />
            ) : failedJobs.length === 0 ? (
              <EmptyState title="No failed jobs." />
            ) : (
              failedJobs.slice(0, 6).map((job) => (
                <div className="list-row" key={job.id}>
                  <div className="list-row-top">
                    <span className="list-row-title">
                      {deviceHostnameById.get(job.device_id) ?? `Device #${job.device_id}`}
                    </span>
                    <StatusBadge status={job.status} />
                  </div>
                  <div className="list-row-config" title={job.error_message ?? undefined}>
                    {humanize(job.job_type)}
                    {job.error_message ? ` — ${truncateText(job.error_message, 90)}` : ""}
                  </div>
                  <div className="list-row-meta">
                    <RelativeTime value={job.finished_at ?? job.started_at} />
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Device Health</h2>
          <span className="panel-header-meta">Most urgent first</span>
        </div>
        <div className="table-wrap" style={{ maxHeight: 420 }}>
          {loading ? (
            <TableSkeleton rows={5} columns={6} />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Hostname</th>
                  <th>Management IP</th>
                  <th>Platform</th>
                  <th>Access</th>
                  <th>Health</th>
                  <th>Response</th>
                  <th>Last Check</th>
                </tr>
              </thead>
              <tbody>
                {sortedDevices.map((device) => {
                  const health = healthByDevice.get(device.id);
                  const status = health?.status ?? device.health_status ?? "unknown";
                  return (
                    <tr
                      key={device.id}
                      className={status === "down" ? "row-alert" : status === "degraded" ? "row-warn" : undefined}
                    >
                      <td className="cell-primary">{device.hostname}</td>
                      <td className="mono secondary">{device.management_ip}</td>
                      <td>
                        <span className="platform-tag">{device.platform}</span>
                      </td>
                      <td>
                        {device.platform === OS6_PLATFORM ? (
                          <span className="access-tag actionable">Actionable</span>
                        ) : (
                          <span className="view-only-tag">View only</span>
                        )}
                      </td>
                      <td title={health?.last_error ?? undefined}>
                        <StatusBadge status={status} />
                      </td>
                      <td className="cell-num">
                        {formatResponseTime(health?.response_time_ms ?? device.response_time_ms ?? null)}
                      </td>
                      <td>
                        <RelativeTime value={health?.last_check_at ?? device.last_check_at ?? null} />
                      </td>
                    </tr>
                  );
                })}
                {devices.length === 0 && (
                  <tr>
                    <td colSpan={7}>
                      <EmptyState title="No devices found." />
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}
