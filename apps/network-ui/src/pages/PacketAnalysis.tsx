import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { deletePcapAnalysis, getPcapAnalysis, listPcapAnalyses, uploadPcapAnalysis } from "../api/client";
import type { PcapAnalysis, PcapFinding, PcapFlow, PcapMode, PcapResult, PcapStatus } from "../api/types";
import { Banner, EmptyState, TableSkeleton } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { Icon } from "../components/Icon";
import { KpiCard } from "../components/KpiCard";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";

// Client-side checks only; the API re-validates name, magic bytes and size.
const MAX_UPLOAD_MB = 200;
const EXTENSIONS = [".pcap", ".pcapng"];
const POLL_MS = 1500;
const ACTIVE: PcapStatus[] = ["queued", "validating", "extracting", "analyzing", "correlating"];

const STEPS: { label: string; status: PcapStatus[]; dualOnly?: boolean }[] = [
  { label: "Validating", status: ["validating"] },
  { label: "Reading packets", status: ["extracting"] },
  { label: "Building flows", status: ["extracting"] },
  { label: "Analyzing TCP", status: ["analyzing"] },
  { label: "Analyzing DNS/TLS", status: ["analyzing"] },
  { label: "Correlating captures", status: ["correlating"], dualOnly: true },
  { label: "Generating findings", status: [] },
];
const STATUS_ORDER: PcapStatus[] = ["queued", "validating", "extracting", "analyzing", "correlating", "completed"];

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s < 1) return `${(s * 1000).toFixed(0)} ms`;
  if (s < 120) return `${s.toFixed(2)} s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(1)} ms`;
}

function endpoint(e: { ip: string; port: number | null }) {
  return e.ip.includes(":") ? `[${e.ip}]:${e.port}` : `${e.ip}:${e.port}`;
}

function fileProblem(file: File | null): string | null {
  if (!file) return null;
  if (!EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext))) return "Only .pcap and .pcapng files are accepted.";
  if (file.size === 0) return "The file is empty.";
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) return `Files are limited to ${MAX_UPLOAD_MB} MB.`;
  return null;
}

function FilePicker({ label, file, onChange, id }: { label: string; file: File | null; onChange: (f: File | null) => void; id: string }) {
  const problem = fileProblem(file);
  const [dragging, setDragging] = useState(false);
  return (
    <div className="pcap-file">
      <span className="form-label">{label}</span>
      {/* The native input stays the source of truth (keyboard + screen readers); the drop zone is its label.
          A dropped file goes through the same validation as a picked one. */}
      <input id={id} className="sr-only pcap-file-input" type="file" accept=".pcap,.pcapng"
        onChange={(e) => onChange(e.target.files?.[0] ?? null)} />
      <label htmlFor={id} className={`pcap-dropzone${dragging ? " dragging" : ""}${file ? " has-file" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); onChange(e.dataTransfer.files?.[0] ?? null); }}>
        <Icon name="upload" size={20} />
        <span><strong>{file ? "Choose another file" : "Choose a capture"}</strong> or drag it here</span>
        <span className="muted">.pcap / .pcapng</span>
      </label>
      {file && (
        <div className="pcap-file-meta">
          <Icon name="file" size={14} />
          <span className="mono">{file.name}</span> · {formatBytes(file.size)} · {file.name.toLowerCase().endsWith(".pcapng") ? "pcapng" : "pcap"}
        </div>
      )}
      {problem && <span className="form-error">{problem}</span>}
    </div>
  );
}

function Progress({ analysis, uploadFraction }: { analysis: PcapAnalysis | null; uploadFraction: number | null }) {
  const status = analysis?.status ?? "queued";
  const index = STATUS_ORDER.indexOf(status);
  const steps = STEPS.filter((s) => !s.dualOnly || analysis?.mode === "dual");
  return (
    <div className="panel pcap-progress" aria-live="polite">
      <div className="panel-header">
        <h2>{uploadFraction !== null && uploadFraction < 1 ? "Uploading capture…" : "Analyzing capture…"}</h2>
        {uploadFraction !== null && uploadFraction < 1 && <span className="panel-header-meta">{Math.round(uploadFraction * 100)}%</span>}
      </div>
      <ol className="pcap-steps">
        {steps.map((step) => {
          const stepIndex = step.status.length ? Math.min(...step.status.map((s) => STATUS_ORDER.indexOf(s))) : STATUS_ORDER.length - 1;
          const state = !analysis ? "pending" : index > stepIndex ? "done" : index === stepIndex ? "active" : "pending";
          return (
            <li key={step.label} className={`pcap-step ${state}`} aria-current={state === "active" ? "step" : undefined}>
              <span className="pcap-step-dot" aria-hidden="true" />
              {step.label}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

const ASSESSMENT_LABEL: Record<string, string> = {
  likely: "Likely issue",
  possible: "Possible issue",
  multiple: "Multiple issues",
  none: "No single root cause",
};

function Assessment({ result }: { result: PcapResult }) {
  const a = result.assessment;
  return (
    <section className="panel pcap-assessment" aria-labelledby="pcap-summary-title">
      <div className="panel-header">
        <h2 id="pcap-summary-title">Analysis Summary</h2>
        <StatusBadge status={a.label === "none" ? "unknown" : a.label === "possible" ? "warning" : "failed"} label={ASSESSMENT_LABEL[a.label]} />
      </div>
      <div className="pcap-assessment-body">
        {a.issue && <div className="pcap-issue">{a.issue}</div>}
        <p className="pcap-summary-text">{a.summary}</p>
        {a.candidates && (
          <ul className="pcap-evidence">
            {a.candidates.map((c) => <li key={c.cause}><strong>{c.issue}</strong>{c.strong_evidence[0] ? ` — ${c.strong_evidence[0].text}` : ""}</li>)}
          </ul>
        )}
        <div className="pcap-evidence-grid">
          {a.strong_evidence.length > 0 && (
            <div>
              <div className="form-label">Strong evidence</div>
              <ul className="pcap-evidence">{a.strong_evidence.map((e) => <li key={e.finding}><span className="mono">{e.finding}</span> {e.text}</li>)}</ul>
            </div>
          )}
          {a.supporting_evidence.length > 0 && (
            <div>
              <div className="form-label">Supporting evidence</div>
              <ul className="pcap-evidence">{a.supporting_evidence.map((e) => <li key={e.finding}><span className="mono">{e.finding}</span> {e.text}</li>)}</ul>
            </div>
          )}
          {a.no_evidence_for.length > 0 && (
            <div>
              <div className="form-label">No evidence found for</div>
              <ul className="pcap-evidence muted">{a.no_evidence_for.map((t) => <li key={t}>{t}</li>)}</ul>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function CaptureSummary({ result, analysis }: { result: PcapResult; analysis: PcapAnalysis }) {
  const roles = Object.keys(result.observations.captures);
  return (
    <section className="panel" aria-labelledby="pcap-capture-title">
      <div className="panel-header">
        <h2 id="pcap-capture-title">Capture Summary</h2>
        {result.performance && <span className="panel-header-meta">analyzed in {formatMs(result.performance.analysis_ms)}</span>}
      </div>
      <div className="pcap-summary-body">
        {roles.map((role) => {
          const c = result.observations.captures[role];
          const name = role === "server" ? analysis.server_filename : analysis.client_filename;
          return (
            <div key={role} className="pcap-capture">
              {result.mode === "dual" && <div className="form-label">{role === "client" ? "Client capture" : "Server capture"}</div>}
              <div className="pcap-capture-meta mono">
                {name} · {c.metadata.file_type} · {c.metadata.encapsulation} · {formatBytes(c.metadata.size_bytes)}
                {c.metadata.first_packet ? ` · ${c.metadata.first_packet}` : ""}
              </div>
              <div className="kpi-grid pcap-kpis">
                <KpiCard label="Packets" value={c.summary.packets.toLocaleString()} />
                <KpiCard label="Duration" value={formatSeconds(c.summary.duration_s)} />
                <KpiCard label="Flows" value={c.summary.flows.toLocaleString()} />
                <KpiCard label="TCP streams" value={c.summary.tcp_streams.toLocaleString()} />
                <KpiCard label="DNS queries" value={c.summary.dns_queries.toLocaleString()} />
                <KpiCard label="TLS sessions" value={c.summary.tls_sessions.toLocaleString()} />
              </div>
            </div>
          );
        })}
        {result.correlation && (
          <div className="kpi-grid pcap-kpis">
            <KpiCard label="Matched flows" value={result.correlation.matched_flows}
              hint={`${result.correlation.only_client_flows} client-only · ${result.correlation.only_server_flows} server-only`} />
            <KpiCard label="Clock offset estimate"
              value={result.correlation.clock.estimated_clock_offset_ms === null ? "—" : formatMs(result.correlation.clock.estimated_clock_offset_ms)}
              hint={`confidence: ${result.correlation.clock.confidence}`} dim={result.correlation.clock.estimated_clock_offset_ms === null} />
          </div>
        )}
      </div>
    </section>
  );
}

function EvidenceView({ evidence }: { evidence: Record<string, unknown> }) {
  const simple = Object.entries(evidence).filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v));
  const nested = Object.entries(evidence).filter(([, v]) => v !== null && typeof v === "object");
  return (
    <div className="pcap-evidence-view">
      {simple.length > 0 && (
        <dl className="detail-list compact">
          {simple.map(([k, v]) => (
            <div key={k} className="detail-pair"><dt>{k.replace(/_/g, " ")}</dt><dd className="mono">{String(v)}</dd></div>
          ))}
        </dl>
      )}
      {nested.map(([k, v]) => (
        <details key={k} className="pcap-evidence-json">
          <summary>{k.replace(/_/g, " ")} ({Array.isArray(v) ? v.length : Object.keys(v as object).length})</summary>
          <pre>{JSON.stringify(v, null, 2)}</pre>
        </details>
      ))}
    </div>
  );
}

function FindingCard({ finding, recommendations, onFlow }: { finding: PcapFinding; recommendations: string[]; onFlow: (id: string) => void }) {
  const tone = finding.severity === "critical" ? "error" : finding.severity;
  return (
    <article className={`pcap-finding finding-${tone}`} aria-labelledby={`finding-${finding.id}`}>
      <header className="pcap-finding-head">
        <span className="finding-level">{finding.severity}</span>
        <h3 id={`finding-${finding.id}`}>{finding.title}</h3>
        <span className="pcap-finding-meta mono">
          {finding.id} · {finding.category}{finding.capture && finding.capture !== finding.category ? ` · ${finding.capture} capture` : ""} · confidence {finding.confidence}
        </span>
      </header>
      <div className="pcap-finding-grid">
        <div><div className="form-label">Observed</div><p>{finding.observed}</p></div>
        <div><div className="form-label">Meaning</div><p>{finding.interpretation}</p></div>
      </div>
      <div className="form-label">Evidence</div>
      <EvidenceView evidence={finding.evidence} />
      {recommendations.length > 0 && (
        <>
          <div className="form-label">Recommendation</div>
          <ul className="pcap-recs">{recommendations.map((r) => <li key={r}>{r}</li>)}</ul>
        </>
      )}
      {finding.flows.length > 0 && (
        <div className="pcap-finding-flows">
          {finding.flows.map((id) => (
            <button key={id} type="button" className="copy-button" onClick={() => onFlow(id)}>{id}</button>
          ))}
        </div>
      )}
    </article>
  );
}

function FlowDetail({ flow, onClose }: { flow: PcapFlow; onClose: () => void }) {
  const tcp = flow.tcp ?? {};
  const num = (k: string) => (typeof tcp[k] === "number" ? (tcp[k] as number) : 0);
  const rows: [string, string][] = [
    ["Client", endpoint(flow.client) + (flow.client_inferred_by === "port_heuristic" ? " (inferred from ports)" : "")],
    ["Server", endpoint(flow.server)],
    ["Protocol / stream", `${flow.protocol.toUpperCase()} · stream ${flow.stream}`],
    ["Start / duration", `${formatSeconds(flow.start_s)} into capture · ${formatSeconds(flow.duration_s)}`],
    ["Client → server", `${flow.client_to_server.packets} packets · ${formatBytes(flow.client_to_server.bytes)}`
      + (flow.client_to_server.payload_bytes !== undefined ? ` (${formatBytes(flow.client_to_server.payload_bytes)} payload)` : "")],
    ["Server → client", `${flow.server_to_client.packets} packets · ${formatBytes(flow.server_to_client.bytes)}`
      + (flow.server_to_client.payload_bytes !== undefined ? ` (${formatBytes(flow.server_to_client.payload_bytes)} payload)` : "")],
  ];
  if (flow.handshake) {
    rows.push(["Handshake", `${flow.handshake.state.replace(/_/g, " ")} · ${flow.handshake.syn_packets} SYN`]);
    rows.push(["SYN → SYN/ACK", formatMs(flow.handshake.syn_to_synack_ms)]);
    rows.push(["SYN/ACK → ACK", formatMs(flow.handshake.synack_to_ack_ms)]);
  }
  if (flow.rtt) rows.push(["ACK RTT", `min ${formatMs(flow.rtt.min_ms)} · median ${formatMs(flow.rtt.median_ms)} · max ${formatMs(flow.rtt.max_ms)} (${flow.rtt.count})`]);
  if (flow.tcp) {
    rows.push(["Retransmissions", `${num("retransmissions")} (${num("fast_retransmissions")} fast, ${num("spurious_retransmissions")} spurious)`]);
    rows.push(["Duplicate ACKs / out-of-order", `${num("duplicate_acks")} / ${num("out_of_order")}`]);
    rows.push(["Resets", String(num("reset_count"))]);
    rows.push(["Zero window", `client ${num("zero_window_from_client")} · server ${num("zero_window_from_server")} · probes ${num("zero_window_probes")}`]);
  }
  if (flow.timing) rows.push(["Request → first response", formatMs(flow.timing.request_to_first_response_ms)]);
  if (flow.tls) {
    rows.push(["TLS", `${flow.tls.negotiated_version ?? "version not seen"}${flow.tls.sni ? ` · SNI ${flow.tls.sni}` : ""}`]);
    rows.push(["ClientHello → ServerHello", formatMs(flow.tls.client_hello_to_server_hello_ms)]);
    if (flow.tls.alerts.length) rows.push(["TLS alerts", flow.tls.alerts.map((a) => `${a.description} (${a.level ?? "?"}, from ${a.from})`).join(", ")]);
  }
  if (flow.dns_transactions) rows.push(["DNS transactions", String(flow.dns_transactions)]);
  if (flow.correlation) {
    const c = flow.correlation;
    rows.push(["Correlation", `matched by ${c.matched_by}`]);
    rows.push(["Seen at both points", `${c.delivered_client_to_server} client→server · ${c.delivered_server_to_client} server→client`]);
    rows.push(["Missing between capture points", `${c.missing_at_server} at server · ${c.missing_at_client} at client`]);
    rows.push(["Response time", `server side ${formatMs(c.server_side_response_ms)} · client side ${formatMs(c.client_side_response_ms)}`]);
  }
  return (
    <Modal kicker="Flow detail" title={flow.key} onClose={onClose}
      actions={<button type="button" className="secondary-button" onClick={onClose}>Close</button>}>
      <dl className="detail-list">
        {rows.map(([k, v]) => <div key={k} className="detail-pair"><dt>{k}</dt><dd>{v}</dd></div>)}
      </dl>
      {flow.issues.length > 0 && <div className="pcap-issues">{flow.issues.map((i) => <span key={i} className="tag tag-warn">{i}</span>)}</div>}
    </Modal>
  );
}

function Report({ analysis, onFlow }: { analysis: PcapAnalysis; onFlow: (flow: PcapFlow) => void }) {
  const result = analysis.result!;
  const [onlyIssues, setOnlyIssues] = useState("all");
  const flowsById = useMemo(() => new Map(result.flows.map((f) => [f.id, f])), [result]);
  const recsByFinding = useMemo(() => {
    const map = new Map<string, string[]>();
    result.recommendations.forEach((r) => r.findings.forEach((id) => map.set(id, [...(map.get(id) ?? []), r.text])));
    return map;
  }, [result]);
  const flows = onlyIssues === "issues" ? result.flows.filter((f) => f.issues.length) : result.flows;
  const openFlow = (id: string) => {
    const flow = flowsById.get(id);
    if (flow) onFlow(flow);
  };
  const tcpMetrics = result.metrics.client?.tcp as Record<string, unknown> | undefined;
  const hsRtt = tcpMetrics?.handshake_rtt_ms as { min: number; median: number; max: number } | null | undefined;

  return (
    <>
      <Assessment result={result} />
      <CaptureSummary result={result} analysis={analysis} />

      <section className="panel" aria-labelledby="pcap-findings-title">
        <div className="panel-header">
          <h2 id="pcap-findings-title">Expert Findings</h2>
          <span className="count-tag">{result.findings.length}</span>
        </div>
        <div className="pcap-findings">
          {result.findings.length === 0 ? (
            <EmptyState title="No findings." hint="None of the TCP, DNS, TLS or ICMP rules matched this capture." />
          ) : (
            result.findings.map((f) => <FindingCard key={f.id} finding={f} recommendations={recsByFinding.get(f.id) ?? []} onFlow={openFlow} />)
          )}
        </div>
      </section>

      {result.recommendations.length > 0 && (
        <section className="panel" aria-labelledby="pcap-recs-title">
          <div className="panel-header"><h2 id="pcap-recs-title">Recommendations</h2></div>
          <ul className="pcap-recs pcap-recs-all">
            {result.recommendations.map((r) => <li key={r.text}>{r.text} <span className="muted mono">({r.findings.join(", ")})</span></li>)}
          </ul>
        </section>
      )}

      <section className="panel" aria-labelledby="pcap-flows-title">
        <div className="panel-header">
          <h2 id="pcap-flows-title">Flows</h2>
          <span className="panel-header-meta">
            {result.flows.length < result.flow_total ? `top ${result.flows.length} of ${result.flow_total}` : `${result.flow_total}`}
          </span>
          <FilterChips label="Flow filter" value={onlyIssues} onChange={setOnlyIssues}
            options={[{ value: "all", label: "All" }, { value: "issues", label: "With issues", count: result.flows.filter((f) => f.issues.length).length }]} />
        </div>
        <div className="table-wrap">
          <table className="data-table pcap-flow-table">
            <thead><tr><th>Client</th><th>Server</th><th>Protocol</th><th>Packets</th><th>Bytes</th><th>Duration</th><th>Issues</th></tr></thead>
            <tbody>
              {flows.map((f) => (
                <tr key={f.id} className="clickable-row" onClick={() => onFlow(f)}>
                  <td><button type="button" className="link-button mono" onClick={(e) => { e.stopPropagation(); onFlow(f); }}>{endpoint(f.client)}</button></td>
                  <td className="mono">{endpoint(f.server)}</td>
                  <td>{f.protocol.toUpperCase()}{f.tls?.sni ? <span className="cell-sub">TLS · {f.tls.sni}</span> : null}</td>
                  <td className="cell-num">{f.packets.toLocaleString()}</td>
                  <td className="cell-num">{formatBytes(f.bytes)}</td>
                  <td className="cell-num">{formatSeconds(f.duration_s)}</td>
                  <td>{f.issues.length ? f.issues.map((i) => <span key={i} className="tag tag-warn">{i}</span>) : <span className="muted">—</span>}</td>
                </tr>
              ))}
              {flows.length === 0 && <tr><td colSpan={7}><EmptyState title="No flows match." /></td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel" aria-labelledby="pcap-tcp-title">
        <div className="panel-header"><h2 id="pcap-tcp-title">TCP Analysis</h2></div>
        {tcpMetrics && (
          <div className="kpi-grid pcap-kpis pcap-pad">
            <KpiCard label="Streams" value={String(tcpMetrics.streams)} />
            <KpiCard label="Handshake RTT (median)" value={hsRtt ? formatMs(hsRtt.median) : "—"} hint={hsRtt ? `min ${formatMs(hsRtt.min)} · max ${formatMs(hsRtt.max)}` : undefined} />
            <KpiCard label="Retransmissions" value={String(tcpMetrics.retransmissions)} />
            <KpiCard label="Duplicate ACKs" value={String(tcpMetrics.duplicate_acks)} />
            <KpiCard label="Resets" value={String(tcpMetrics.resets)} />
            <KpiCard label="Zero-window events" value={String(tcpMetrics.zero_window_events)} />
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="pcap-dns-title">
        <div className="panel-header"><h2 id="pcap-dns-title">DNS</h2><span className="count-tag">{result.dns.transactions.length}</span></div>
        <div className="table-wrap">
          {result.dns.transactions.length === 0 ? <EmptyState title="No DNS in this capture." /> : (
            <table className="data-table">
              <thead><tr><th>Time</th><th>Name</th><th>Type</th><th>Server</th><th>Response</th><th>Latency</th><th>Repeats</th></tr></thead>
              <tbody>
                {result.dns.transactions.map((t, i) => (
                  <tr key={i}>
                    <td className="mono">{formatSeconds(t.at_s)}</td><td className="mono">{t.name ?? "—"}</td><td>{t.type ?? "—"}</td>
                    <td className="mono">{t.server}</td>
                    <td>{t.answered ? <StatusBadge status={t.rcode === "NOERROR" ? "success" : "failed"} label={t.rcode ?? "?"} /> : <span className="muted">no response</span>}</td>
                    <td className="cell-num">{formatMs(t.latency_ms)}</td><td className="cell-num">{t.repeats}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="panel" aria-labelledby="pcap-tls-title">
        <div className="panel-header"><h2 id="pcap-tls-title">TLS</h2><span className="count-tag">{result.tls.sessions.length}</span></div>
        <div className="table-wrap">
          {result.tls.sessions.length === 0 ? <EmptyState title="No TLS handshakes in this capture." /> : (
            <table className="data-table">
              <thead><tr><th>Flow</th><th>SNI</th><th>Version</th><th>ClientHello → ServerHello</th><th>Alerts</th></tr></thead>
              <tbody>
                {result.tls.sessions.map((s) => (
                  <tr key={s.flow}>
                    <td><button type="button" className="link-button mono" onClick={() => openFlow(s.flow)}>{s.key}</button></td>
                    <td className="mono">{s.sni ?? "—"}</td><td>{s.negotiated_version ?? "—"}</td>
                    <td className="cell-num">{formatMs(s.client_hello_to_server_hello_ms)}</td>
                    <td>{s.alerts.length ? s.alerts.map((a, i) => <span key={i} className="tag tag-warn">{a.description} ({a.from})</span>) : <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {result.icmp.events.length > 0 && (
        <section className="panel" aria-labelledby="pcap-icmp-title">
          <div className="panel-header"><h2 id="pcap-icmp-title">ICMP</h2><span className="count-tag">{result.icmp.events.length}</span></div>
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Time</th><th>Message</th><th>From</th><th>To</th><th>MTU</th></tr></thead>
              <tbody>
                {result.icmp.events.map((e, i) => (
                  <tr key={i}><td className="mono">{formatSeconds(e.at_s)}</td><td>{e.name}{e.detail ? ` (${e.detail})` : ""}</td>
                    <td className="mono">{e.from}</td><td className="mono">{e.to}</td><td className="cell-num">{e.mtu ?? "—"}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}

export default function PacketAnalysis() {
  const [searchParams, setSearchParams] = useSearchParams();
  const currentId = searchParams.get("id");
  const [mode, setMode] = useState<PcapMode>("single");
  const [clientFile, setClientFile] = useState<File | null>(null);
  const [serverFile, setServerFile] = useState<File | null>(null);
  const [uploadFraction, setUploadFraction] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<PcapAnalysis | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [recent, setRecent] = useState<PcapAnalysis[] | null>(null);
  const [flow, setFlow] = useState<PcapFlow | null>(null);
  const timer = useRef<number | null>(null);

  const loadRecent = useCallback(async () => {
    try {
      setRecent(await listPcapAnalyses());
    } catch {
      setRecent((r) => r ?? []);
    }
  }, []);

  useEffect(() => { void loadRecent(); }, [loadRecent]);

  // Poll the current analysis while it is queued/running; stop on completed/failed.
  useEffect(() => {
    if (!currentId) {
      setAnalysis(null);
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const data = await getPcapAnalysis(currentId);
        if (cancelled) return;
        setAnalysis(data);
        setLoadError(null);
        if (ACTIVE.includes(data.status)) {
          timer.current = window.setTimeout(poll, POLL_MS);
        } else {
          void loadRecent();
        }
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : "Failed to load analysis");
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [currentId, loadRecent]);

  const problems = [fileProblem(clientFile), mode === "dual" ? fileProblem(serverFile) : null].filter(Boolean);
  const ready = !!clientFile && (mode === "single" || !!serverFile) && problems.length === 0 && uploadFraction === null;

  async function analyze() {
    if (!clientFile) return;
    setUploadError(null);
    setUploadFraction(0);
    try {
      const { analysis_id } = await uploadPcapAnalysis(mode, clientFile, mode === "dual" ? serverFile : null, setUploadFraction);
      setAnalysis(null);
      setSearchParams({ id: analysis_id });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploadFraction(null);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("Delete this analysis result? This cannot be undone.")) return;
    try {
      await deletePcapAnalysis(id);
      if (id === currentId) setSearchParams({});
      void loadRecent();
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Delete failed");
    }
  }

  const active = analysis && ACTIVE.includes(analysis.status);

  return (
    <>
      <PageHeader title="Packet Analysis"
        subtitle="Upload a capture (or a client + server pair). TShark extracts protocol metadata; deterministic rules produce the findings. Payloads are never shown." />

      <section className="panel" aria-labelledby="pcap-upload-title">
        <div className="panel-header"><h2 id="pcap-upload-title">New analysis</h2></div>
        <div className="pcap-upload">
          <FilterChips label="Analysis mode" value={mode} onChange={(v) => setMode(v as PcapMode)}
            options={[{ value: "single", label: "Single capture" }, { value: "dual", label: "Client + Server" }]} />
          <div className="pcap-files">
            <FilePicker id="pcap-client" label={mode === "dual" ? "Client capture" : "Capture"} file={clientFile} onChange={setClientFile} />
            {mode === "dual" && <FilePicker id="pcap-server" label="Server capture" file={serverFile} onChange={setServerFile} />}
          </div>
          <div className="pcap-upload-actions">
            <button type="button" className="primary-button" disabled={!ready} onClick={analyze}>
              {uploadFraction === null && <Icon name="play" size={12} />}
              {uploadFraction !== null ? `Uploading… ${Math.round(uploadFraction * 100)}%` : "Analyze"}
            </button>
            <span className="muted small-note">.pcap / .pcapng, up to {MAX_UPLOAD_MB} MB each. Files are deleted when the analysis finishes.</span>
          </div>
          {uploadFraction !== null && (
            <div className="upload-progress" role="progressbar" aria-label="Upload progress" aria-valuemin={0} aria-valuemax={100}
              aria-valuenow={Math.round(uploadFraction * 100)}>
              <span style={{ width: `${Math.round(uploadFraction * 100)}%` }} />
            </div>
          )}
          {uploadError && <Banner tone="danger" title="Upload rejected.">{uploadError}</Banner>}
        </div>
      </section>

      {(uploadFraction !== null || active) && <Progress analysis={analysis} uploadFraction={uploadFraction} />}
      {loadError && <Banner tone="danger" title="Could not load the analysis.">{loadError}</Banner>}
      {analysis?.status === "failed" && (
        <Banner tone="danger" title="Analysis failed.">{analysis.error ?? "The capture could not be analyzed."}</Banner>
      )}
      {analysis?.status === "completed" && analysis.result && <Report analysis={analysis} onFlow={setFlow} />}
      {flow && <FlowDetail flow={flow} onClose={() => setFlow(null)} />}

      <section className="panel" aria-labelledby="pcap-recent-title">
        <div className="panel-header"><h2 id="pcap-recent-title">Recent analyses</h2><span className="count-tag">{recent?.length ?? 0}</span></div>
        <div className="table-wrap">
          {recent === null ? <TableSkeleton rows={3} columns={6} /> : (
            <table className="data-table">
              <thead><tr><th>Created</th><th>Capture</th><th>Mode</th><th>Status</th><th>Result</th><th /></tr></thead>
              <tbody>
                {recent.map((a) => (
                  <tr key={a.id} className={a.id === currentId ? "row-selected" : undefined}>
                    <td><RelativeTime value={a.created_at} /></td>
                    <td className="mono">
                      <button type="button" className="link-button mono" onClick={() => setSearchParams({ id: a.id })}>{a.client_filename}</button>
                      {a.server_filename && <span className="cell-sub">+ {a.server_filename}</span>}
                    </td>
                    <td>{a.mode === "dual" ? "Client + Server" : "Single"}</td>
                    <td><StatusBadge status={a.status === "completed" ? "success" : a.status === "failed" ? "failed" : "running"} label={a.status} /></td>
                    <td>
                      {a.status === "completed"
                        ? (a.likely_issue ?? "No single root cause")
                          + (a.finding_counts ? ` · ${a.finding_counts.critical} critical, ${a.finding_counts.warning} warning` : "")
                        : a.error ?? "—"}
                    </td>
                    <td>
                      {!ACTIVE.includes(a.status) && (
                        <button type="button" className="secondary-button small-button destructive" onClick={() => remove(a.id)}
                          aria-label={`Delete analysis of ${a.client_filename}`}>Delete</button>
                      )}
                    </td>
                  </tr>
                ))}
                {recent.length === 0 && <tr><td colSpan={6}><EmptyState title="No analyses yet." /></td></tr>}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </>
  );
}
