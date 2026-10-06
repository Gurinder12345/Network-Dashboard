import type { ConfigBlock, ExecutionResult, PrecheckChecks, PrecheckSummary, SemanticResult } from "../api/types";
import { StatusBadge, type Tone } from "./StatusBadge";

const BLOCK_STATUS_TONE: Record<string, Tone> = {
  applied: "success",
  failed: "danger",
  not_attempted: "neutral",
  pending: "neutral",
  applying: "info",
  unknown: "warning",
};

const SEMANTIC_LABEL: Record<string, { label: string; tone: Tone }> = {
  verified: { label: "Verified", tone: "success" },
  not_present: { label: "Not present", tone: "warning" },
  not_available: { label: "Not available", tone: "neutral" },
};

export function blockTitle(block: { parent: string | null }, index: number): string {
  return `Block ${index + 1}${block.parent ? "" : " · Global configuration"}`;
}

/** Read-only ordered blocks: parent, numbered child commands. */
export function BlocksView({ blocks, legacy }: { blocks: ConfigBlock[]; legacy?: boolean }) {
  return (
    <div className="blocks-view">
      {legacy && <div className="form-hint">Historical single-parent change (shown as one block).</div>}
      {blocks.map((block, index) => (
        <div className="blocks-view-block" key={index}>
          <div className="blocks-view-head">
            <span className="block-number">{index + 1}</span>
            {block.parent ? <code>{block.parent}</code> : <span className="global-tag">GLOBAL CONFIGURATION</span>}
            <span className="muted">{block.commands.length} command(s)</span>
          </div>
          <pre className="config-view-lines">
            {block.commands.map((line, n) => (
              <div key={n}>
                <span className="line-no">{n + 1}</span>
                {line}
              </div>
            ))}
          </pre>
        </div>
      ))}
    </div>
  );
}

export function CliPreview({ text, label = "CLI preview" }: { text: string; label?: string }) {
  return (
    <div className="cli-preview">
      <span className="form-label">{label} · read-only, exact execution order</span>
      <pre className="config-preview">{text || "(empty)"}</pre>
    </div>
  );
}

function checkTone(value: string): Tone {
  if (value === "PASS" || value === "SUPPORTED") return "success";
  if (value === "FAILED" || value === "FAIL") return "danger";
  if (value === "PARTIAL") return "warning";
  return "neutral";
}

const CHECK_LABELS: [keyof PrecheckChecks, string][] = [
  ["structural_validation", "Structural validation"],
  ["device_connectivity", "Device connectivity"],
  ["current_config_capture", "Current config capture"],
  ["semantic_verification", "Semantic verification"],
];

export function PrecheckChecksView({ checks, overall }: { checks: PrecheckChecks; overall?: string | null }) {
  return (
    <div className="check-grid">
      {CHECK_LABELS.map(([key, label]) => (
        <div className="check-item" key={key}>
          <span className="form-label">{label}</span>
          <StatusBadge status={checks[key].toLowerCase()} label={checks[key]} tone={checkTone(checks[key])} />
        </div>
      ))}
      {overall && (
        <div className="check-item">
          <span className="form-label">Overall</span>
          <StatusBadge status={overall.toLowerCase()} label={overall} tone={checkTone(overall)} />
        </div>
      )}
    </div>
  );
}

export function SemanticBadge({ result }: { result: SemanticResult }) {
  const meta = SEMANTIC_LABEL[result.status] ?? { label: result.status, tone: "neutral" as Tone };
  return <StatusBadge status={result.status} label={meta.label} tone={meta.tone} title={result.detail} />;
}

/** Precheck stored on the approval: per-block PASS/FAILED and semantic coverage. */
export function PrecheckSummaryView({ summary }: { summary: PrecheckSummary }) {
  const results = summary.blocks.flatMap((block) => block.commands);
  const count = (status: string) => results.filter((r) => r.status === status).length;
  return (
    <div className="precheck-summary-view">
      <PrecheckChecksView checks={summary.checks} overall={summary.overall} />
      <div className="form-hint">
        Semantic pre-validation: {count("verified")} already in effect, {count("not_present")} will change,{" "}
        {count("not_available")} not verifiable before apply (sent as written).
      </div>
      <ul className="block-status-list">
        {summary.blocks.map((block) => (
          <li key={block.index}>
            <StatusBadge status={block.status.toLowerCase()} label={block.status} tone={checkTone(block.status)} />
            <span>{blockTitle(block, block.index)}</span>
            {block.parent && <code>{block.parent}</code>}
            {block.issues.length > 0 && <span className="form-error">{block.issues.join("; ")}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function outcomeBadge(result: ExecutionResult) {
  switch (result.outcome) {
    case "applied":
      return <StatusBadge status="applied" label="Applied" />;
    case "partial_apply":
      return <StatusBadge status="failed" label="Partial apply" tone="danger" />;
    case "failed":
      return <StatusBadge status="failed" label="Failed" />;
    case "applying":
      return <StatusBadge status="applying" label="Applying" />;
    default:
      return <StatusBadge status="unknown" label="Result unknown" tone="warning" />;
  }
}

/** Per-block execution progress, partial apply, post-check and verification output. */
export function ExecutionView({ result, blocks }: { result: ExecutionResult; blocks: ConfigBlock[] }) {
  const semantic = result.semantic ?? "not run";
  const postBlocks = new Map((result.post_check?.blocks ?? []).map((b) => [b.index, b]));
  return (
    <div className="execution-view">
      <div className="execution-head">
        {outcomeBadge(result)}
        <span>
          Execution <strong>{result.execution}</strong> · Semantic verification <strong>{semantic.replace("_", " ")}</strong>
        </span>
      </div>
      {result.outcome === "partial_apply" && (
        <div className="finding finding-error">
          <span className="finding-level">partial apply</span>
          Some blocks were applied before a command was rejected. Network CLI is not transactional: applied blocks were
          not rolled back. Review the device and the pre-change backup.
        </div>
      )}
      {result.error && <div className="finding finding-warning">{result.error}</div>}
      <ol className="execution-blocks">
        {result.blocks.map((entry) => {
          const block = blocks[entry.index];
          const post = postBlocks.get(entry.index);
          return (
            <li key={entry.index} className={`execution-block status-${entry.status}`}>
              <div className="execution-block-head">
                <StatusBadge
                  status={entry.status}
                  label={entry.status.replace("_", " ")}
                  tone={BLOCK_STATUS_TONE[entry.status] ?? "neutral"}
                />
                <span>{blockTitle(entry, entry.index)}</span>
                {entry.parent && <code>{entry.parent}</code>}
                <span className="muted">{entry.command_count} command(s)</span>
              </div>
              {entry.status === "failed" && (
                <div className="form-error">
                  {entry.failed_command && block
                    ? `Command ${entry.failed_command} rejected: ${block.commands[entry.failed_command - 1] ?? ""}`
                    : `Rejected at ${entry.failed_step ?? "step"}`}
                  {entry.error ? ` — ${entry.error}` : ""}
                </div>
              )}
              {post && post.commands.length > 0 && (
                <ul className="semantic-list">
                  {post.commands.map((command, n) => (
                    <li key={n}>
                      <SemanticBadge result={command} />
                      <span className="mono">{command.command}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
      {result.post_check?.error && <div className="form-error">{result.post_check.error}</div>}
      {result.verification.length > 0 && (
        <div className="verification-outputs">
          <span className="form-label">Verification commands</span>
          {result.verification.map((item, n) => (
            <details key={n} className="error-details" style={{ maxWidth: "none" }}>
              <summary className="mono">
                {item.ok ? "✓" : "✗"} {item.command}
              </summary>
              <pre className="error-full">{item.output}</pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
