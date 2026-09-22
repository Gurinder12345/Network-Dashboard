import { useEffect, useMemo, useState } from "react";
import { getApprovals, getDevices } from "../api/client";
import type { Approval, Device } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { formatTimestamp, truncateId } from "../utils/format";

const ALL_STATUSES = "all";

function ConfigDetail({ approval }: { approval: Approval }) {
  const parents = approval.config_parents ?? [];
  const lines = approval.config_lines ?? [];

  if (parents.length === 0 && lines.length === 0) {
    return <span className="mono">—</span>;
  }

  return (
    <div className="config-block">
      {parents.map((parent) => (
        <div className="config-parent" key={parent}>
          {parent}
        </div>
      ))}
      {lines.map((line, index) => (
        <div className="config-line" key={index}>
          {line}
        </div>
      ))}
    </div>
  );
}

export function Approvals() {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState(ALL_STATUSES);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [approvalsData, devicesData] = await Promise.all([
          getApprovals(),
          getDevices(),
        ]);

        if (cancelled) return;

        setApprovals(approvalsData);
        setDevices(devicesData);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load approvals");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => {
      cancelled = true;
    };
  }, []);

  const deviceHostnameById = useMemo(() => {
    const map = new Map<number, string>();
    devices.forEach((device) => map.set(device.id, device.hostname));
    return map;
  }, [devices]);

  const statuses = useMemo(
    () => Array.from(new Set(approvals.map((approval) => approval.status))).sort(),
    [approvals],
  );

  const filteredApprovals = useMemo(() => {
    const query = search.trim().toLowerCase();

    return approvals.filter((approval) => {
      const matchesStatus =
        statusFilter === ALL_STATUSES || approval.status === statusFilter;

      if (!matchesStatus) return false;

      if (query.length === 0) return true;

      const hostname = deviceHostnameById.get(approval.device_id) ?? "";
      const configText = [
        ...(approval.config_parents ?? []),
        ...(approval.config_lines ?? []),
      ].join(" ");

      const haystack = [
        hostname,
        approval.requested_by,
        approval.approved_by ?? "",
        configText,
      ]
        .join(" ")
        .toLowerCase();

      return haystack.includes(query);
    });
  }, [approvals, search, statusFilter, deviceHostnameById]);

  return (
    <>
      <h1 className="page-title">Approvals</h1>
      <p className="page-subtitle">Change approval history and pending requests.</p>

      {error && <div className="error-banner">Failed to load approvals: {error}</div>}

      <div className="filters-bar">
        <input
          type="text"
          className="search-input"
          placeholder="Search device, requester, or config..."
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="filter-select"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
        >
          <option value={ALL_STATUSES}>All statuses</option>
          {statuses.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Approvals</h2>
          <span className="count-tag">{filteredApprovals.length}</span>
        </div>
        <div className="panel-body" style={{ maxHeight: "none" }}>
          {loading ? (
            <div className="empty-state">Loading approvals&hellip;</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Device</th>
                  <th>Requested By</th>
                  <th>Approved By</th>
                  <th>Config</th>
                  <th>Created</th>
                  <th>Approved</th>
                  <th>Backup Job</th>
                </tr>
              </thead>
              <tbody>
                {filteredApprovals.map((approval) => (
                  <tr key={approval.id}>
                    <td>
                      <StatusBadge status={approval.status} />
                    </td>
                    <td>
                      {deviceHostnameById.get(approval.device_id) ??
                        `Device #${approval.device_id}`}
                    </td>
                    <td>{approval.requested_by}</td>
                    <td>{approval.approved_by ?? "—"}</td>
                    <td>
                      <ConfigDetail approval={approval} />
                    </td>
                    <td className="mono">{formatTimestamp(approval.created_at)}</td>
                    <td className="mono">{formatTimestamp(approval.approved_at)}</td>
                    <td className="mono" title={approval.backup_job_id ?? undefined}>
                      {truncateId(approval.backup_job_id)}
                    </td>
                  </tr>
                ))}
                {filteredApprovals.length === 0 && (
                  <tr>
                    <td colSpan={8} className="empty-state">
                      {approvals.length === 0
                        ? "No approvals recorded yet."
                        : "No approvals match your search or filter."}
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
