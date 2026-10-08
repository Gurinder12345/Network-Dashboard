import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  getApprovals,
  getAuditEvents,
  getBackups,
  getDeviceMetricsLatest,
  getDevices,
  getJobs,
  getTopology,
  requestHealthCheck,
} from "../api/client";
import { OS10_PLATFORM, OS6_PLATFORM } from "../api/constants";
import type {
  Approval,
  AuditEvent,
  Backup,
  Device,
  DeviceHealthEntry,
  DeviceTelemetry,
  FleetHealth,
  HealthStatus,
  Job,
  TopologyGraph,
} from "../api/types";
import { Donut } from "../components/charts";
import {
  BackupStatusCard,
  COLORS,
  ChangeActivity,
  DeviceStatusTable,
  HealthTrendCard,
  PlatformDistribution,
  RecentIncidents,
  TopologyPreview,
  UtilizationCard,
  type Loadable,
} from "../components/overview/Widgets";
import { Banner, EmptyState, StaleDataWarning, TableSkeleton } from "../components/Feedback";
import { Icon, IconTile, type IconName } from "../components/Icon";
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
const HEALTH_ICONS: Record<HealthStatus, { icon: IconName; tone: Tone }> = {
  healthy: { icon: "server", tone: "success" },
  degraded: { icon: "alert", tone: "warning" },
  down: { icon: "arrowDown", tone: "danger" },
  unknown: { icon: "help", tone: "neutral" },
};

function percentOf(count: number | undefined, total: number | undefined): string | undefined {
  if (count === undefined || !total) return undefined;
  return `${Math.round((count / total) * 100)}% of fleet`;
}

/** Access level per platform: both Dell platforms accept guarded configuration changes. */
export function AccessTag({ platform }: { platform: string }) {
  if (platform === OS6_PLATFORM || platform === OS10_PLATFORM) {
    return (
      <span className="access-tag actionable" title="Guarded multi-block changes (precheck, backup, approval)">
        Actionable
      </span>
    );
  }
  return <span className="view-only-tag">View only</span>;
}

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
        <h2>
          <Icon name="pulse" size={17} className="panel-title-icon" />
          Fleet Health
        </h2>
        <div className="fleet-panel-actions">
          {fleet?.check_running && <StatusBadge status="running" label="Check running" />}
          <button
            type="button"
            className="secondary-button small-button"
            onClick={onCheckNow}
            disabled={checkBusy || fleet?.check_running}
            title="Queue one read-only health sweep of all enabled switches"
          >
            <Icon name="play" size={12} />
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
          <div className="fleet-layout">
            <Donut
              segments={HEALTH_ORDER.map((status) => ({
                key: status,
                label: HEALTH_LABELS[status],
                value: fleet[status],
                color: COLORS[status],
              }))}
              centerValue={total}
              centerLabel="enabled"
              ariaLabel={`Fleet health: ${HEALTH_ORDER.map((s) => `${fleet[s]} ${s}`).join(", ")}`}
              size={120}
            />
            <div className="fleet-detail">
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
                <div className={`legend-item legend-${status}`} key={status}>
                  <div className="legend-text">
                    <span className="legend-label">
                      <span className={`tone-dot dot-${status}`} aria-hidden="true" />
                      {HEALTH_LABELS[status]}
                    </span>
                    <span className="legend-value">{fleet[status]}</span>
                    <span className="legend-pct">
                      {total > 0 ? `${Math.round((fleet[status] / total) * 100)}%` : "—"}
                    </span>
                  </div>
                  <IconTile name={HEALTH_ICONS[status].icon} tone={HEALTH_ICONS[status].tone} />
                </div>
              ))}
            </div>
            </div>
          </div>
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

interface ComponentStatus {
  name: string;
  icon: IconName;
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
      icon: "server",
      tone: apiOk ? "success" : "danger",
      detail: apiOk ? "Responding" : "Not responding",
      title: "Result of the dashboard's latest API poll",
    },
    {
      name: "PostgreSQL",
      icon: "database",
      tone: dbOk === null ? "neutral" : dbOk ? "success" : "warning",
      detail: dbOk === null ? "Not confirmed yet" : dbOk ? "Reachable via API" : "Jobs/approvals query failed",
      title: "Inferred: the jobs and approvals lists are read directly from PostgreSQL",
    },
    {
      name: "Beat · Redis · Worker",
      icon: "box",
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
      icon: "shield",
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
  // Widget data loaded independently (one request each on load / Refresh; no extra polling).
  const [backups, setBackups] = useState<Loadable<Backup[]>>({ data: null, error: null, loading: true });
  const [audit, setAudit] = useState<Loadable<AuditEvent[]>>({ data: null, error: null, loading: true });
  const [topology, setTopology] = useState<Loadable<TopologyGraph>>({ data: null, error: null, loading: true });
  const [metrics, setMetrics] = useState<Loadable<Map<number, DeviceTelemetry>>>({ data: null, error: null, loading: true });

  const loadWidgets = useCallback(async (deviceList: Device[]) => {
    const settle = <T,>(promise: Promise<T>, set: (value: Loadable<T>) => void) =>
      promise.then(
        (data) => set({ data, error: null, loading: false }),
        (err) => set({ data: null, error: err instanceof Error ? err.message : "Request failed", loading: false }),
      );
    // Latest stored telemetry per enabled device (Redis-backed reads; never polls switches).
    const enabled = deviceList.filter((d) => d.enabled);
    const metricsPromise = Promise.allSettled(enabled.map((d) => getDeviceMetricsLatest(d.id))).then((results) => {
      const map = new Map<number, DeviceTelemetry>();
      results.forEach((r, i) => {
        if (r.status === "fulfilled") map.set(enabled[i].id, r.value);
      });
      if (map.size === 0 && results.some((r) => r.status === "rejected")) throw new Error("Telemetry unavailable");
      return map;
    });
    await Promise.all([
      settle(getBackups(), (v) => setBackups((prev) => (v.error && prev.data ? { ...prev, error: v.error, loading: false } : v))),
      settle(getAuditEvents(), (v) => setAudit((prev) => (v.error && prev.data ? { ...prev, error: v.error, loading: false } : v))),
      settle(getTopology(), (v) => setTopology((prev) => (v.error && prev.data ? { ...prev, error: v.error, loading: false } : v))),
      settle(metricsPromise, (v) => setMetrics((prev) => (v.error && prev.data ? { ...prev, error: v.error, loading: false } : v))),
    ]);
  }, []);

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
      void loadWidgets(devicesData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard data");
      setDbOk((previous) => (previous === null ? null : false));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [refreshHealth, loadWidgets]);

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
            <Icon name="refresh" size={14} />
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
        <KpiCard
          label="Total Devices"
          value={loading ? "—" : devices.length}
          tone="info"
          icon="server"
          hint={fleet ? `${fleet.total} enabled` : undefined}
        />
        <KpiCard
          label="Healthy"
          value={fleet ? fleet.healthy : "—"}
          tone="success"
          icon="check"
          dim={!hasChecks}
          hint={fleet && !hasChecks ? "Awaiting first check" : hasChecks ? percentOf(fleet?.healthy, fleet?.total) : undefined}
        />
        <KpiCard
          label="Degraded"
          value={fleet ? fleet.degraded : "—"}
          tone="warning"
          icon="alert"
          emphasize={(fleet?.degraded ?? 0) > 0}
          dim={!hasChecks}
          hint={hasChecks ? percentOf(fleet?.degraded, fleet?.total) : undefined}
        />
        <KpiCard
          label="Down"
          value={fleet ? fleet.down : "—"}
          tone="danger"
          icon="alertCircle"
          emphasize={(fleet?.down ?? 0) > 0}
          dim={!hasChecks}
          hint={hasChecks ? percentOf(fleet?.down, fleet?.total) : undefined}
        />
        <KpiCard
          label="Unknown"
          value={fleet ? fleet.unknown : "—"}
          tone="neutral"
          icon="help"
          dim={!hasChecks}
          hint={hasChecks ? percentOf(fleet?.unknown, fleet?.total) : undefined}
        />
        <KpiCard
          label="Pending Approvals"
          value={loading ? "—" : pendingApprovals.length}
          tone="warning"
          icon="fileCheck"
          emphasize={pendingApprovals.length > 0}
          hint={loading ? undefined : `${pendingApprovals.length} awaiting review`}
        />
        <KpiCard
          label="Failed Jobs"
          value={loading ? "—" : failedJobs.length}
          tone="danger"
          icon="gear"
          hint="All recorded jobs"
        />
      </div>

      <div className="components-strip" aria-label="System components">
        {components.map((component) => (
          <div className="component-chip" key={component.name} title={component.title}>
            <IconTile name={component.icon} tone="neutral" />
            <div className="component-chip-text">
              <span className="component-chip-name">
                <span className={`tone-dot tone-${component.tone}`} aria-hidden="true" />
                {component.name}
              </span>
              <span className="component-chip-detail">{component.detail}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="dash-row dash-row-fleet">
        <FleetHealthPanel
          fleet={fleet}
          onCheckNow={handleCheckNow}
          checkBusy={checkBusy}
          checkMessage={checkMessage}
        />
        <HealthTrendCard />
        <PlatformDistribution devices={devices} health={healthByDevice} loading={loading} />
      </div>

      <div className="dash-row">
        <UtilizationCard kind="cpu" devices={devices} metrics={metrics} />
        <UtilizationCard kind="memory" devices={devices} metrics={metrics} />
        <ChangeActivity approvals={approvals} loading={loading} />
      </div>

      <div className="dash-row">
        <RecentIncidents
          fleet={fleet}
          audit={audit}
          deviceName={(id) => (id === null ? "Platform" : deviceHostnameById.get(id) ?? `Device #${id}`)}
        />
        <TopologyPreview topology={topology} />
        <BackupStatusCard devices={devices} backups={backups} jobs={jobs} />
      </div>

      <div className="panel-grid">
        <div className="panel">
          <div className="panel-header">
            <h2>
              <Icon name="clock" size={17} className="panel-title-icon" />
              Recent Changes
            </h2>
            <Link to="/approvals" className="panel-header-meta panel-header-link">
              View approvals <Icon name="arrowRight" size={13} />
            </Link>
          </div>
          <div className="panel-body">
            {loading ? (
              <TableSkeleton rows={4} columns={2} />
            ) : approvals.length === 0 ? (
              <EmptyState title="No changes recorded yet." hint="Run a precheck from Changes to start one." />
            ) : (
              approvals.slice(0, 6).map((approval) => (
                <Link className="list-row list-row-rich" key={approval.id} to="/approvals" title="Open Approvals">
                  <IconTile name="server" tone="neutral" />
                  <div className="list-row-main">
                    <span className="list-row-title">
                      {deviceHostnameById.get(approval.device_id) ?? `Device #${approval.device_id}`}
                    </span>
                    <div className="list-row-config" title={summarizeConfig(approval)}>
                      {summarizeConfig(approval)}
                    </div>
                    <div className="list-row-meta">
                      requested by {approval.requested_by} · <RelativeTime value={approval.created_at} />
                    </div>
                  </div>
                  <StatusBadge status={approval.status} />
                  <Icon name="chevronRight" size={16} className="row-chevron" />
                </Link>
              ))
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <h2>
              <Icon name="alert" size={17} className="panel-title-icon tone-text-danger" />
              Recent Failed Jobs
            </h2>
            <Link to="/jobs" className="panel-header-meta panel-header-link">
              View jobs <Icon name="arrowRight" size={13} />
            </Link>
          </div>
          <div className="panel-body">
            {loading ? (
              <TableSkeleton rows={4} columns={2} />
            ) : failedJobs.length === 0 ? (
              <EmptyState title="No failed jobs." />
            ) : (
              failedJobs.slice(0, 6).map((job) => (
                <Link className="list-row list-row-rich" key={job.id} to="/jobs" title="Open Jobs for the full error">
                  <IconTile name="server" tone="neutral" />
                  <div className="list-row-main">
                    <span className="list-row-title">
                      {deviceHostnameById.get(job.device_id) ?? `Device #${job.device_id}`}
                    </span>
                    <div className="list-row-config" title={job.error_message ?? undefined}>
                      {humanize(job.job_type)}
                      {job.error_message ? ` — ${truncateText(job.error_message, 90)}` : ""}
                    </div>
                    <div className="list-row-meta">
                      <RelativeTime value={job.finished_at ?? job.started_at} />
                    </div>
                  </div>
                  <StatusBadge status={job.status} />
                  <Icon name="chevronRight" size={16} className="row-chevron" />
                </Link>
              ))
            )}
          </div>
        </div>
      </div>

      <DeviceStatusTable
        devices={devices}
        health={healthByDevice}
        metrics={metrics}
        loading={loading}
        accessTag={(platform) => <AccessTag platform={platform} />}
        responseTime={formatResponseTime}
      />
    </>
  );
}
