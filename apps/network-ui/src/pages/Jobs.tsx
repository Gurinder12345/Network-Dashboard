import { useEffect, useMemo, useState } from "react";
import { getDevices, getJobs } from "../api/client";
import type { Device, Job } from "../api/types";
import { Banner, EmptyState, TableSkeleton } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { formatDuration, humanize, truncateId, truncateText } from "../utils/format";

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

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    jobs.forEach((job) => (counts[job.status] = (counts[job.status] ?? 0) + 1));
    return counts;
  }, [jobs]);

  return (
    <>
      <PageHeader title="Jobs" subtitle="Worker task history: backups, prechecks and applies." />

      {error && (
        <Banner tone="danger" title="Failed to load jobs.">
          {error}
        </Banner>
      )}

      <div className="filters-bar">
        <input
          type="search"
          className="search-input"
          placeholder="Search device, job type, or error…"
          aria-label="Search jobs"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <FilterChips
          label="Job status"
          value={statusFilter}
          onChange={setStatusFilter}
          options={[
            { value: ALL_STATUSES, label: "All", count: jobs.length },
            ...statuses.map((status) => ({
              value: status,
              label: humanize(status),
              count: statusCounts[status] ?? 0,
              status,
            })),
          ]}
        />
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Jobs</h2>
          <span className="count-tag">{filteredJobs.length}</span>
        </div>
        <div className="table-wrap">
          {loading ? (
            <TableSkeleton rows={8} columns={7} />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Device</th>
                  <th>Job Type</th>
                  <th>Requested By</th>
                  <th>Started</th>
                  <th>Duration</th>
                  <th>Job ID</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {filteredJobs.map((job) => (
                  <tr key={job.id} className={job.status === "failed" ? "row-alert" : undefined}>
                    <td>
                      <StatusBadge status={job.status} />
                    </td>
                    <td className="cell-primary">
                      {deviceHostnameById.get(job.device_id) ?? `Device #${job.device_id}`}
                    </td>
                    <td>
                      {humanize(job.job_type)}
                      <span className="cell-sub mono">{job.job_type}</span>
                    </td>
                    <td className="secondary">{job.requested_by}</td>
                    <td>
                      <RelativeTime value={job.started_at} />
                    </td>
                    <td className="duration">
                      {job.finished_at ? formatDuration(job.started_at, job.finished_at) : <span className="muted">running</span>}
                    </td>
                    <td className="mono muted" title={job.id}>
                      {truncateId(job.id)}
                    </td>
                    <td>
                      <ErrorMessageCell message={job.error_message} />
                    </td>
                  </tr>
                ))}
                {filteredJobs.length === 0 && (
                  <tr>
                    <td colSpan={8}>
                      <EmptyState
                        title={jobs.length === 0 ? "No jobs recorded yet." : "No jobs match these filters."}
                      />
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
