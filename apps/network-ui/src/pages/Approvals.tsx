import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyApproval,
  approveApproval,
  getApplyStatus,
  getApprovals,
  getDevices,
} from "../api/client";
import { OS6_PLATFORM } from "../api/constants";
import type { Approval, Device } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { formatTimestamp, truncateId } from "../utils/format";

const ALL_STATUSES = "all";
const APPROVER_STORAGE_KEY = "network-ui.approver";
const APPROVER_PATTERN = /^[A-Za-z0-9._@-]{2,64}$/;
const APPLY_POLL_INTERVAL_MS = 3000;
const APPLY_POLL_TIMEOUT_MS = 15 * 60 * 1000;
const MAX_POLL_ERRORS = 5;

interface ActiveApply {
  approvalId: string;
  requestId: string;
  hostname: string;
  startedAt: number;
  phase: "applying" | "applied" | "failed" | "unknown";
  error: string | null;
}

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

interface ApplyDialogProps {
  approval: Approval;
  hostname: string;
  submitting: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

function ApplyDialog({ approval, hostname, submitting, error, onConfirm, onCancel }: ApplyDialogProps) {
  const [typedHostname, setTypedHostname] = useState("");
  const confirmed = typedHostname.trim() === hostname;

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
        aria-labelledby="apply-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="panel-header">
          <h2 id="apply-title">Apply approved change</h2>
          <StatusBadge status={approval.status} />
        </div>

        <div className="modal-body">
          <div className="danger-banner">
            <strong>This WILL modify the running configuration of {hostname}.</strong>
            <span>
              The commands below are sent to the switch now, followed by a post-check. There is no
              automatic rollback. Do not apply changes to management or uplink interfaces.
            </span>
          </div>

          <dl className="detail-list">
            <dt>Device</dt>
            <dd>{hostname}</dd>
            <dt>Approval ID</dt>
            <dd className="mono">{approval.id}</dd>
            <dt>Approved by</dt>
            <dd>{approval.approved_by ?? "—"}</dd>
            <dt>Approved at</dt>
            <dd className="mono">
              {approval.approved_at ? new Date(approval.approved_at).toLocaleString() : "—"}
            </dd>
            <dt>Backup job</dt>
            <dd className="mono">{approval.backup_job_id ?? "—"}</dd>
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
            <span className="form-label">Type the hostname to confirm</span>
            <input
              type="text"
              className="search-input mono"
              style={{ maxWidth: "none" }}
              placeholder={hostname}
              value={typedHostname}
              disabled={submitting}
              autoFocus
              autoComplete="off"
              onChange={(event) => setTypedHostname(event.target.value)}
            />
          </label>

          {error && <div className="error-banner" style={{ marginBottom: 0 }}>{error}</div>}
        </div>

        <div className="modal-actions">
          <button type="button" className="copy-button" disabled={submitting} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="danger-button"
            disabled={!confirmed || submitting}
            onClick={onConfirm}
          >
            {submitting ? "Submitting…" : "Apply to switch"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface ApprovalActionProps {
  approval: Approval;
  platform: string | undefined;
  busy: boolean;
  onApprove: () => void;
  onApply: () => void;
}

function ApprovalAction({ approval, platform, busy, onApprove, onApply }: ApprovalActionProps) {
  if (platform !== undefined && platform !== OS6_PLATFORM) {
    return <span className="view-only-tag">View only</span>;
  }

  switch (approval.status) {
    case "pending":
      return (
        <button type="button" className="primary-button small-button" disabled={busy} onClick={onApprove}>
          Approve
        </button>
      );
    case "approved":
      return (
        <button type="button" className="danger-button small-button" disabled={busy} onClick={onApply}>
          Apply
        </button>
      );
    case "applying":
      return (
        <button type="button" className="primary-button small-button" disabled>
          Applying&hellip;
        </button>
      );
    default:
      return <span className="view-only-tag">—</span>;
  }
}

function ApplyStatusBanner({ apply, onDismiss }: { apply: ActiveApply; onDismiss: () => void }) {
  const tone =
    apply.phase === "applied" ? "applied" : apply.phase === "failed" ? "failed" : "applying";
  const label =
    apply.phase === "applied"
      ? "Applied"
      : apply.phase === "failed"
        ? "Failed"
        : apply.phase === "unknown"
          ? "Status unknown"
          : "Applying";

  return (
    <div className={`apply-banner apply-banner-${tone}`}>
      <div className="apply-banner-head">
        <span className={`badge badge-${tone}`}>
          <span className="badge-dot" />
          {label}
        </span>
        <span>{apply.hostname}</span>
        <span className="mono muted" title={apply.requestId}>
          req {truncateId(apply.requestId)}
        </span>
        {apply.phase === "applying" ? (
          <span className="muted">Applying and running post-check&hellip;</span>
        ) : (
          <button type="button" className="copy-button" onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
      {apply.phase === "applied" && (
        <div className="muted">Post-check confirmed the configuration is present on the device.</div>
      )}
      {apply.error && (
        <details className="error-details" style={{ maxWidth: "none" }} open={apply.error.length < 300}>
          <summary className="error-summary mono">{apply.error.split("\n")[0]}</summary>
          <pre className="error-full">{apply.error}</pre>
        </details>
      )}
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
  const [applyTarget, setApplyTarget] = useState<Approval | null>(null);
  const [applySubmitting, setApplySubmitting] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [activeApply, setActiveApply] = useState<ActiveApply | null>(null);
  const pollTimer = useRef<number | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    };
  }, []);

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

  const closeApplyDialog = useCallback(() => {
    setApplyTarget(null);
    setApplyError(null);
  }, []);

  const updateApply = useCallback((patch: Partial<ActiveApply>) => {
    if (!mounted.current) return;
    setActiveApply((current) => (current ? { ...current, ...patch } : current));
  }, []);

  function pollApply(approvalId: string, requestId: string, startedAt: number, errors: number) {
    pollTimer.current = window.setTimeout(async () => {
      if (!mounted.current) return;

      if (Date.now() - startedAt > APPLY_POLL_TIMEOUT_MS) {
        updateApply({
          phase: "unknown",
          error: "Still applying after 15 minutes. Check Jobs and Audit before taking any action.",
        });
        await load();
        return;
      }

      try {
        const status = await getApplyStatus(approvalId, requestId);

        // PostgreSQL approval status is the source of truth for the outcome.
        if (status.approval_status === "applied") {
          updateApply({ phase: "applied", error: null });
          await load();
        } else if (status.approval_status === "failed") {
          updateApply({ phase: "failed", error: status.error ?? "Apply failed. See Audit for details." });
          await load();
        } else if (status.task_state === "failed") {
          updateApply({
            phase: "unknown",
            error: `Worker task failed but approval is still ${status.approval_status}: ${status.error ?? "no detail"}`,
          });
          await load();
        } else {
          pollApply(approvalId, requestId, startedAt, 0);
        }
      } catch (err) {
        if (errors + 1 >= MAX_POLL_ERRORS) {
          updateApply({
            phase: "unknown",
            error: `Lost contact with the API while applying: ${
              err instanceof Error ? err.message : "unknown error"
            }. Check Jobs and Audit.`,
          });
        } else {
          pollApply(approvalId, requestId, startedAt, errors + 1);
        }
      }
    }, APPLY_POLL_INTERVAL_MS);
  }

  async function handleApply() {
    if (!applyTarget || applySubmitting) return;

    setApplySubmitting(true);
    setApplyError(null);

    try {
      const submitted = await applyApproval(applyTarget.id);
      const startedAt = Date.now();

      setApplyTarget(null);
      setNotice(null);
      setActiveApply({
        approvalId: submitted.approval_id,
        requestId: submitted.request_id,
        hostname: submitted.hostname,
        startedAt,
        phase: "applying",
        error: null,
      });
      await load();
      pollApply(submitted.approval_id, submitted.request_id, startedAt, 0);
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : "Apply request failed");
      await load();
    } finally {
      setApplySubmitting(false);
    }
  }

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

  const devicePlatformById = useMemo(() => {
    const map = new Map<number, string>();
    devices.forEach((device) => map.set(device.id, device.platform));
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
      {activeApply && (
        <ApplyStatusBanner apply={activeApply} onDismiss={() => setActiveApply(null)} />
      )}
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
                      <ApprovalAction
                        approval={approval}
                        platform={devicePlatformById.get(approval.device_id)}
                        busy={submitting || applySubmitting || activeApply?.phase === "applying"}
                        onApprove={() => {
                          setNotice(null);
                          setActionError(null);
                          setConfirmTarget(approval);
                        }}
                        onApply={() => {
                          setApplyError(null);
                          setApplyTarget(approval);
                        }}
                      />
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

      {applyTarget && (
        <ApplyDialog
          approval={applyTarget}
          hostname={deviceHostnameById.get(applyTarget.device_id) ?? `Device #${applyTarget.device_id}`}
          submitting={applySubmitting}
          error={applyError}
          onConfirm={handleApply}
          onCancel={closeApplyDialog}
        />
      )}

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
