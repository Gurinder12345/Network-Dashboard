import { useEffect, useMemo, useState } from "react";
import { getDevices, getJobs } from "../api/client";
import type { Device, Job } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { formatTimestamp, truncateText } from "../utils/format";

const ALL_STATUSES = "all";
const ERROR_PREVIEW_LENGTH = 80;

function ErrorMessageCell({ message }: { message: string | null }) {
  if (!message) return <span className="mono">—</span>;

  const isLong = message.length > ERROR_PREVIEW_LENGTH || message.includes("\n");

  if (!isLong) {
    return <span className="mono">{message}</span>;
  }

  return (
    <details className="error-details">
      <summary className="error-summary mono">
        {truncateText(message, ERROR_PREVIEW_LENGTH)}
      </summary>
      <pre className="error-full">{message}</pre>
    </details>
  );
}

export function Jobs() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState(ALL_STATUSES);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [jobsData, devicesData] = await Promise.all([getJobs(), getDevices()]);

        if (cancelled) return;

        setJobs(jobsData);
        setDevices(devicesData);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load jobs");
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
    () => Array.from(new Set(jobs.map((job) => job.status))).sort(),
    [jobs],
  );

  const filteredJobs = useMemo(() => {
    const query = search.trim().toLowerCase();

    return jobs.filter((job) => {
      const matchesStatus = statusFilter === ALL_STATUSES || job.status === statusFilter;

      if (!matchesStatus) return false;

      if (query.length === 0) return true;

      const hostname = deviceHostnameById.get(job.device_id) ?? "";

      const haystack = [
        hostname,
        job.job_type,
        job.requested_by,
        job.error_message ?? "",
      ]
        .join(" ")
        .toLowerCase();

      return haystack.includes(query);
    });
  }, [jobs, search, statusFilter, deviceHostnameById]);

  return (
    <>
      <h1 className="page-title">Jobs</h1>
      <p className="page-subtitle">Worker task history.</p>

      {error && <div className="error-banner">Failed to load jobs: {error}</div>}

      <div className="filters-bar">
        <input
          type="text"
          className="search-input"
          placeholder="Search device, job type, or error..."
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
          <h2>Jobs</h2>
          <span className="count-tag">{filteredJobs.length}</span>
        </div>
        <div className="panel-body" style={{ maxHeight: "none" }}>
          {loading ? (
            <div className="empty-state">Loading jobs&hellip;</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Device</th>
                  <th>Job Type</th>
                  <th>Requested By</th>
                  <th>Started</th>
                  <th>Finished</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {filteredJobs.map((job) => (
                  <tr key={job.id}>
                    <td>
                      <StatusBadge status={job.status} />
                    </td>
                    <td>
                      {deviceHostnameById.get(job.device_id) ?? `Device #${job.device_id}`}
                    </td>
                    <td>{job.job_type}</td>
                    <td>{job.requested_by}</td>
                    <td className="mono">{formatTimestamp(job.started_at)}</td>
                    <td className="mono">{formatTimestamp(job.finished_at)}</td>
                    <td>
                      <ErrorMessageCell message={job.error_message} />
                    </td>
                  </tr>
                ))}
                {filteredJobs.length === 0 && (
                  <tr>
                    <td colSpan={7} className="empty-state">
                      {jobs.length === 0
                        ? "No jobs recorded yet."
                        : "No jobs match your search or filter."}
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
