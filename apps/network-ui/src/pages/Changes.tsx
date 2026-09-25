import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { getDevices, getOs6Precheck, submitOs6Precheck } from "../api/client";
import { OS6_PLATFORM } from "../api/constants";
import type { Device, Os6PrecheckStatus } from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { truncateId, truncateText } from "../utils/format";

type FindingLevel = "error" | "warning" | "info";

interface Finding {
  level: FindingLevel;
  text: string;
}

type RunPhase = "blocked" | "submitting" | "polling" | "completed" | "failed";

interface PrecheckRun {
  device: Device;
  configParents: string[];
  configLines: string[];
  findings: Finding[];
  startedAt: number;
  phase: RunPhase;
  requestId: string | null;
  status: Os6PrecheckStatus | null;
  error: string | null;
}

const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000;
const MAX_POLL_ERRORS = 3;

// Mirrors the API's checks so obvious problems are caught before submission.
// The playbook enters/exits configuration mode itself; "do" would run exec-mode commands.
const MODE_COMMANDS = /^(configure|config|conf\s+t|end|exit|do)(\s|$)/i;
// Commands that can drop links, wipe config, or cut management access.
const RISKY_COMMANDS = /^(shutdown|no\s+|reload|write erase|erase|delete|clear config|copy\s)/i;
const PRINTABLE_LINE = /^[\x20-\x7e]+$/;
const MAX_PARENTS = 5;
const MAX_LINES = 50;

function splitLines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

function isActionable(device: Device): boolean {
  return device.platform === OS6_PLATFORM && device.enabled;
}

function runLocalValidation(device: Device, configParents: string[], configLines: string[]): Finding[] {
  const findings: Finding[] = [];

  if (!isActionable(device)) {
    findings.push({
      level: "error",
      text: `${device.hostname} (${device.platform}) is view-only. Only enabled OS6 devices accept changes.`,
    });
  }

  if (configLines.length === 0) {
    findings.push({ level: "error", text: "At least one configuration line is required." });
  }
  if (configLines.length > MAX_LINES) {
    findings.push({ level: "error", text: `At most ${MAX_LINES} configuration lines are allowed.` });
  }
  if (configParents.length > MAX_PARENTS) {
    findings.push({ level: "error", text: `At most ${MAX_PARENTS} parent contexts are allowed.` });
  }

  [...configParents, ...configLines].forEach((line) => {
    if (MODE_COMMANDS.test(line)) {
      findings.push({
        level: "error",
        text: `"${line}" — configuration mode is handled by the platform; remove this line.`,
      });
    } else if (!PRINTABLE_LINE.test(line)) {
      findings.push({ level: "error", text: `"${line}" contains unsupported characters.` });
    }
  });

  configLines.forEach((line) => {
    if (RISKY_COMMANDS.test(line)) {
      findings.push({
        level: "warning",
        text: `"${line}" is disruptive. Confirm the target is not a management or uplink interface.`,
      });
    }
  });

  if (configParents.length === 0 && configLines.length > 0) {
    findings.push({ level: "info", text: "No parent context — lines apply at global configuration level." });
  }

  const duplicates = configLines.filter((line, index) => configLines.indexOf(line) !== index);
  if (duplicates.length > 0) {
    findings.push({ level: "warning", text: `Duplicate lines: ${Array.from(new Set(duplicates)).join(", ")}` });
  }

  return findings;
}

function runSummary(run: PrecheckRun): { label: string; tone: string } {
  switch (run.phase) {
    case "blocked":
      return { label: "Blocked", tone: "failed" };
    case "submitting":
      return { label: "Submitting", tone: "applying" };
    case "polling":
      return { label: run.status?.state === "running" ? "Running" : "Queued", tone: "applying" };
    case "failed":
      return { label: "Failed", tone: "failed" };
    case "completed":
      return run.status?.result?.status === "no_change_required"
        ? { label: "No change required", tone: "applied" }
        : { label: "Approval pending", tone: "pending" };
  }
}

function FindingList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return <div className="finding finding-ok">No issues found in the submitted lines.</div>;
  }

  return (
    <ul className="finding-list">
      {findings.map((finding, index) => (
        <li key={index} className={`finding finding-${finding.level}`}>
          <span className="finding-level">{finding.level}</span>
          {finding.text}
        </li>
      ))}
    </ul>
  );
}

function DevicePrecheck({ run, elapsed }: { run: PrecheckRun; elapsed: number }) {
  if (run.phase === "blocked") {
    return <div className="precheck-note">Not submitted — fix the errors above first.</div>;
  }

  if (run.phase === "submitting" || run.phase === "polling") {
    return (
      <div className="finding finding-info">
        <span className="finding-level">{run.phase === "submitting" ? "submitting" : run.status?.state ?? "queued"}</span>
        Reading running-config and verifying commands on {run.device.hostname}&hellip; {elapsed}s
      </div>
    );
  }

  if (run.phase === "failed") {
    const message = run.error ?? "Precheck failed";
    return (
      <details className="error-details" open={message.length < 200} style={{ maxWidth: "none" }}>
        <summary className="error-summary mono">{truncateText(message, 120)}</summary>
        <pre className="error-full">{message}</pre>
      </details>
    );
  }

  const result = run.status?.result;
  if (!result) return <div className="precheck-note">The worker returned no result.</div>;

  return (
    <>
      <table className="data-table">
        <thead>
          <tr>
            <th>Command</th>
            <th>Check</th>
            <th>Device state</th>
          </tr>
        </thead>
        <tbody>
          {result.dry_run.command_results.map((command, index) => (
            <tr key={index}>
              <td className="mono">{command.command}</td>
              <td className="mono muted">{command.verification_method}</td>
              <td>
                {command.desired_state_present ? (
                  <span className="badge badge-unknown">
                    <span className="badge-dot" />
                    Already present
                  </span>
                ) : (
                  <span className="badge badge-pending">
                    <span className="badge-dot" />
                    Will change
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {result.backup && (
        <div className="precheck-kv">
          <span className="form-label">Pre-change backup</span>
          <span className="mono" title={result.backup.job_id ?? undefined}>
            job {truncateId(result.backup.job_id)}
          </span>
          <span className="mono muted" title={result.backup.checksum ?? undefined}>
            {truncateId(result.backup.checksum, 12)}
          </span>
        </div>
      )}

      {result.approval?.approval_id && (
        <div className="finding finding-warning">
          <span className="finding-level">approval</span>
          Pending approval <span className="mono">{truncateId(result.approval.approval_id)}</span> was created.
          Review it on the <Link to="/approvals">Approvals</Link> page. Nothing has been applied.
        </div>
      )}

      {result.status === "no_change_required" && (
        <div className="finding finding-ok">
          All commands are already present on the device. No backup or approval was created.
        </div>
      )}
    </>
  );
}

function PrecheckResults({ run }: { run: PrecheckRun | null }) {
  const [now, setNow] = useState(Date.now());
  const active = run?.phase === "submitting" || run?.phase === "polling";

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);

  if (!run) {
    return (
      <div className="empty-state">
        Select an OS6 device, enter configuration, and run a precheck.
      </div>
    );
  }

  const summary = runSummary(run);
  const elapsed = Math.max(0, Math.round((now - run.startedAt) / 1000));

  return (
    <div className="precheck-results">
      <div className="precheck-summary">
        <span className={`badge badge-${summary.tone}`}>
          <span className="badge-dot" />
          {summary.label}
        </span>
        <span>{run.device.hostname}</span>
        <span className="mono muted">{run.device.management_ip}</span>
        {run.requestId && (
          <span className="precheck-request-id">
            <span className="mono muted" title={run.requestId}>
              req {truncateId(run.requestId)}
            </span>
            <CopyButton value={run.requestId} />
          </span>
        )}
      </div>

      <div className="precheck-section-label">Local validation</div>
      <FindingList findings={run.findings} />

      <div className="precheck-section-label">Proposed change</div>
      <pre className="config-preview">
        {run.configParents.map((parent, index) => `${" ".repeat(index)}${parent}\n`).join("")}
        {run.configLines.map((line) => `${" ".repeat(run.configParents.length)}${line}`).join("\n")}
      </pre>

      <div className="precheck-section-label">Device precheck</div>
      <DevicePrecheck run={run} elapsed={elapsed} />

      <div className="precheck-note">
        Precheck is read-only on the device. Configuration is never applied from this page.
      </div>
    </div>
  );
}

export function Changes() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deviceId, setDeviceId] = useState("");
  const [parentsText, setParentsText] = useState("");
  const [linesText, setLinesText] = useState("");
  const [run, setRun] = useState<PrecheckRun | null>(null);
  const pollTimer = useRef<number | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;

    async function load() {
      try {
        const devicesData = await getDevices();

        if (cancelled) return;

        setDevices(devicesData);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load devices");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => {
      cancelled = true;
      mounted.current = false;
      if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    };
  }, []);

  const sortedDevices = useMemo(
    () => [...devices].sort((a, b) => a.hostname.localeCompare(b.hostname)),
    [devices],
  );
  const actionableDevices = sortedDevices.filter(isActionable);
  const viewOnlyDevices = sortedDevices.filter((device) => !isActionable(device));

  const selectedDevice = devices.find((device) => String(device.id) === deviceId) ?? null;
  const configLines = splitLines(linesText);
  const running = run?.phase === "submitting" || run?.phase === "polling";
  const canRunPrecheck =
    !running && selectedDevice !== null && isActionable(selectedDevice) && configLines.length > 0;

  // Any edit invalidates the previous result so stale output is never shown against new input.
  function edit(setter: (value: string) => void) {
    return (value: string) => {
      setter(value);
      setRun(null);
    };
  }

  function updateRun(patch: Partial<PrecheckRun>) {
    if (!mounted.current) return;
    setRun((current) => (current ? { ...current, ...patch } : current));
  }

  function poll(requestId: string, startedAt: number, consecutiveErrors: number) {
    pollTimer.current = window.setTimeout(async () => {
      if (!mounted.current) return;

      if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
        updateRun({
          phase: "failed",
          error: `Timed out waiting for the worker. Request ${requestId} may still finish — check Jobs and Approvals.`,
        });
        return;
      }

      try {
        const status = await getOs6Precheck(requestId);

        if (status.state === "completed") {
          updateRun({ phase: "completed", status });
        } else if (status.state === "failed") {
          updateRun({ phase: "failed", status, error: status.error });
        } else {
          updateRun({ status });
          poll(requestId, startedAt, 0);
        }
      } catch (err) {
        if (consecutiveErrors + 1 >= MAX_POLL_ERRORS) {
          updateRun({
            phase: "failed",
            error: `Lost contact with the API while waiting for request ${requestId}: ${
              err instanceof Error ? err.message : "unknown error"
            }`,
          });
        } else {
          poll(requestId, startedAt, consecutiveErrors + 1);
        }
      }
    }, POLL_INTERVAL_MS);
  }

  async function handleRunPrecheck() {
    if (!selectedDevice || running) return;

    const configParents = splitLines(parentsText);
    const findings = runLocalValidation(selectedDevice, configParents, configLines);
    const blocked = findings.some((finding) => finding.level === "error");
    const startedAt = Date.now();

    setRun({
      device: selectedDevice,
      configParents,
      configLines,
      findings,
      startedAt,
      phase: blocked ? "blocked" : "submitting",
      requestId: null,
      status: null,
      error: null,
    });

    if (blocked) return;

    try {
      // Only the device id and commands leave the browser; the worker resolves credentials from Vault.
      const submitted = await submitOs6Precheck({
        device_id: selectedDevice.id,
        config_parents: configParents,
        config_lines: configLines,
      });

      updateRun({ phase: "polling", requestId: submitted.request_id });
      poll(submitted.request_id, startedAt, 0);
    } catch (err) {
      updateRun({ phase: "failed", error: err instanceof Error ? err.message : "Failed to submit precheck" });
    }
  }

  return (
    <>
      <h1 className="page-title">Changes</h1>
      <p className="page-subtitle">Prepare a guarded OS6 configuration change. OS10 devices are view-only.</p>

      {error && <div className="error-banner">Failed to load devices: {error}</div>}

      <div className="changes-grid">
        <div className="panel">
          <div className="panel-header">
            <h2>OS6 Change Request</h2>
            <span className="count-tag">{actionableDevices.length} actionable</span>
          </div>
          <div className="change-form">
            <label className="form-field">
              <span className="form-label">Target device</span>
              <select
                className="filter-select"
                value={deviceId}
                disabled={loading || running}
                onChange={(event) => edit(setDeviceId)(event.target.value)}
              >
                <option value="">{loading ? "Loading devices…" : "Select a device"}</option>
                <optgroup label="OS6 — actionable">
                  {actionableDevices.map((device) => (
                    <option key={device.id} value={device.id}>
                      {device.hostname} ({device.management_ip})
                    </option>
                  ))}
                </optgroup>
                <optgroup label="View only">
                  {viewOnlyDevices.map((device) => (
                    <option key={device.id} value={device.id} disabled>
                      {device.hostname} — {device.platform}
                      {device.enabled ? "" : ", disabled"}
                    </option>
                  ))}
                </optgroup>
              </select>
            </label>

            <label className="form-field">
              <span className="form-label">Parent / context</span>
              <span className="form-hint">One per line, outermost first. Leave empty for global config.</span>
              <textarea
                className="config-input mono"
                rows={2}
                placeholder="interface Tw1/0/4"
                value={parentsText}
                disabled={running}
                onChange={(event) => edit(setParentsText)(event.target.value)}
              />
            </label>

            <label className="form-field">
              <span className="form-label">Configuration lines</span>
              <span className="form-hint">One command per line. Do not include configure / exit / end.</span>
              <textarea
                className="config-input mono"
                rows={6}
                placeholder="description NETOPS-TEST"
                value={linesText}
                disabled={running}
                onChange={(event) => edit(setLinesText)(event.target.value)}
              />
            </label>

            <div className="form-actions">
              <button
                type="button"
                className="primary-button"
                disabled={!canRunPrecheck}
                onClick={handleRunPrecheck}
              >
                {running ? "Running Precheck…" : "Run Precheck"}
              </button>
              <span className="view-only-tag">Apply is not available from this page yet.</span>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <h2>Precheck Results</h2>
          </div>
          <div className="panel-body" style={{ maxHeight: "none" }}>
            <PrecheckResults run={run} />
          </div>
        </div>
      </div>
    </>
  );
}
