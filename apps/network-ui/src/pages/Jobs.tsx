import { useEffect, useMemo, useState } from "react";
import { ApiRequestError, deleteJob, getDevices, getJobs } from "../api/client";
import type { Device, Job } from "../api/types";
import { Banner, EmptyState, TableSkeleton } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { formatDuration, humanize, truncateId, truncateText } from "../utils/format";

const ALL_STATUSES = "all";
const ERROR_PREVIEW_LENGTH = 80;
// Finished states the API allows deleting. The server re-checks the current status, so
// this only decides where the Delete action is shown.
const DELETABLE = new Set(["success", "failed", "completed", "cancelled"]);

function deleteErrorMessage(err: unknown): string {
  if (err instanceof ApiRequestError && err.status === 409) return "This job is still active and cannot be deleted.";
  if (err instanceof ApiRequestError && err.status === 404) return "Job no longer exists.";
  return "Unable to delete job.";
}

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
  const [confirmJob, setConfirmJob] = useState<Job | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; title: string; detail?: string } | null>(null);

  async function confirmDelete() {
    if (!confirmJob) return;
    const job = confirmJob;
    setDeleting(true);
    try {
      await deleteJob(job.id);
      // Remove only after the API confirmed it; filters and search stay as they are.
      setJobs((current) => current.filter((j) => j.id !== job.id));
      setNotice({ tone: "success", title: "Job deleted" });
    } catch (err) {
      setNotice({
        tone: "danger",
        title: deleteErrorMessage(err),
        detail: err instanceof ApiRequestError && err.status === 409 ? err.message : undefined,
      });
    } finally {
      setDeleting(false);
      setConfirmJob(null);
    }
  }

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

  // Keep the selected status chip even if its last job was deleted.
  const statuses = useMemo(() => {
    const set = new Set(jobs.map((job) => job.status));
    if (statusFilter !== ALL_STATUSES) set.add(statusFilter);
    return Array.from(set).sort();
  }, [jobs, statusFilter]);

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

      {notice && (
        <Banner tone={notice.tone} title={notice.title} onDismiss={() => setNotice(null)}>
          {notice.detail}
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
            <TableSkeleton rows={8} columns={9} />
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
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
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
                      <span className="cell-sub mono">
                        {job.job_type}
                        {job.backup_id ? ` · backup #${job.backup_id}` : ""}
                      </span>
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
                    <td>
                      {DELETABLE.has(job.status) && (
                        <button
                          type="button"
                          className="secondary-button small-button destructive"
                          disabled={deleting && confirmJob?.id === job.id}
                          onClick={() => setConfirmJob(job)}
                          aria-label={`Delete job ${truncateId(job.id)}`}
                        >
                          Delete
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {filteredJobs.length === 0 && (
                  <tr>
                    <td colSpan={9}>
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
      {confirmJob && (
        <Modal
          kicker="Delete job"
          title={`Delete job #${truncateId(confirmJob.id)}?`}
          busy={deleting}
          onClose={() => setConfirmJob(null)}
          actions={
            <>
              <button type="button" className="secondary-button" disabled={deleting} onClick={() => setConfirmJob(null)}>
                Cancel
              </button>
              <button type="button" className="danger-button" disabled={deleting} onClick={confirmDelete}>
                {deleting ? "Deleting…" : "Delete"}
              </button>
            </>
          }
        >
          <p>This removes the job record from the Jobs page.</p>
          <p>Related backups, approvals and audit history will be preserved.</p>
          <p className="muted">
            {humanize(confirmJob.job_type)} · {deviceHostnameById.get(confirmJob.device_id) ?? `Device #${confirmJob.device_id}`} ·{" "}
            {confirmJob.status}
          </p>
        </Modal>
      )}
    </>
  );
}
