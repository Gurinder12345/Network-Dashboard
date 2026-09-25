import { useCallback, useEffect, useMemo, useState } from "react";
import { approveApproval, getApprovals, getDevices } from "../api/client";
import type { Approval, Device } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { formatTimestamp, truncateId } from "../utils/format";

const ALL_STATUSES = "all";
const APPROVER_STORAGE_KEY = "network-ui.approver";
const APPROVER_PATTERN = /^[A-Za-z0-9._@-]{2,64}$/;

function readStoredApprover(): string {
  try {
    return window.localStorage.getItem(APPROVER_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function storeApprover(value: string) {
  try {
    window.localStorage.setItem(APPROVER_STORAGE_KEY, value);
  } catch {
    // Storage unavailable (private mode); the name just won't be remembered.
  }
}

interface ApproveDialogProps {
  approval: Approval;
  hostname: string;
  submitting: boolean;
  error: string | null;
  onConfirm: (approvedBy: string) => void;
  onCancel: () => void;
}

function ApproveDialog({ approval, hostname, submitting, error, onConfirm, onCancel }: ApproveDialogProps) {
  const [approver, setApprover] = useState(readStoredApprover);
  const trimmed = approver.trim();
  const approverValid = APPROVER_PATTERN.test(trimmed);
  const sameAsRequester = trimmed.toLowerCase() === approval.requested_by.toLowerCase();
  const canConfirm = approverValid && !sameAsRequester && !submitting;

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !submitting) onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel, submitting]);

  return (
    <div className="modal-backdrop" onClick={() => !submitting && onCancel()}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approve-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="panel-header">
          <h2 id="approve-title">Approve change</h2>
          <StatusBadge status={approval.status} />
        </div>

        <div className="modal-body">
          <dl className="detail-list">
            <dt>Device</dt>
            <dd>{hostname}</dd>
            <dt>Requested by</dt>
            <dd>{approval.requested_by}</dd>
            <dt>Backup job</dt>
            <dd className="mono">{approval.backup_job_id ?? "—"}</dd>
            <dt>Approval ID</dt>
            <dd className="mono">{approval.id}</dd>
          </dl>

          <div className="form-field">
            <span className="form-label">Parent / context</span>
            <pre className="config-preview">
              {(approval.config_parents ?? []).length > 0
                ? (approval.config_parents ?? []).join("\n")
                : "(global configuration)"}
            </pre>
          </div>

          <div className="form-field">
            <span className="form-label">Configuration lines</span>
            <pre className="config-preview">{(approval.config_lines ?? []).join("\n")}</pre>
          </div>

          <label className="form-field">
            <span className="form-label">Approver</span>
            <input
              type="text"
              className="search-input mono"
              style={{ maxWidth: "none" }}
              placeholder="your name"
              value={approver}
              disabled={submitting}
              autoFocus
              onChange={(event) => setApprover(event.target.value)}
            />
            {trimmed.length > 0 && !approverValid && (
              <span className="form-error">2–64 characters: letters, digits, . _ @ -</span>
            )}
            {approverValid && sameAsRequester && (
              <span className="form-error">Approver must be different from the requester.</span>
            )}
          </label>

          <div className="precheck-note">
            Approving records your decision only. Configuration is not applied from this page.
          </div>

          {error && <div className="error-banner" style={{ marginBottom: 0 }}>{error}</div>}
        </div>

        <div className="modal-actions">
          <button type="button" className="copy-button" disabled={submitting} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={!canConfirm}
            onClick={() => {
              storeApprover(trimmed);
              onConfirm(trimmed);
            }}
          >
            {submitting ? "Approving…" : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}

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
  const [confirmTarget, setConfirmTarget] = useState<Approval | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (isCancelled: () => boolean = () => false) => {
    try {
      const [approvalsData, devicesData] = await Promise.all([
        getApprovals(),
        getDevices(),
      ]);

      if (isCancelled()) return;

      setApprovals(approvalsData);
      setDevices(devicesData);
      setError(null);
    } catch (err) {
      if (isCancelled()) return;
      setError(err instanceof Error ? err.message : "Failed to load approvals");
    } finally {
      if (!isCancelled()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    load(() => cancelled);

    return () => {
      cancelled = true;
    };
  }, [load]);

  const closeDialog = useCallback(() => {
    setConfirmTarget(null);
    setActionError(null);
  }, []);

  async function handleApprove(approvedBy: string) {
    if (!confirmTarget || submitting) return;

    setSubmitting(true);
    setActionError(null);

    try {
      const updated = await approveApproval(confirmTarget.id, approvedBy);
      const hostname = deviceHostnameById.get(updated.device_id) ?? `Device #${updated.device_id}`;
      setConfirmTarget(null);
      setNotice(`Approved change for ${hostname} as ${updated.approved_by}. It has not been applied.`);
      await load();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Approval failed");
      // The status may have changed underneath us (e.g. already approved); refresh the table.
      await load();
    } finally {
      setSubmitting(false);
    }
  }

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
      {notice && (
        <div className="notice-banner">
          <span>{notice}</span>
          <button type="button" className="copy-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

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
                  <th>Actions</th>
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
                    <td>
                      {approval.status === "pending" ? (
                        <button
                          type="button"
                          className="primary-button small-button"
                          disabled={submitting}
                          onClick={() => {
                            setNotice(null);
                            setActionError(null);
                            setConfirmTarget(approval);
                          }}
                        >
                          Approve
                        </button>
                      ) : (
                        <span className="view-only-tag">—</span>
                      )}
                    </td>
                  </tr>
                ))}
                {filteredApprovals.length === 0 && (
                  <tr>
                    <td colSpan={9} className="empty-state">
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

      {confirmTarget && (
        <ApproveDialog
          approval={confirmTarget}
          hostname={
            deviceHostnameById.get(confirmTarget.device_id) ?? `Device #${confirmTarget.device_id}`
          }
          submitting={submitting}
          error={actionError}
          onConfirm={handleApprove}
          onCancel={closeDialog}
        />
      )}
    </>
  );
}
