import { useEffect, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { getChangePrecheck, getDevices, submitChangePrecheck } from "../api/client";
import { CHANGE_PLATFORMS, OS10_PLATFORM, OS6_PLATFORM } from "../api/constants";
import type { ConfigBlock, Device, Os6PrecheckStatus, PrecheckBlock } from "../api/types";
import { CliPreview, PrecheckChecksView, SemanticBadge, blockTitle } from "../components/ChangeBlocks";
import { CopyButton } from "../components/CopyButton";
import { Banner, EmptyState } from "../components/Feedback";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import {
  blockProblems,
  commandTotal,
  emptyBlock,
  newBlockId,
  renderCliPreview,
  splitPastedLines,
  toRequestBlocks,
  verificationProblems,
  type EditorBlock,
} from "../utils/configBlocks";
import { platformLabel, truncateId, truncateText } from "../utils/format";

type FindingLevel = "error" | "warning" | "info";

interface Finding {
  level: FindingLevel;
  text: string;
}

type RunPhase = "blocked" | "submitting" | "polling" | "completed" | "failed";

interface PrecheckRun {
  device: Device;
  blocks: ConfigBlock[];
  verificationCommands: string[];
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

function isActionable(device: Device): boolean {
  return CHANGE_PLATFORMS.includes(device.platform) && device.enabled;
}

function splitLines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

// Structural checks only (the API and worker repeat them); no command-type policy exists.
function runLocalValidation(device: Device, blocks: ConfigBlock[], verificationCommands: string[]): Finding[] {
  const findings: Finding[] = [];
  if (!isActionable(device)) {
    findings.push({
      level: "error",
      text: `${device.hostname} (${device.platform}) is view-only. Only enabled Dell OS6 / Dell OS10 devices accept changes.`,
    });
  }
  blockProblems(blocks).forEach((text) => findings.push({ level: "error", text }));
  verificationProblems(verificationCommands).forEach((text) => findings.push({ level: "error", text }));
  if (findings.length === 0) {
    findings.push({
      level: "info",
      text: `${blocks.length} block(s), ${commandTotal(blocks)} command(s). Commands are not restricted by type; the device is the syntax authority.`,
    });
  }
  return findings;
}

// status drives the shared badge color: failed=red, submitting/running=blue (pulsing), queued=blue,
// pending=amber, passed=green.
function runSummary(run: PrecheckRun): { label: string; status: string } {
  switch (run.phase) {
    case "blocked":
      return { label: "Blocked by validation", status: "failed" };
    case "submitting":
      return { label: "Submitting", status: "submitting" };
    case "polling":
      return run.status?.state === "running"
        ? { label: "Checking device", status: "running" }
        : { label: "Waiting for worker", status: "queued" };
    case "failed":
      return { label: "Failed", status: "failed" };
    case "completed":
      if (run.status?.result?.status === "precheck_failed") return { label: "Precheck failed", status: "failed" };
      return run.status?.result?.status === "no_change_required"
        ? { label: "Passed · no change required", status: "passed" }
        : { label: "Passed · approval pending", status: "pending" };
  }
}

function FindingList({ findings }: { findings: Finding[] }) {
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

function PrecheckBlockRow({ block }: { block: PrecheckBlock }) {
  return (
    <div className={`precheck-block ${block.status === "FAILED" ? "is-failed" : ""}`}>
      <div className="precheck-block-head">
        <StatusBadge status={block.status.toLowerCase()} label={block.status} tone={block.status === "PASS" ? "success" : "danger"} />
        <strong>{blockTitle(block, block.index)}</strong>
        {block.parent && <code>{block.parent}</code>}
        <span className="muted">
          {block.parent ? (block.current_config_found ? "current config captured" : "not in running config") : "global"}
        </span>
      </div>
      {block.issues.length > 0 && <div className="form-error">{block.issues.join("; ")}</div>}
      <ul className="semantic-list">
        {block.commands.map((command, n) => (
          <li key={n}>
            <SemanticBadge result={command} />
            <span className="mono">{command.command}</span>
            <span className="muted">{command.detail}</span>
          </li>
        ))}
      </ul>
      {block.parent && block.current_config.length > 0 && (
        <details className="change-details">
          <summary>Current configuration ({block.current_config.length} lines)</summary>
          <pre className="config-preview">{[block.parent, ...block.current_config.map((l) => ` ${l}`)].join("\n")}</pre>
        </details>
      )}
    </div>
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
        Reading the running configuration of {run.device.hostname} and checking {run.blocks.length} block(s)&hellip; {elapsed}s
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
  const dryRun = result.dry_run;

  return (
    <>
      {dryRun.checks && <PrecheckChecksView checks={dryRun.checks} overall={dryRun.overall} />}

      {result.status === "precheck_failed" && (
        <ul className="finding-list">
          {(result.rejection_reasons ?? []).map((reason, index) => (
            <li key={index} className="finding finding-error">
              <span className="finding-level">failed</span>
              {reason}
            </li>
          ))}
          <li className="finding finding-info">The change is one unit: nothing was backed up, approved or sent to the device.</li>
        </ul>
      )}

      <div className="precheck-section-label">Per-block result</div>
      {(dryRun.blocks ?? []).map((block) => (
        <PrecheckBlockRow key={block.index} block={block} />
      ))}
      <div className="form-hint">
        "Not available" means the command is accepted for execution but cannot be semantically pre-validated; the device
        validates its syntax at apply time.
      </div>

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
          One pending approval <span className="mono">{truncateId(result.approval.approval_id)}</span> covers all{" "}
          {result.block_count ?? run.blocks.length} block(s). Review it on the <Link to="/approvals">Approvals</Link> page.
          Nothing has been applied.
        </div>
      )}

      {result.status === "no_change_required" && (
        <div className="finding finding-ok">
          Every command is already in effect on the device. No backup or approval was created.
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
      <EmptyState
        title="No precheck yet."
        hint="Select a Dell OS6 or Dell OS10 device, build one or more configuration blocks, then run a precheck."
      />
    );
  }

  const summary = runSummary(run);
  const elapsed = Math.max(0, Math.round((now - run.startedAt) / 1000));

  return (
    <div className="precheck-results">
      <div className="precheck-summary">
        <StatusBadge status={summary.status} label={summary.label} />
        <span className="target-host">{run.device.hostname}</span>
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

      <div className="precheck-section-label">Structural validation</div>
      <FindingList findings={run.findings} />

      <CliPreview text={renderCliPreview(run.blocks)} label="Submitted change" />

      <div className="precheck-section-label">Device precheck</div>
      <DevicePrecheck run={run} elapsed={elapsed} />

      <div className="precheck-note">
        Precheck is read-only on the device. Configuration is never applied from this page.
      </div>
    </div>
  );
}

interface BlockEditorProps {
  block: EditorBlock;
  index: number;
  count: number;
  disabled: boolean;
  onChange: (block: EditorBlock) => void;
  onMove: (delta: number) => void;
  onDuplicate: () => void;
  onDelete: () => void;
}

function BlockEditor({ block, index, count, disabled, onChange, onMove, onDuplicate, onDelete }: BlockEditorProps) {
  const isGlobal = block.parent.trim().length === 0;
  const commandRefs = useRef<(HTMLInputElement | null)[]>([]);
  const focusIndex = useRef<number | null>(null);

  useEffect(() => {
    if (focusIndex.current !== null) {
      commandRefs.current[focusIndex.current]?.focus();
      focusIndex.current = null;
    }
  });

  function setCommands(commands: string[], focus?: number) {
    if (focus !== undefined) focusIndex.current = focus;
    onChange({ ...block, commands });
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>, n: number) {
    if (event.key === "Enter") {
      event.preventDefault();
      const next = [...block.commands];
      next.splice(n + 1, 0, "");
      setCommands(next, n + 1);
    } else if (event.key === "Backspace" && block.commands[n] === "" && block.commands.length > 1) {
      event.preventDefault();
      setCommands(block.commands.filter((_, i) => i !== n), Math.max(0, n - 1));
    }
  }

  function onPaste(event: ClipboardEvent<HTMLInputElement>, n: number) {
    const lines = splitPastedLines(event.clipboardData.getData("text"));
    if (lines.length < 2) return;
    event.preventDefault();
    const next = [...block.commands];
    const merged = block.commands[n].trim() ? [block.commands[n], ...lines] : lines;
    next.splice(n, 1, ...merged.map((line) => line.trim()));
    setCommands(next, n + merged.length - 1);
  }

  return (
    <div className={`block-editor${isGlobal ? " is-global" : ""}`}>
      <div className="block-editor-head">
        <span className="block-number">{index + 1}</span>
        <strong>Block {index + 1}</strong>
        {isGlobal && <span className="global-tag">GLOBAL CONFIGURATION</span>}
        <div className="block-editor-tools">
          <button type="button" className="icon-button" disabled={disabled || index === 0} onClick={() => onMove(-1)}
            aria-label={`Move block ${index + 1} up`} title="Move up">↑</button>
          <button type="button" className="icon-button" disabled={disabled || index === count - 1} onClick={() => onMove(1)}
            aria-label={`Move block ${index + 1} down`} title="Move down">↓</button>
          <button type="button" className="secondary-button small-button" disabled={disabled} onClick={onDuplicate}>
            Duplicate
          </button>
          <button type="button" className="secondary-button small-button destructive" disabled={disabled || count === 1}
            onClick={onDelete}>
            Delete
          </button>
        </div>
      </div>

      <label className="form-field">
        <span className="form-label">Parent / context</span>
        <input
          type="text"
          className="search-input mono block-input"
          placeholder="e.g. interface ethernet1/1/18 — leave empty for global configuration"
          aria-label={`Block ${index + 1} parent`}
          spellCheck={false}
          value={block.parent}
          disabled={disabled}
          onChange={(event) => onChange({ ...block, parent: event.target.value })}
        />
      </label>

      <div className="form-field">
        <span className="form-label">Commands (in order)</span>
        <ol className="command-list">
          {block.commands.map((command, n) => (
            <li key={n} className="command-row">
              <span className="line-no">{n + 1}</span>
              <input
                ref={(element) => {
                  commandRefs.current[n] = element;
                }}
                type="text"
                className="search-input mono block-input"
                aria-label={`Block ${index + 1} command ${n + 1}`}
                spellCheck={false}
                placeholder={n === 0 ? (isGlobal ? "e.g. ip routing" : "e.g. description APP-SERVER") : ""}
                value={command}
                disabled={disabled}
                onKeyDown={(event) => onKeyDown(event, n)}
                onPaste={(event) => onPaste(event, n)}
                onChange={(event) => {
                  const next = [...block.commands];
                  next[n] = event.target.value;
                  setCommands(next);
                }}
              />
              <button type="button" className="icon-button" disabled={disabled || block.commands.length === 1}
                aria-label={`Delete block ${index + 1} command ${n + 1}`} title="Delete command"
                onClick={() => setCommands(block.commands.filter((_, i) => i !== n))}>
                ×
              </button>
            </li>
          ))}
        </ol>
        <div className="block-editor-foot">
          <button type="button" className="secondary-button small-button" disabled={disabled}
            onClick={() => setCommands([...block.commands, ""], block.commands.length)}>
            + Add command
          </button>
          <span className="form-hint">Enter adds a line; pasting several lines adds one command per line.</span>
        </div>
        {isGlobal && (
          <span className="form-hint">
            Runs in global configuration. A command that opens a context (e.g. <span className="mono">router ospf 1</span>) is
            sent as raw CLI, and the following commands of this block run inside it. Each block ends with{" "}
            <span className="mono">end</span>.
          </span>
        )}
      </div>
    </div>
  );
}

export function Changes() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deviceId, setDeviceId] = useState("");
  const [blocks, setBlocks] = useState<EditorBlock[]>(() => [emptyBlock()]);
  const [verificationText, setVerificationText] = useState("");
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

  const sortedDevices = useMemo(() => [...devices].sort((a, b) => a.hostname.localeCompare(b.hostname)), [devices]);
  const actionableDevices = sortedDevices.filter(isActionable);
  const viewOnlyDevices = sortedDevices.filter((device) => !isActionable(device));

  const selectedDevice = devices.find((device) => String(device.id) === deviceId) ?? null;
  const requestBlocks = useMemo(() => toRequestBlocks(blocks), [blocks]);
  const verificationCommands = splitLines(verificationText);
  const totalCommands = commandTotal(requestBlocks);
  const running = run?.phase === "submitting" || run?.phase === "polling";
  const canRunPrecheck = !running && selectedDevice !== null && isActionable(selectedDevice) && totalCommands > 0;

  // Any edit invalidates the previous result so stale output is never shown against new input.
  function editBlocks(update: (current: EditorBlock[]) => EditorBlock[]) {
    setBlocks(update);
    setRun(null);
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
        const status = await getChangePrecheck(requestId);
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

    const findings = runLocalValidation(selectedDevice, requestBlocks, verificationCommands);
    const blocked = findings.some((finding) => finding.level === "error");
    const startedAt = Date.now();

    setRun({
      device: selectedDevice,
      blocks: requestBlocks,
      verificationCommands,
      findings,
      startedAt,
      phase: blocked ? "blocked" : "submitting",
      requestId: null,
      status: null,
      error: null,
    });

    if (blocked) return;

    try {
      // Only the device id, the ordered blocks and read-only verification commands leave the
      // browser; the worker resolves credentials from Vault.
      const submitted = await submitChangePrecheck({
        device_id: selectedDevice.id,
        blocks: requestBlocks,
        verification_commands: verificationCommands,
      });
      updateRun({ phase: "polling", requestId: submitted.request_id });
      poll(submitted.request_id, startedAt, 0);
    } catch (err) {
      updateRun({ phase: "failed", error: err instanceof Error ? err.message : "Failed to submit precheck" });
    }
  }

  return (
    <>
      <PageHeader
        title="Changes"
        subtitle="Build a guarded configuration change as ordered blocks (Dell OS6 or Dell OS10). Prechecks are read-only."
      />

      {error && (
        <Banner tone="danger" title="Failed to load devices.">
          {error}
        </Banner>
      )}

      <div className="changes-grid">
        <div className="panel">
          <div className="panel-header">
            <h2>Change Request</h2>
            <span className="count-tag">
              {blocks.length} block(s) · {totalCommands} command(s)
            </span>
          </div>
          <div className="change-form">
            <div className="form-section">
              <span className="form-section-title">
                <span className="step-number">1</span>Target device
              </span>
              <select
                aria-label="Target device"
                className="filter-select"
                value={deviceId}
                disabled={loading || running}
                onChange={(event) => {
                  setDeviceId(event.target.value);
                  setRun(null);
                }}
              >
                <option value="">{loading ? "Loading devices…" : "Select a device"}</option>
                <optgroup label="Dell OS6 — actionable">
                  {actionableDevices.filter((d) => d.platform === OS6_PLATFORM).map((device) => (
                    <option key={device.id} value={device.id}>
                      {device.hostname} ({device.management_ip})
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Dell OS10 — actionable">
                  {actionableDevices.filter((d) => d.platform === OS10_PLATFORM).map((device) => (
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
              {selectedDevice && (
                <span className="form-hint">
                  {platformLabel(selectedDevice.platform)} · {selectedDevice.management_ip} · commands are not restricted by
                  type; the device validates syntax. Precheck, backup and approval always apply.
                </span>
              )}
            </div>

            <div className="form-section">
              <span className="form-section-title">
                <span className="step-number">2</span>Configuration blocks
              </span>
              <span className="form-hint">
                Blocks run strictly in order; each enters its parent, runs its commands in order and returns to global
                configuration. Do not include configure / exit / end.
              </span>
              {blocks.map((block, index) => (
                <BlockEditor
                  key={block.id}
                  block={block}
                  index={index}
                  count={blocks.length}
                  disabled={running}
                  onChange={(next) => editBlocks((current) => current.map((b) => (b.id === block.id ? next : b)))}
                  onMove={(delta) =>
                    editBlocks((current) => {
                      const next = [...current];
                      const target = index + delta;
                      [next[index], next[target]] = [next[target], next[index]];
                      return next;
                    })
                  }
                  onDuplicate={() =>
                    editBlocks((current) => {
                      const next = [...current];
                      next.splice(index + 1, 0, { ...block, id: newBlockId(), commands: [...block.commands] });
                      return next;
                    })
                  }
                  onDelete={() => editBlocks((current) => current.filter((b) => b.id !== block.id))}
                />
              ))}
              <button
                type="button"
                className="secondary-button add-block-button"
                disabled={running}
                onClick={() => editBlocks((current) => [...current, emptyBlock()])}
              >
                + Add configuration block
              </button>
            </div>

            <label className="form-section">
              <span className="form-section-title">
                <span className="step-number">3</span>Verification commands (optional)
              </span>
              <span className="form-hint">
                Read-only <span className="mono">show ...</span> commands run after apply; their output is stored with the
                change. One per line.
              </span>
              <textarea
                className="config-input mono"
                rows={2}
                placeholder={"show running-configuration interface ethernet1/1/18\nshow vlan"}
                spellCheck={false}
                value={verificationText}
                disabled={running}
                onChange={(event) => {
                  setVerificationText(event.target.value);
                  setRun(null);
                }}
              />
            </label>

            <div className="form-section">
              <span className="form-section-title">
                <span className="step-number">4</span>CLI preview
              </span>
              <CliPreview text={renderCliPreview(requestBlocks.filter((b) => b.commands.length > 0 || b.parent))} />
            </div>

            <div className="form-actions">
              <button type="button" className="primary-button large-button" disabled={!canRunPrecheck} onClick={handleRunPrecheck}>
                {running ? "Running precheck…" : "Run precheck"}
              </button>
              <span className="form-hint">
                Read-only on the device. Approved changes are applied from{" "}
                <Link to="/approvals" className="secondary">
                  Approvals
                </Link>
                .
              </span>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <h2>Precheck Results</h2>
            {run ? (
              <StatusBadge status={runSummary(run).status} label={runSummary(run).label} />
            ) : (
              <span className="panel-header-meta">Idle</span>
            )}
          </div>
          <div className="panel-body" style={{ maxHeight: "none" }}>
            <PrecheckResults run={run} />
          </div>
        </div>
      </div>
    </>
  );
}
