import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getApprovals,
  getDevices,
  getFleetHealth,
  getJobs,
  requestHealthCheck,
} from "../api/client";
import { OS6_PLATFORM } from "../api/constants";
import type { Approval, Device, FleetHealth, Job } from "../api/types";
import { KpiCard } from "../components/KpiCard";
import { StatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { formatResponseTime, formatTime, formatTimestamp } from "../utils/format";

const HEALTH_POLL_MS = 20000;
const CHECK_NOW_COOLDOWN_MS = 20000;

const FLEET_ROWS = [
  { key: "healthy", label: "Healthy" },
  { key: "degraded", label: "Degraded" },
  { key: "down", label: "Down" },
  { key: "unknown", label: "Unknown" },
] as const;

function FleetHealthPanel({
  fleet,
  error,
  onCheckNow,
  checkBusy,
  checkMessage,
}: {
  fleet: FleetHealth | null;
  error: string | null;
  onCheckNow: () => void;
  checkBusy: boolean;
  checkMessage: string | null;
}) {
  return (
    <div className="panel fleet-panel">
      <div className="panel-header">
        <h2>Fleet Health</h2>
        <div className="fleet-panel-actions">
          {fleet?.check_running && <span className="muted">Check running&hellip;</span>}
          <button
            type="button"
            className="refresh-button"
            onClick={onCheckNow}
            disabled={checkBusy || fleet?.check_running}
            title="Queue one read-only health sweep of all enabled switches"
          >
            Check Now
          </button>
        </div>
      </div>

      <div className="fleet-body">
        {error && <div className="error-banner" style={{ marginBottom: 0 }}>Health unavailable: {error}</div>}

        <div className="fleet-grid">
          <div className="fleet-cell">
            <span className="fleet-label">Total</span>
            <span className="fleet-value">{fleet ? fleet.total : "—"}</span>
          </div>
          {FLEET_ROWS.map((row) => (
            <div className={`fleet-cell fleet-${row.key}`} key={row.key}>
              <span className="fleet-label">
                <span className="fleet-dot" />
                {row.label}
              </span>
              <span className="fleet-value">{fleet ? fleet[row.key] : "—"}</span>
            </div>
          ))}
        </div>

        <div className="fleet-footer">
          <span>Last health update: {fleet ? formatTime(fleet.last_updated) : "—"}</span>
          {fleet && !fleet.last_updated && <span>No health checks have completed yet.</span>}
          {checkMessage && <span className="fleet-message">{checkMessage}</span>}
        </div>
      </div>
    </div>
  );
}

function summarizeConfig(approval: Approval): string {
  const parents = approval.config_parents ?? [];
  const lines = approval.config_lines ?? [];

  if (parents.length === 0 && lines.length === 0) return "—";

  return [...parents, ...lines].join(" → ");
}

export function Overview() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
  const [fleet, setFleet] = useState<FleetHealth | null>(null);
  const [fleetError, setFleetError] = useState<string | null>(null);
  const [checkBusy, setCheckBusy] = useState(false);
  const [checkMessage, setCheckMessage] = useState<string | null>(null);

  // Reads stored health only (Redis/PostgreSQL); never starts a switch check.
  const loadHealth = useCallback(async () => {
    try {
      setFleet(await getFleetHealth());
      setFleetError(null);
    } catch (err) {
      setFleetError(err instanceof Error ? err.message : "Failed to load health");
    }
  }, []);

  const load = useCallback(async () => {
    setRefreshing(true);

    try {
      const [devicesData, jobsData, approvalsData] = await Promise.all([
        getDevices(),
        getJobs(),
        getApprovals(),
        loadHealth(),
      ]);

      setDevices(devicesData);
      setJobs(jobsData);
      setApprovals(approvalsData);
      setError(null);
      setLastRefreshedAt(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard data");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [loadHealth]);

  useEffect(() => {
    load();
  }, [load]);

  usePolling(loadHealth, HEALTH_POLL_MS);

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
    await loadHealth();
  }

  const healthByDevice = useMemo(() => {
    const map = new Map<number, FleetHealth["devices"][number]>();
    fleet?.devices.forEach((entry) => map.set(entry.device_id, entry));
    return map;
  }, [fleet]);

  const deviceHostnameById = useMemo(() => {
    const map = new Map<number, string>();
    devices.forEach((device) => map.set(device.id, device.hostname));
    return map;
  }, [devices]);

  const pendingApprovals = useMemo(
    () => approvals.filter((approval) => approval.status === "pending"),
    [approvals],
  );

  const failedJobs = useMemo(
    () => jobs.filter((job) => job.status === "failed"),
    [jobs],
  );

  const recentChanges = approvals.slice(0, 5);
  const pendingApprovalsPreview = pendingApprovals.slice(0, 5);

  return (
    <>
      <div className="page-toolbar">
        <div>
          <h1 className="page-title">Overview</h1>
          <p className="page-subtitle">Platform status at a glance.</p>
        </div>
        <div className="refresh-control">
          <span className="last-refreshed">
            Last refreshed: {lastRefreshedAt ? lastRefreshedAt.toLocaleTimeString() : "—"}
          </span>
          <button
            type="button"
            className="refresh-button"
            onClick={() => load()}
            disabled={refreshing}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && <div className="error-banner">Failed to load dashboard data: {error}</div>}

      <div className="kpi-grid">
        <KpiCard label="Total Devices" value={loading ? "—" : devices.length} />
        <KpiCard
          label="Healthy Devices"
          value={fleet ? `${fleet.healthy} / ${fleet.total}` : "—"}
          dim={!fleet || !fleet.last_updated}
          hint={fleet && !fleet.last_updated ? "Awaiting first health check" : undefined}
        />
        <KpiCard
          label="Pending Approvals"
          value={loading ? "—" : pendingApprovals.length}
        />
        <KpiCard label="Failed Jobs" value={loading ? "—" : failedJobs.length} />
      </div>

      <FleetHealthPanel
        fleet={fleet}
        error={fleetError}
        onCheckNow={handleCheckNow}
        checkBusy={checkBusy}
        checkMessage={checkMessage}
      />

      <div className="panel-grid">
        <div className="panel">
          <div className="panel-header">
            <h2>Recent Changes</h2>
            <span className="count-tag">{recentChanges.length}</span>
          </div>
          <div className="panel-body">
            {recentChanges.length === 0 ? (
              <div className="empty-state">No changes recorded yet.</div>
            ) : (
              recentChanges.map((approval) => (
                <div className="list-row" key={approval.id}>
                  <div className="list-row-top">
                    <span className="list-row-title">
                      {deviceHostnameById.get(approval.device_id) ??
                        `Device #${approval.device_id}`}
                    </span>
                    <StatusBadge status={approval.status} />
                  </div>
                  <div className="list-row-config" title={summarizeConfig(approval)}>
                    {summarizeConfig(approval)}
                  </div>
                  <div className="list-row-meta">
                    requested by {approval.requested_by} &middot;{" "}
                    {formatTimestamp(approval.created_at)}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <h2>Pending Approvals</h2>
            <span className="count-tag">{pendingApprovals.length}</span>
          </div>
          <div className="panel-body">
            {pendingApprovalsPreview.length === 0 ? (
              <div className="empty-state">Nothing awaiting approval.</div>
            ) : (
              pendingApprovalsPreview.map((approval) => (
                <div className="list-row" key={approval.id}>
                  <div className="list-row-top">
                    <span className="list-row-title">
                      {deviceHostnameById.get(approval.device_id) ??
                        `Device #${approval.device_id}`}
                    </span>
                    <StatusBadge status={approval.status} />
                  </div>
                  <div className="list-row-config" title={summarizeConfig(approval)}>
                    {summarizeConfig(approval)}
                  </div>
                  <div className="list-row-meta">
                    requested by {approval.requested_by} &middot;{" "}
                    {formatTimestamp(approval.created_at)}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Devices</h2>
          <span className="count-tag">{devices.length}</span>
        </div>
        <div className="panel-body" style={{ maxHeight: "none" }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Hostname</th>
                <th>Management IP</th>
                <th>Platform</th>
                <th>Access</th>
                <th>Health</th>
                <th>Response</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((device) => {
                const health = healthByDevice.get(device.id);
                return (
                <tr key={device.id}>
                  <td>{device.hostname}</td>
                  <td className="mono">{device.management_ip}</td>
                  <td>
                    <span className="platform-tag">{device.platform}</span>
                  </td>
                  <td>
                    {device.platform === OS6_PLATFORM ? (
                      "Actionable"
                    ) : (
                      <span className="view-only-tag">View only</span>
                    )}
                  </td>
                  <td title={health?.last_error ?? undefined}>
                    <StatusBadge status={health?.status ?? device.health_status ?? "unknown"} />
                  </td>
                  <td className="mono">
                    {formatResponseTime(health?.response_time_ms ?? device.response_time_ms ?? null)}
                  </td>
                </tr>
                );
              })}
              {devices.length === 0 && !loading && (
                <tr>
                  <td colSpan={6} className="empty-state">
                    No devices found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
