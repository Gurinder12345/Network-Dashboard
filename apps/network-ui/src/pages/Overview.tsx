import { useCallback, useEffect, useMemo, useState } from "react";
import { getApprovals, getDevices, getJobs } from "../api/client";
import { OS6_PLATFORM } from "../api/constants";
import type { Approval, Device, Job } from "../api/types";
import { KpiCard } from "../components/KpiCard";
import { StatusBadge } from "../components/StatusBadge";
import { formatTimestamp } from "../utils/format";

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

  const load = useCallback(async () => {
    setRefreshing(true);

    try {
      const [devicesData, jobsData, approvalsData] = await Promise.all([
        getDevices(),
        getJobs(),
        getApprovals(),
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
  }, []);

  useEffect(() => {
    load();
  }, [load]);

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
          value="—"
          dim
          hint="Health monitoring not implemented yet"
        />
        <KpiCard
          label="Pending Approvals"
          value={loading ? "—" : pendingApprovals.length}
        />
        <KpiCard label="Failed Jobs" value={loading ? "—" : failedJobs.length} />
      </div>

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
              </tr>
            </thead>
            <tbody>
              {devices.map((device) => (
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
                  <td>
                    <StatusBadge status="unknown" />
                  </td>
                </tr>
              ))}
              {devices.length === 0 && !loading && (
                <tr>
                  <td colSpan={5} className="empty-state">
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
