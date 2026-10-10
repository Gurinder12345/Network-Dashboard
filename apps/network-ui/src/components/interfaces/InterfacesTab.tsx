import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getDeviceInterfaces } from "../../api/client";
import type { DeviceInterfaces, InterfaceItem } from "../../api/types";
import { usePolling } from "../../hooks/usePolling";
import { formatRelative } from "../../utils/format";
import { BarList, Donut, type BarRow } from "../charts";
import { Banner, EmptyState, TableSkeleton } from "../Feedback";
import { FilterChips } from "../FilterChips";
import { KpiCard } from "../KpiCard";
import { RelativeTime } from "../RelativeTime";
import { WidgetCard } from "../overview/Widgets";
import { InterfaceDrawer } from "./InterfaceDrawer";
import {
  ROLE_LABELS,
  STATUS_LABELS,
  STATUS_TONES,
  formatCount,
  formatSpeed,
  formatUtil,
  utilLevel,
  vlanText,
} from "./format";
import { StatusBadge } from "../StatusBadge";

// Re-reads the cached snapshot (Redis); the worker collects every 5 minutes, never the browser.
const SNAPSHOT_POLL_MS = 60000;

type Filter = "all" | "up" | "down" | "admin_down" | "trunk" | "access" | "erroring";
type SortKey = "interface" | "rx" | "tx" | "errors" | "crc" | "discards" | "last_change";

const SORT_VALUE: Record<Exclude<SortKey, "interface">, (i: InterfaceItem) => number | null> = {
  rx: (i) => i.rx_utilization_pct,
  tx: (i) => i.tx_utilization_pct,
  errors: (i) => i.errors_delta,
  crc: (i) => i.crc_delta,
  discards: (i) => i.discards_delta,
  last_change: (i) => (i.last_state_change ? Date.parse(i.last_state_change) : null),
};

const MATCHES: Record<Filter, (i: InterfaceItem) => boolean> = {
  all: () => true,
  up: (i) => i.status === "up",
  down: (i) => i.status === "down",
  admin_down: (i) => i.status === "admin_down",
  trunk: (i) => i.mode === "trunk",
  access: (i) => i.mode === "access",
  erroring: (i) => i.erroring,
};

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function attemptText(data: DeviceInterfaces) {
  const attempt = data.last_attempt;
  if (!attempt || attempt.status === "success") return null;
  const when = attempt.at ? ` ${formatRelative(attempt.at)}` : "";
  if (attempt.status === "skipped_change") return `Latest poll${when} was skipped: a configuration change was in progress.`;
  if (attempt.status === "skipped_busy") return `Latest poll${when} was skipped: the device was busy with another operation.`;
  if (attempt.status === "skipped_unavailable") return `Latest poll${when} was skipped: device coordination was unavailable.`;
  if (attempt.status === "failed") return `Latest poll${when} failed: ${attempt.error ?? "unknown error"}.`;
  if (attempt.status === "partial") return null;
  return null;
}

/** `refreshToken` changes when the page's Refresh button is pressed (re-reads stored data only). */
export function InterfacesTab({ deviceId, refreshToken = 0 }: { deviceId: number; refreshToken?: number }) {
  const [data, setData] = useState<DeviceInterfaces | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "interface", dir: "asc" });
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const controller = useRef<AbortController | null>(null);
  const opener = useRef<HTMLElement | null>(null);

  const load = useCallback(async () => {
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    try {
      const snapshot = await getDeviceInterfaces(deviceId, current.signal);
      if (current.signal.aborted) return;
      setData(snapshot);
      setError(null);
    } catch (err) {
      if (current.signal.aborted) return;
      setError(err instanceof Error ? err.message : "Failed to load interfaces");
    } finally {
      if (!current.signal.aborted) setLoading(false);
    }
  }, [deviceId]);

  useEffect(() => {
    setData(null);
    setLoading(true);
    setSelectedId(null);
    void load();
    return () => controller.current?.abort();
  }, [load]);

  usePolling(load, SNAPSHOT_POLL_MS);

  const initialToken = useRef(refreshToken);
  useEffect(() => {
    if (refreshToken !== initialToken.current) void load();
  }, [refreshToken, load]);

  const interfaces = useMemo(() => data?.interfaces ?? [], [data]);

  const counts = useMemo(() => {
    const result = {} as Record<Filter, number>;
    (Object.keys(MATCHES) as Filter[]).forEach((f) => (result[f] = interfaces.filter(MATCHES[f]).length));
    return result;
  }, [interfaces]);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = interfaces.filter((i) => {
      if (!MATCHES[filter](i)) return false;
      if (!needle) return true;
      return [i.name, i.canonical_name, i.description, i.access_vlan, i.native_vlan, i.allowed_vlans]
        .some((value) => value !== null && value !== undefined && String(value).toLowerCase().includes(needle));
    });
    const sign = sort.dir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      if (sort.key === "interface") return sign * collator.compare(a.canonical_name, b.canonical_name);
      const va = SORT_VALUE[sort.key](a);
      const vb = SORT_VALUE[sort.key](b);
      if (va === null && vb === null) return collator.compare(a.canonical_name, b.canonical_name);
      if (va === null) return 1; // unknown values always last
      if (vb === null) return -1;
      return sign * (va - vb) || collator.compare(a.canonical_name, b.canonical_name);
    });
  }, [interfaces, filter, query, sort]);

  const topUtilization = useMemo<BarRow[]>(
    () =>
      interfaces
        .filter((i) => i.rx_utilization_pct !== null || i.tx_utilization_pct !== null)
        .map((i) => ({ i, peak: Math.max(i.rx_utilization_pct ?? 0, i.tx_utilization_pct ?? 0) }))
        .sort((a, b) => b.peak - a.peak)
        .slice(0, 5)
        .map(({ i, peak }) => ({
          key: String(i.id),
          label: i.name,
          sublabel: `RX ${formatUtil(i.rx_utilization_pct)} · TX ${formatUtil(i.tx_utilization_pct)}`,
          value: peak,
          tone: { normal: "info", warning: "warning", critical: "danger" }[utilLevel(peak)],
        })),
    [interfaces],
  );

  const topErrors = useMemo(
    () =>
      interfaces
        .filter((i) => (i.errors_delta ?? 0) + (i.crc_delta ?? 0) + (i.discards_delta ?? 0) > 0)
        .sort(
          (a, b) =>
            (b.errors_delta ?? 0) + (b.crc_delta ?? 0) - ((a.errors_delta ?? 0) + (a.crc_delta ?? 0)) ||
            (b.discards_delta ?? 0) - (a.discards_delta ?? 0),
        )
        .slice(0, 5),
    [interfaces],
  );

  const selected = selectedId === null ? null : interfaces.find((i) => i.id === selectedId) ?? null;

  function open(item: InterfaceItem, element: HTMLElement | null) {
    opener.current = element;
    setSelectedId(item.id);
  }

  function close() {
    setSelectedId(null);
    opener.current?.focus();
  }

  function sortBy(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "interface" ? "asc" : "desc" }));
  }

  function header(key: SortKey, label: string, numeric = false) {
    const active = sort.key === key;
    return (
      <th aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"} className={numeric ? "cell-num-head" : undefined}>
        <button type="button" className="sort-button" onClick={() => sortBy(key)}>
          {label}
          <span className="sort-indicator" aria-hidden="true">
            {active ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}
          </span>
        </button>
      </th>
    );
  }

  if (loading && !data) {
    return (
      <div className="panel" aria-busy="true">
        <TableSkeleton rows={8} columns={8} />
      </div>
    );
  }

  if (!data) {
    return <Banner tone="danger" title="Interface data could not be loaded.">{error}</Banner>;
  }

  const notice = attemptText(data);

  if (data.status === "not_collected" || !data.summary) {
    return (
      <div className="panel">
        <EmptyState
          title="No interface data collected yet."
          hint={
            notice ??
            `Interfaces are read with show commands every ${Math.round(data.poll_interval_seconds / 60)} minutes once interface polling is enabled; nothing is shown until real data exists.`
          }
        />
      </div>
    );
  }

  const summary = data.summary;
  const pollMinutes = Math.round(data.poll_interval_seconds / 60);

  return (
    <div className={`iface-tab${selected ? " drawer-open" : ""}`}>
      <div className="iface-freshness" role="status">
        {data.stale ? (
          <StatusBadge status="stale" tone="warning" label="Stale" />
        ) : (
          <StatusBadge status="fresh" tone="success" label="Current" />
        )}
        <span>
          {data.stale ? "Last successful poll " : "Last interface poll: "}
          <strong>{formatRelative(data.collected_at)}</strong>
          <span className="muted"> · polled every {pollMinutes} min · {data.source === "cache" ? "cached snapshot" : "from database"}</span>
        </span>
        {error && <span className="form-error">Refresh failed: {error}</span>}
      </div>

      {data.stale && (
        <Banner tone="warning" title="Interface data is stale.">
          The newest successful collection is {formatRelative(data.collected_at)}; values below are from then, not current.
          Stale ports are shown with their last known state, never as down. {notice}
        </Banner>
      )}
      {!data.stale && notice && <Banner tone="info" title="Latest poll did not update this data.">{notice}</Banner>}
      {data.collection_status === "partial" && data.problems.length > 0 && (
        <Banner tone="info" title="Partial collection: some fields are unavailable.">
          {data.problems.join("; ")}
        </Banner>
      )}

      <div className="kpi-grid iface-kpis">
        <KpiCard label="Total Interfaces" value={summary.total} icon="layers" tone="info" hint="Monitored ports and LAGs" />
        <KpiCard label="Up" value={summary.up} icon="check" tone="success" hint="Admin up, link up" />
        <KpiCard
          label="Down"
          value={summary.down}
          icon="alert"
          tone={summary.down ? "danger" : "neutral"}
          emphasize={summary.down > 0}
          hint={`Admin up, link down${summary.admin_down ? ` · ${summary.admin_down} admin down` : ""}`}
        />
        <KpiCard
          label="Erroring"
          value={summary.erroring}
          icon="alertCircle"
          tone={summary.erroring ? "warning" : "neutral"}
          emphasize={summary.erroring > 0}
          hint="Errors/CRC rose in the latest interval"
        />
        <KpiCard label="Trunks" value={summary.trunks} icon="topology" tone="info" />
        <KpiCard label="Access Ports" value={summary.access_ports} icon="server" tone="info" />
      </div>

      <div className="dash-row iface-charts">
        <WidgetCard title="Interface Utilization" icon="pulse" meta="top 5 · latest interval">
          {topUtilization.length ? (
            <BarList rows={topUtilization} ariaLabel="Highest interface utilization in the latest polling interval" />
          ) : (
            <EmptyState
              title="Utilization not available yet"
              hint="Needs two consecutive polls with valid counters and a known link speed."
            />
          )}
        </WidgetCard>
        <WidgetCard title="Interface Errors / Discards" icon="alert" meta="latest interval">
          {topErrors.length ? (
            <table className="mini-table" aria-label="Interfaces with errors or discards in the latest interval">
              <thead>
                <tr>
                  <th>Interface</th>
                  <th className="cell-num-head">Errors</th>
                  <th className="cell-num-head">CRC</th>
                  <th className="cell-num-head">Discards</th>
                </tr>
              </thead>
              <tbody>
                {topErrors.map((i) => (
                  <tr key={i.id}>
                    <td>
                      <button type="button" className="link-button" onClick={(e) => open(i, e.currentTarget)}>
                        {i.name}
                      </button>
                    </td>
                    <td className="cell-num">{formatCount(i.errors_delta)}</td>
                    <td className="cell-num">{formatCount(i.crc_delta)}</td>
                    <td className="cell-num">{formatCount(i.discards_delta)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title="No errors or discards in the latest interval"
              hint="Counts are increases between the two newest polls, not lifetime totals."
            />
          )}
        </WidgetCard>
        <WidgetCard title="Interface Status Distribution" icon="layers">
          <Donut
            segments={(["up", "down", "admin_down", "unknown"] as const).map((status) => ({
              key: status,
              label: STATUS_LABELS[status],
              value: summary[status],
              color: { up: "var(--green)", down: "var(--red)", admin_down: "var(--gray-status)", unknown: "var(--purple)" }[status],
            }))}
            centerValue={summary.total}
            centerLabel="interfaces"
            ariaLabel={`Interface status: ${summary.up} up, ${summary.down} down, ${summary.admin_down} admin down, ${summary.unknown} unknown`}
          />
        </WidgetCard>
      </div>

      <div className="filters-bar iface-toolbar">
        <FilterChips
          label="Filter interfaces"
          value={filter}
          onChange={(v) => setFilter(v as Filter)}
          options={[
            { value: "all", label: "All", count: counts.all },
            { value: "up", label: "Up", count: counts.up, status: "healthy" },
            { value: "down", label: "Down", count: counts.down, status: "down" },
            { value: "admin_down", label: "Admin Down", count: counts.admin_down, status: "disabled" },
            { value: "trunk", label: "Trunk", count: counts.trunk },
            { value: "access", label: "Access", count: counts.access },
            { value: "erroring", label: "Erroring", count: counts.erroring, status: "warning" },
          ]}
        />
        <input
          className="search-input"
          type="search"
          placeholder="Search interface, description or VLAN…"
          aria-label="Search interfaces by name, description or VLAN"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Interfaces</h2>
          <span className="count-tag">
            {rows.length === interfaces.length ? interfaces.length : `${rows.length} of ${interfaces.length}`}
          </span>
        </div>
        <div className="table-wrap no-max">
          <table className="data-table iface-table">
            <thead>
              <tr>
                {header("interface", "Interface")}
                <th>Description</th>
                <th>Admin</th>
                <th>Oper</th>
                <th>Speed</th>
                <th>VLAN / Mode</th>
                {header("rx", "RX Util", true)}
                {header("tx", "TX Util", true)}
                {header("errors", "Errors", true)}
                {header("crc", "CRC", true)}
                {header("discards", "Discards", true)}
                {header("last_change", "Last Change")}
                <th className="actions-cell">View</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => (
                <tr
                  key={i.id}
                  className={`clickable-row${selectedId === i.id ? " row-selected" : ""}${
                    i.status === "down" ? " row-alert" : i.erroring ? " row-warn" : ""
                  }`}
                  onClick={(e) => open(i, (e.currentTarget.querySelector(".iface-view") as HTMLElement) ?? null)}
                >
                  <td className="cell-primary">
                    <span className="mono">{i.name}</span>
                    {i.role && (
                      <span className="tag-plain iface-role" title={ROLE_LABELS[i.role].title}>
                        {ROLE_LABELS[i.role].label}
                      </span>
                    )}
                    {i.port_channel && <span className="cell-sub">member of {i.port_channel}</span>}
                  </td>
                  <td className="iface-desc" title={i.description ?? undefined}>
                    {i.description ?? <span className="muted">—</span>}
                  </td>
                  <td>{i.admin_status ? i.admin_status : <span className="muted">—</span>}</td>
                  <td>
                    <StatusBadge status={i.status} tone={STATUS_TONES[i.status]} label={STATUS_LABELS[i.status]} />
                    {i.erroring && <span className="tag-warn iface-erroring">erroring</span>}
                  </td>
                  <td className="cell-num">{formatSpeed(i.speed_bps)}</td>
                  <td className="nowrap">{vlanText(i)}</td>
                  <UtilCell value={i.rx_utilization_pct} />
                  <UtilCell value={i.tx_utilization_pct} />
                  <td className="cell-num">{formatCount(i.errors_delta)}</td>
                  <td className="cell-num">{formatCount(i.crc_delta)}</td>
                  <td className="cell-num">{formatCount(i.discards_delta)}</td>
                  <td className="nowrap">
                    <RelativeTime value={i.last_state_change} />
                  </td>
                  <td className="actions-cell">
                    <button
                      type="button"
                      className="secondary-button small-button iface-view"
                      aria-label={`View details of ${i.name}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        open(i, e.currentTarget);
                      }}
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={13}>
                    <EmptyState title="No interfaces match this filter." />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="table-footnote">
          Errors, CRC and discards are increases in the latest polling interval (not lifetime counters); “—” means no
          valid counter delta yet. Last change is device-reported where available, otherwise the poll that first saw the
          new link state.
        </div>
      </div>

      {selected && (
        <InterfaceDrawer
          deviceId={deviceId}
          item={selected}
          collectedAt={data.collected_at}
          stale={data.stale}
          pollSeconds={data.poll_interval_seconds}
          onClose={close}
        />
      )}
    </div>
  );
}

function UtilCell({ value }: { value: number | null }) {
  if (value === null) {
    return (
      <td className="cell-num" title="No valid counter delta yet (first sample, counter reset or unknown speed)">
        <span className="muted">—</span>
      </td>
    );
  }
  const level = utilLevel(value);
  return (
    <td className="cell-num">
      <span className={`metric-cell metric-${level}`}>
        <span className="metric-cell-bar" aria-hidden="true">
          <span style={{ width: `${Math.min(value, 100)}%` }} />
        </span>
        {formatUtil(value)}
      </span>
    </td>
  );
}
