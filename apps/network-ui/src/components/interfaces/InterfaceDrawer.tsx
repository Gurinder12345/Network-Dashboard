import { useCallback, useEffect, useRef, useState } from "react";
import { getInterfaceMetrics } from "../../api/client";
import type { InterfaceHistory, InterfaceItem, InterfaceSample, MetricsRange } from "../../api/types";
import { formatRelative, formatTimestamp } from "../../utils/format";
import { EmptyState, TableSkeleton } from "../Feedback";
import { MetricChart } from "../MetricChart";
import { StatusBadge } from "../StatusBadge";
import { ROLE_LABELS, STATUS_LABELS, STATUS_TONES, UTIL_THRESHOLDS, formatBytes, formatCount, formatSpeed, formatUtil } from "./format";

const RANGES: { value: MetricsRange; label: string; seconds: number }[] = [
  { value: "1h", label: "1H", seconds: 3600 },
  { value: "6h", label: "6H", seconds: 6 * 3600 },
  { value: "24h", label: "24H", seconds: 24 * 3600 },
  { value: "7d", label: "7D", seconds: 7 * 24 * 3600 },
];
const TABS = [
  { id: "overview", label: "Overview" },
  { id: "graphs", label: "Graphs" },
  { id: "counters", label: "Counters" },
] as const;
type DrawerTab = (typeof TABS)[number]["id"];

const rx = (s: InterfaceSample) => s.rx_utilization_pct;
const tx = (s: InterfaceSample) => s.tx_utilization_pct;
const errors = (s: InterfaceSample) => s.errors_delta;
const crc = (s: InterfaceSample) => s.crc_delta;
const discards = (s: InterfaceSample) => s.discards_delta;

interface DrawerProps {
  deviceId: number;
  item: InterfaceItem;
  collectedAt: string | null;
  stale: boolean;
  pollSeconds: number;
  onClose: () => void;
}

/** Right-side detail panel. History is fetched only while the Graphs tab is open. */
export function InterfaceDrawer({ deviceId, item, collectedAt, stale, pollSeconds, onClose }: DrawerProps) {
  const [tab, setTab] = useState<DrawerTab>("overview");
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
  }, [item.id]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside className="drawer" role="dialog" aria-modal="false" aria-labelledby="drawer-title">
      <header className="drawer-header">
        <div>
          <div className="modal-kicker">Interface</div>
          <h2 id="drawer-title" className="mono">
            {item.name}
          </h2>
          <div className="drawer-sub">
            <StatusBadge status={item.status} tone={STATUS_TONES[item.status]} label={STATUS_LABELS[item.status]} />
            {item.erroring && <span className="tag-warn">erroring</span>}
            {item.description && <span className="muted">{item.description}</span>}
          </div>
        </div>
        <button ref={closeRef} type="button" className="icon-button" aria-label="Close interface details" onClick={onClose}>
          ✕
        </button>
      </header>

      <div className="detail-tabs drawer-tabs" role="tablist" aria-label="Interface sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`drawer-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`drawer-panel-${t.id}`}
            className="detail-tab"
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="drawer-body" role="tabpanel" id={`drawer-panel-${tab}`} aria-labelledby={`drawer-tab-${tab}`}>
        {tab === "overview" && <OverviewPanel item={item} collectedAt={collectedAt} stale={stale} />}
        {tab === "graphs" && <GraphsPanel deviceId={deviceId} interfaceId={item.id} name={item.name} pollSeconds={pollSeconds} />}
        {tab === "counters" && <CountersPanel item={item} collectedAt={collectedAt} />}
      </div>
    </aside>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children ?? <span className="muted">—</span>}</dd>
    </>
  );
}

function OverviewPanel({ item, collectedAt, stale }: { item: InterfaceItem; collectedAt: string | null; stale: boolean }) {
  return (
    <dl className="detail-list compact">
      <Row label="Interface">
        <span className="mono">{item.name}</span>
      </Row>
      <Row label="Description">{item.description}</Row>
      <Row label="Admin status">{item.admin_status}</Row>
      <Row label="Operational status">{item.oper_status}</Row>
      <Row label="Type">{item.type.replace("_", "-")}</Row>
      <Row label="Role">{item.role ? `${ROLE_LABELS[item.role].label} — ${ROLE_LABELS[item.role].title}` : null}</Row>
      <Row label="Speed">{item.speed_bps ? formatSpeed(item.speed_bps) : null}</Row>
      <Row label="Duplex">{item.duplex}</Row>
      <Row label="Mode">{item.mode}</Row>
      <Row label="Access VLAN">{item.access_vlan}</Row>
      <Row label="Native VLAN">{item.native_vlan}</Row>
      <Row label="Allowed VLANs">{item.allowed_vlans}</Row>
      <Row label="Port-channel">{item.port_channel}</Row>
      <Row label="MTU">{item.mtu}</Row>
      <Row label="Last state change">
        {item.last_state_change ? `${formatTimestamp(item.last_state_change)} (${formatRelative(item.last_state_change)})` : null}
      </Row>
      <Row label="Last poll">{collectedAt ? `${formatTimestamp(collectedAt)} (${formatRelative(collectedAt)})` : null}</Row>
      <Row label="Freshness">
        {stale ? <StatusBadge status="stale" tone="warning" label="Stale" /> : <StatusBadge status="fresh" tone="success" label="Current" />}
      </Row>
    </dl>
  );
}

function CountersPanel({ item, collectedAt }: { item: InterfaceItem; collectedAt: string | null }) {
  const rows: [string, string][] = [
    ["RX bytes", formatBytes(item.rx_bytes)],
    ["TX bytes", formatBytes(item.tx_bytes)],
    ["RX packets", formatCount(item.rx_packets)],
    ["TX packets", formatCount(item.tx_packets)],
    ["RX errors", formatCount(item.rx_errors)],
    ["TX errors", formatCount(item.tx_errors)],
    ["CRC errors", formatCount(item.crc_errors)],
    ["Input discards", formatCount(item.input_discards)],
    ["Output discards", formatCount(item.output_discards)],
  ];
  return (
    <>
      <p className="small-note muted">
        Lifetime counters as reported by the switch at the last poll{collectedAt ? ` (${formatRelative(collectedAt)})` : ""}; “—”
        means the switch did not report that counter. Utilization in the latest interval: RX {formatUtil(item.rx_utilization_pct)}, TX{" "}
        {formatUtil(item.tx_utilization_pct)}.
      </p>
      <table className="data-table counters-table">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td className="cell-num">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function GraphsPanel({ deviceId, interfaceId, name, pollSeconds }: { deviceId: number; interfaceId: number; name: string; pollSeconds: number }) {
  const [range, setRange] = useState<MetricsRange>("24h");
  const [history, setHistory] = useState<{ data: InterfaceHistory; endMs: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (signal: AbortSignal) => {
      setHistory(null);
      setError(null);
      getInterfaceMetrics(deviceId, interfaceId, range, signal)
        .then((data) => {
          if (!signal.aborted) setHistory({ data, endMs: Date.now() });
        })
        .catch((err) => {
          if (!signal.aborted) setError(err instanceof Error ? err.message : "Failed to load history");
        });
    },
    [deviceId, interfaceId, range],
  );

  // One request per (interface, range); a newer selection or closing the drawer aborts it.
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const windowSeconds = RANGES.find((r) => r.value === range)!.seconds;
  const samples = history?.data.samples ?? [];
  const hasData = samples.some((s) => [rx, tx, errors, crc, discards].some((f) => f(s) !== null));

  return (
    <>
      <div className="chip-group" role="group" aria-label="History range">
        {RANGES.map((r) => (
          <button key={r.value} type="button" className="chip" aria-pressed={range === r.value} onClick={() => setRange(r.value)}>
            {r.label}
          </button>
        ))}
      </div>
      {error && <EmptyState title="History unavailable" hint={error} />}
      {!error && !history && <TableSkeleton rows={4} columns={1} />}
      {history && !hasData && (
        <EmptyState
          title="Historical interface data not yet available"
          hint="Graphs appear after the worker has stored at least two polls of this interface."
        />
      )}
      {history && hasData && (
        <div className="drawer-charts">
          {(
            [
              ["RX utilization", rx, "%"],
              ["TX utilization", tx, "%"],
              ["Errors per interval", errors, "count"],
              ["CRC errors per interval", crc, "count"],
              ["Discards per interval", discards, "count"],
            ] as const
          ).map(([label, value, unit]) => (
            <section key={label} className="drawer-chart">
              <h3>{label}</h3>
              <MetricChart
                label={label}
                value={value}
                unit={unit}
                samples={samples}
                windowSeconds={windowSeconds}
                stepSeconds={history.data.interval_seconds || pollSeconds}
                endMs={history.endMs}
                thresholds={unit === "%" ? UTIL_THRESHOLDS : undefined}
                ariaLabel={`${label} of ${name}, last ${RANGES.find((r) => r.value === range)!.label.toLowerCase()}`}
              />
            </section>
          ))}
          {history.data.truncated && <div className="small-note">History truncated to the newest samples.</div>}
        </div>
      )}
    </>
  );
}
