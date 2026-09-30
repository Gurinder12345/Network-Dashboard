import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyApproval,
  approveApproval,
  cancelApproval,
  getApplyStatus,
  getApprovals,
  getDevices,
} from "../api/client";
import { OS6_PLATFORM } from "../api/constants";
import type { Approval, Device } from "../api/types";
import { ConfigView } from "../components/ConfigView";
import { Banner, EmptyState, TableSkeleton } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { formatTimestamp, truncateId } from "../utils/format";

const ALL_STATUSES = "all";
// Change-control lifecycle order, used for the status filter.
const STATUS_ORDER = ["pending", "approved", "applying", "applied", "failed", "cancelled"];
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

/** Device emphasized at the top of every change-control dialog. */
function TargetCard({ hostname, approval }: { hostname: string; approval: Approval }) {
  return (
    <div className="target-card">
      <div>
        <div className="target-host">{hostname}</div>
        <div className="target-sub">
          Requested by {approval.requested_by}
          {approval.created_at ? ` · ${formatTimestamp(approval.created_at)}` : ""}
        </div>
      </div>
      <StatusBadge status={approval.status} />
    </div>
  );
}

/** Reference IDs, visually secondary to the device and commands. */
function ReferenceIds({ approval, extra }: { approval: Approval; extra?: [string, string][] }) {
  return (
    <dl className="detail-list secondary">
      {(extra ?? []).map(([label, value]) => (
        <FragmentPair key={label} label={label} value={value} />
      ))}
      <FragmentPair label="Approval ID" value={approval.id} mono />
      <FragmentPair label="Backup job" value={approval.backup_job_id ?? "—"} mono />
    </dl>
  );
}

function FragmentPair({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <>
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined}>{value}</dd>
    </>
  );
}

function NameField({
  label,
  value,
  onChange,
  disabled,
  error,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  error: string | null;
}) {
  return (
    <label className="form-field">
      <span className="form-label">{label}</span>
      <input
        type="text"
        className="search-input mono"
        style={{ maxWidth: "none" }}
        placeholder="your name"
        value={value}
        disabled={disabled}
        autoFocus
        autoComplete="off"
        onChange={(event) => onChange(event.target.value)}
      />
      {error && <span className="form-error">{error}</span>}
    </label>
  );
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
  const [reviewed, setReviewed] = useState(false);
  const trimmed = approver.trim();
  const approverValid = APPROVER_PATTERN.test(trimmed);
  const sameAsRequester = trimmed.toLowerCase() === approval.requested_by.toLowerCase();
  const canConfirm = approverValid && !sameAsRequester && reviewed && !submitting;

  const nameError =
    trimmed.length > 0 && !approverValid
      ? "2–64 characters: letters, digits, . _ @ -"
      : approverValid && sameAsRequester
        ? "Approver must be different from the requester."
        : null;

  return (
    <Modal
      kicker="Change control"
      title="Approve change"
      busy={submitting}
      onClose={onCancel}
      actions={
        <>
          <button type="button" className="secondary-button" disabled={submitting} onClick={onCancel}>
            Back
          </button>
          <button
            type="button"
            className="success-button"
            disabled={!canConfirm}
            onClick={() => {
              storeApprover(trimmed);
              onConfirm(trimmed);
            }}
          >
            {submitting ? "Approving…" : "Approve change"}
          </button>
        </>
      }
    >
      <TargetCard hostname={hostname} approval={approval} />

      <Banner tone="info" title="Approval only.">
        Approving records your decision. Nothing is sent to the switch until someone applies it.
      </Banner>

      <ConfigView parents={approval.config_parents} lines={approval.config_lines} />

      <ReferenceIds approval={approval} />

      <NameField label="Approver" value={approver} onChange={setApprover} disabled={submitting} error={nameError} />

      <label className="form-field form-field-inline confirm-box">
        <input
          type="checkbox"
          checked={reviewed}
          disabled={submitting}
          onChange={(event) => setReviewed(event.target.checked)}
        />
        <span>I have reviewed the target device and every command above.</span>
      </label>

      {error && (
        <Banner tone="danger" title="Approval failed.">
          {error}
        </Banner>
      )}
    </Modal>
  );
}

function ConfigDetail({ approval }: { approval: Approval }) {
  const parents = approval.config_parents ?? [];
  const lines = approval.config_lines ?? [];

  if (parents.length === 0 && lines.length === 0) {
    return <span className="muted">—</span>;
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

  return (
    <Modal
      kicker="High-risk action"
      title="Apply change to switch"
      busy={submitting}
      onClose={onCancel}
      actions={
        <>
          <button type="button" className="secondary-button" disabled={submitting} onClick={onCancel}>
            Back
          </button>
          <button type="button" className="danger-button" disabled={!confirmed || submitting} onClick={onConfirm}>
            {submitting ? "Submitting…" : "Apply to switch"}
          </button>
        </>
      }
    >
      <div className="danger-banner" role="alert">
        <strong>This WILL modify the running configuration of {hostname}.</strong>
        <span>
          The commands below are sent to the switch now, followed by a post-check. There is no
          automatic rollback. Do not apply changes to management or uplink interfaces.
        </span>
      </div>

      <TargetCard hostname={hostname} approval={approval} />

      <ConfigView parents={approval.config_parents} lines={approval.config_lines} />

      <ReferenceIds
        approval={approval}
        extra={[
          ["Approved by", approval.approved_by ?? "—"],
          ["Approved at", approval.approved_at ? new Date(approval.approved_at).toLocaleString() : "—"],
        ]}
      />

      <label className="form-field confirm-box">
        <span className="form-label">
          Type <span className="mono">{hostname}</span> to confirm
        </span>
        <input
          type="text"
          className="search-input mono"
          style={{ maxWidth: "none" }}
          placeholder={hostname}
          value={typedHostname}
          disabled={submitting}
          autoFocus
          autoComplete="off"
          aria-invalid={typedHostname.length > 0 && !confirmed}
          onChange={(event) => setTypedHostname(event.target.value)}
        />
      </label>

      {error && (
        <Banner tone="danger" title="Apply request failed.">
          {error}
        </Banner>
      )}
    </Modal>
  );
}

interface CancelDialogProps {
  approval: Approval;
  hostname: string;
  submitting: boolean;
  error: string | null;
  onConfirm: (cancelledBy: string, reason: string | null) => void;
  onCancel: () => void;
}

function CancelDialog({ approval, hostname, submitting, error, onConfirm, onCancel }: CancelDialogProps) {
  const [cancelledBy, setCancelledBy] = useState(readStoredApprover);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const trimmed = cancelledBy.trim();
  const nameValid = APPROVER_PATTERN.test(trimmed);
  const reasonTooLong = reason.trim().length > 500;
  const canConfirm = nameValid && !reasonTooLong && confirmed && !submitting;

  return (
    <Modal
      kicker="Change control"
      title="Cancel change"
      busy={submitting}
      onClose={onCancel}
      actions={
        <>
          <button type="button" className="secondary-button" disabled={submitting} onClick={onCancel}>
            Keep change
          </button>
          <button
            type="button"
            className="danger-button"
            disabled={!canConfirm}
            onClick={() => {
              storeApprover(trimmed);
              onConfirm(trimmed, reason.trim() || null);
            }}
          >
            {submitting ? "Cancelling…" : "Cancel change"}
          </button>
        </>
      }
    >
      <TargetCard hostname={hostname} approval={approval} />

      <Banner tone="warning" title="Cancellation is permanent.">
        The change can never be approved or applied afterwards. Nothing is sent to the switch,
        and the record is kept for audit.
      </Banner>

      <ConfigView parents={approval.config_parents} lines={approval.config_lines} />

      <ReferenceIds
        approval={approval}
        extra={[
          ["Current status", approval.status],
          ...(approval.approved_by ? ([["Approved by", approval.approved_by]] as [string, string][]) : []),
        ]}
      />

      <NameField
        label="Cancelled by"
        value={cancelledBy}
        onChange={setCancelledBy}
        disabled={submitting}
        error={trimmed.length > 0 && !nameValid ? "2–64 characters: letters, digits, . _ @ -" : null}
      />

      <label className="form-field">
        <span className="form-label">Reason (optional)</span>
        <textarea
          className="config-input"
          rows={2}
          maxLength={500}
          value={reason}
          disabled={submitting}
          placeholder="e.g. Superseded by a new change request"
          onChange={(event) => setReason(event.target.value)}
        />
        {reasonTooLong && <span className="form-error">At most 500 characters.</span>}
      </label>

      <label className="form-field form-field-inline confirm-box">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={submitting}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>Cancel this change permanently. It can never be approved or applied.</span>
      </label>

      {error && (
        <Banner tone="danger" title="Cancel failed.">
          {error}
        </Banner>
      )}
    </Modal>
  );
}

interface ApprovalActionProps {
  approval: Approval;
  platform: string | undefined;
  busy: boolean;
  onApprove: () => void;
  onApply: () => void;
  onCancelChange: () => void;
}

function ApprovalAction({ approval, platform, busy, onApprove, onApply, onCancelChange }: ApprovalActionProps) {
  if (platform !== undefined && platform !== OS6_PLATFORM) {
    return <span className="view-only-tag">View only</span>;
  }

  const cancelButton = (
    <button type="button" className="secondary-button destructive small-button" disabled={busy} onClick={onCancelChange}>
      Cancel
    </button>
  );

  switch (approval.status) {
    case "pending":
      return (
        <div className="action-group">
          <button type="button" className="success-button small-button" disabled={busy} onClick={onApprove}>
            Approve
          </button>
          {cancelButton}
        </div>
      );
    case "approved":
      return (
        <div className="action-group">
          <button type="button" className="danger-button small-button" disabled={busy} onClick={onApply}>
            Apply
          </button>
          {cancelButton}
        </div>
      );
    case "applying":
      return <StatusBadge status="applying" label="Applying…" />;
    default:
      return <span className="muted">—</span>;
  }
}

function ApplyStatusBanner({ apply, onDismiss }: { apply: ActiveApply; onDismiss: () => void }) {
  const status =
    apply.phase === "applied" ? "applied" : apply.phase === "failed" ? "failed" : apply.phase === "unknown" ? "unknown" : "applying";
  const label =
    apply.phase === "applied"
      ? "Applied"
      : apply.phase === "failed"
        ? "Failed"
        : apply.phase === "unknown"
          ? "Status unknown"
          : "Applying";

  return (
    <div className={`apply-banner apply-banner-${status}`} role="status">
      <div className="apply-banner-head">
        <StatusBadge status={status} label={label} />
        <strong>{apply.hostname}</strong>
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
  const [cancelTarget, setCancelTarget] = useState<Approval | null>(null);
  const [cancelSubmitting, setCancelSubmitting] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
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

  const closeCancelDialog = useCallback(() => {
    setCancelTarget(null);
    setCancelError(null);
  }, []);

  async function handleCancelChange(cancelledBy: string, reason: string | null) {
    if (!cancelTarget || cancelSubmitting) return;

    setCancelSubmitting(true);
    setCancelError(null);

    try {
      const updated = await cancelApproval(cancelTarget.id, cancelledBy, reason);
      const hostname = deviceHostnameById.get(updated.device_id) ?? `Device #${updated.device_id}`;
      setCancelTarget(null);
      setNotice(`Cancelled change for ${hostname} as ${updated.cancelled_by}. Nothing was sent to the switch.`);
      await load();
    } catch (err) {
      setCancelError(err instanceof Error ? err.message : "Cancel failed");
      // The status may have moved on (e.g. Apply started); show the current state.
      await load();
    } finally {
      setCancelSubmitting(false);
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

  // Lifecycle order first, then any unexpected status values.
  const statuses = useMemo(() => {
    const present = new Set(approvals.map((approval) => approval.status));
    return [
      ...STATUS_ORDER.filter((status) => present.has(status)),
      ...Array.from(present).filter((status) => !STATUS_ORDER.includes(status)).sort(),
    ];
  }, [approvals]);

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    approvals.forEach((approval) => (counts[approval.status] = (counts[approval.status] ?? 0) + 1));
    return counts;
  }, [approvals]);

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
      <PageHeader
        title="Approvals"
        subtitle="Change control: review, approve, apply or cancel OS6 changes."
      />

      {error && (
        <Banner tone="danger" title="Failed to load approvals.">
          {error}
        </Banner>
      )}
      {activeApply && (
        <ApplyStatusBanner apply={activeApply} onDismiss={() => setActiveApply(null)} />
      )}
      {notice && (
        <Banner tone="success" onDismiss={() => setNotice(null)}>
          {notice}
        </Banner>
      )}

      <div className="filters-bar">
        <input
          type="search"
          className="search-input"
          placeholder="Search device, requester, or config…"
          aria-label="Search approvals"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <FilterChips
          label="Approval status"
          value={statusFilter}
          onChange={setStatusFilter}
          options={[
            { value: ALL_STATUSES, label: "All", count: approvals.length },
            ...statuses.map((status) => ({
              value: status,
              label: status.charAt(0).toUpperCase() + status.slice(1),
              count: statusCounts[status] ?? 0,
              status,
            })),
          ]}
        />
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Approvals</h2>
          <span className="count-tag">{filteredApprovals.length}</span>
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
                  <th>Change</th>
                  <th>Requested</th>
                  <th>Decision</th>
                  <th>Backup Job</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredApprovals.map((approval) => (
                  <tr
                    key={approval.id}
                    className={
                      approval.status === "failed"
                        ? "row-alert"
                        : approval.status === "pending" || approval.status === "approved"
                          ? "row-warn"
                          : undefined
                    }
                  >
                    <td
                      title={
                        approval.status === "cancelled"
                          ? `Cancelled by ${approval.cancelled_by ?? "unknown"} at ${formatTimestamp(approval.cancelled_at)}` +
                            (approval.cancellation_reason ? `\nReason: ${approval.cancellation_reason}` : "")
                          : undefined
                      }
                    >
                      <StatusBadge status={approval.status} />
                    </td>
                    <td className="cell-primary">
                      {deviceHostnameById.get(approval.device_id) ?? `Device #${approval.device_id}`}
                      <span className="cell-sub mono" title={approval.id}>
                        {truncateId(approval.id)}
                      </span>
                    </td>
                    <td>
                      <ConfigDetail approval={approval} />
                    </td>
                    <td>
                      {approval.requested_by}
                      <span className="cell-sub">
                        <RelativeTime value={approval.created_at} />
                      </span>
                    </td>
                    <td>
                      {approval.status === "cancelled" ? (
                        <>
                          Cancelled by {approval.cancelled_by ?? "—"}
                          <span className="cell-sub">
                            <RelativeTime value={approval.cancelled_at} />
                          </span>
                        </>
                      ) : approval.approved_by ? (
                        <>
                          {approval.approved_by}
                          <span className="cell-sub">
                            <RelativeTime value={approval.approved_at} />
                          </span>
                        </>
                      ) : (
                        <span className="muted">Awaiting approval</span>
                      )}
                    </td>
                    <td className="mono muted" title={approval.backup_job_id ?? undefined}>
                      {truncateId(approval.backup_job_id)}
                    </td>
                    <td>
                      <ApprovalAction
                        approval={approval}
                        platform={devicePlatformById.get(approval.device_id)}
                        busy={
                          submitting ||
                          applySubmitting ||
                          cancelSubmitting ||
                          activeApply?.phase === "applying"
                        }
                        onApprove={() => {
                          setNotice(null);
                          setActionError(null);
                          setConfirmTarget(approval);
                        }}
                        onApply={() => {
                          setApplyError(null);
                          setApplyTarget(approval);
                        }}
                        onCancelChange={() => {
                          setNotice(null);
                          setCancelError(null);
                          setCancelTarget(approval);
                        }}
                      />
                    </td>
                  </tr>
                ))}
                {filteredApprovals.length === 0 && (
                  <tr>
                    <td colSpan={7}>
                      <EmptyState
                        title={approvals.length === 0 ? "No approvals recorded yet." : "No approvals match these filters."}
                        hint={approvals.length === 0 ? "Prechecks that detect a change create approvals here." : undefined}
                      />
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

      {cancelTarget && (
        <CancelDialog
          approval={cancelTarget}
          hostname={deviceHostnameById.get(cancelTarget.device_id) ?? `Device #${cancelTarget.device_id}`}
          submitting={cancelSubmitting}
          error={cancelError}
          onConfirm={handleCancelChange}
          onCancel={closeCancelDialog}
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
