import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getTopology, requestTopologyDiscovery } from "../api/client";
import type { HealthStatus, TopologyGraph as TopologyData, TopologyLink, TopologyNode } from "../api/types";
import { Banner, EmptyState, StaleDataWarning } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { KpiCard } from "../components/KpiCard";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { TopologyGraph, type GraphSelection, type LayoutName } from "../components/TopologyGraph";
import { useFleetHealth } from "../hooks/FleetHealthContext";
import { usePolling } from "../hooks/usePolling";
import { formatRelative, formatResponseTime } from "../utils/format";

const TOPOLOGY_POLL_MS = 60000;
const DISCOVER_COOLDOWN_MS = 45000;
// Beat discovers every 5 min; three missed runs means topology is not being refreshed.
const TOPOLOGY_STALE_AFTER_MS = 15 * 60 * 1000;
const ALL = "all";

type Node = TopologyNode & { effective_health: HealthStatus };

function platformLabel(platform: string | null): string {
  return platform ? platform.replace("dell_", "").toUpperCase() : "—";
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function NodeDetails({ node, graph, nodesById }: { node: Node; graph: TopologyData; nodesById: Map<string, Node> }) {
  const links = graph.links.filter((l) => l.source === node.id || l.target === node.id);

  if (!node.managed) {
    return (
      <>
        <div className="target-card">
          <div>
            <div className="target-host">{node.hostname}</div>
            <div className="target-sub">Seen via LLDP · not in the managed inventory</div>
          </div>
          <span className="tag tag-plain">Unmanaged</span>
        </div>
        <dl className="detail-list">
          <DetailRow label="Advertised name">{node.advertised_system_name ?? <span className="muted">not advertised</span>}</DetailRow>
          <DetailRow label="Management IP">{node.management_ip ? <span className="mono">{node.management_ip}</span> : <span className="muted">not advertised</span>}</DetailRow>
          <DetailRow label="Chassis ID">{node.chassis_id ? <span className="mono">{node.chassis_id}</span> : <span className="muted">—</span>}</DetailRow>
          <DetailRow label="Remote ports">
            {node.remote_port_ids?.length ? <span className="mono">{node.remote_port_ids.join(", ")}</span> : <span className="muted">—</span>}
          </DetailRow>
          <DetailRow label="Attached to">
            {(node.attached_device_ids ?? []).map((id) => nodesById.get(`device:${id}`)?.hostname ?? `Device #${id}`).join(", ") || "—"}
          </DetailRow>
          <DetailRow label="Last seen"><RelativeTime value={node.topology_last_seen_at} /></DetailRow>
        </dl>
        <p className="form-hint">Health is not monitored for unmanaged neighbors.</p>
      </>
    );
  }

  return (
    <>
      <div className="target-card">
        <div>
          <div className="target-host">{node.hostname}</div>
          <div className="target-sub mono">{node.management_ip}</div>
        </div>
        <StatusBadge status={node.effective_health} />
      </div>
      <dl className="detail-list">
        <DetailRow label="Platform"><span className="platform-tag">{node.platform}</span></DetailRow>
        <DetailRow label="Response time"><span className="mono">{formatResponseTime(node.response_time_ms)}</span></DetailRow>
        <DetailRow label="Last health check"><RelativeTime value={node.last_check_at ?? null} /></DetailRow>
        <DetailRow label="Last discovery"><RelativeTime value={node.topology_last_success_at ?? null} /></DetailRow>
        <DetailRow label="Active neighbors">{links.filter((l) => l.active).length}</DetailRow>
      </dl>

      {node.topology_last_error && (
        <Banner tone="warning" title="Last LLDP discovery failed.">
          <span className="mono">{node.topology_last_error}</span> Links shown are from the last successful discovery.
        </Banner>
      )}

      {links.length > 0 && (
        <div className="form-field">
          <span className="form-label">LLDP neighbors</span>
          <ul className="neighbor-list">
            {links.map((link) => {
              const local = link.source === node.id;
              const other = nodesById.get(local ? link.target : link.source);
              return (
                <li key={link.id}>
                  <span className="mono">{(local ? link.source_interface : link.target_interface) ?? "—"}</span>
                  <span className="muted"> → </span>
                  <strong>{other?.hostname ?? "?"}</strong>{" "}
                  <span className="mono secondary">
                    {(local ? link.target_interface ?? link.target_port_id : link.source_interface) ?? ""}
                  </span>
                  {!other?.managed && <span className="tag tag-plain">Unmanaged</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <div className="detail-links">
        <Link to="/devices">Devices</Link>
        <Link to="/jobs">Jobs</Link>
        <Link to="/backups">Backups</Link>
        <Link to="/audit">Audit</Link>
      </div>
    </>
  );
}

function EdgeDetails({ link, nodesById }: { link: TopologyLink; nodesById: Map<string, Node> }) {
  const source = nodesById.get(link.source);
  const target = nodesById.get(link.target);

  return (
    <>
      <dl className="detail-list">
        <DetailRow label="Source"><strong>{source?.hostname ?? link.source}</strong></DetailRow>
        <DetailRow label="Source port"><span className="mono">{link.source_interface ?? "—"}</span></DetailRow>
        <DetailRow label="Target">
          <strong>{target?.hostname ?? link.target}</strong>
          {!target?.managed && <span className="cell-sub">Advertised identity (unmanaged)</span>}
        </DetailRow>
        <DetailRow label="Target port">
          <span className="mono">{link.target_interface ?? link.target_port_id ?? "—"}</span>
          {!link.target_interface && link.target_port_id && <span className="cell-sub">LLDP Port ID (not an interface name)</span>}
        </DetailRow>
        <DetailRow label="Protocol">LLDP</DetailRow>
        <DetailRow label="Relationship">{link.relationship === "managed" ? "Managed ↔ managed" : "Managed → unmanaged"}</DetailRow>
        <DetailRow label="Observation">
          {link.observed_bidirectionally ? (
            <StatusBadge status="confirmed" label="Confirmed from both ends" tone="success" />
          ) : (
            <StatusBadge status="one-side" label="Observed from one side" tone="warning" />
          )}
        </DetailRow>
        <DetailRow label="Active">{link.active ? "Yes" : "No (not seen in latest discovery)"}</DetailRow>
        <DetailRow label="Last seen"><RelativeTime value={link.last_seen_at} /></DetailRow>
        <DetailRow label="First seen"><RelativeTime value={link.first_seen_at} /></DetailRow>
      </dl>
    </>
  );
}

export function Topology() {
  const { fleet } = useFleetHealth();
  const [graph, setGraph] = useState<TopologyData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const [selection, setSelection] = useState<GraphSelection>(null);
  const [layout, setLayout] = useState<LayoutName>("breadthfirst");
  const [fitSignal, setFitSignal] = useState(0);
  const [resetSignal, setResetSignal] = useState(0);
  const [search, setSearch] = useState("");
  const [healthFilter, setHealthFilter] = useState(ALL);
  const [managedFilter, setManagedFilter] = useState(ALL);
  const [platformFilter, setPlatformFilter] = useState(ALL);
  const [discoverBusy, setDiscoverBusy] = useState(false);
  const [discoverMessage, setDiscoverMessage] = useState<string | null>(null);

  // Reads stored topology only; discovery runs via Beat (5 min) or Discover now.
  const load = useCallback(async () => {
    try {
      setGraph(await getTopology());
      setError(null);
      setLastLoadedAt(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load topology");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  usePolling(load, TOPOLOGY_POLL_MS);

  // Health comes from the shared 20 s fleet poll when available (fresher, no extra request).
  const fleetHealth = useMemo(() => {
    const map = new Map<number, HealthStatus>();
    fleet?.devices.forEach((d) => map.set(d.device_id, d.status));
    return map;
  }, [fleet]);

  const nodes: Node[] = useMemo(
    () =>
      (graph?.nodes ?? []).map((node) => ({
        ...node,
        effective_health: node.managed
          ? (node.device_id !== null && fleetHealth.get(node.device_id)) || node.health_status
          : "unknown",
      })),
    [graph, fleetHealth],
  );
  const links = useMemo(() => graph?.links ?? [], [graph]);
  const nodesById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  const visibleNodeIds = useMemo(() => {
    const query = search.trim().toLowerCase();
    return new Set(
      nodes
        .filter((n) => {
          // Health applies to managed devices only; unmanaged neighbors have no health state.
          if (healthFilter !== ALL && (!n.managed || n.effective_health !== healthFilter)) return false;
          if (managedFilter === "managed" && !n.managed) return false;
          if (managedFilter === "unmanaged" && n.managed) return false;
          if (platformFilter !== ALL && n.platform !== platformFilter) return false;
          if (query && !`${n.hostname} ${n.management_ip ?? ""}`.toLowerCase().includes(query)) return false;
          return true;
        })
        .map((n) => n.id),
    );
  }, [nodes, search, healthFilter, managedFilter, platformFilter]);

  async function handleDiscover() {
    setDiscoverBusy(true);
    try {
      await requestTopologyDiscovery();
      setDiscoverMessage("Discovery requested. The map refreshes when it completes.");
      window.setTimeout(load, 5000);
    } catch (err) {
      setDiscoverMessage(err instanceof Error ? err.message : "Discovery request failed");
    }
    window.setTimeout(() => setDiscoverBusy(false), DISCOVER_COOLDOWN_MS);
  }

  const selectedNode = selection?.kind === "node" ? nodesById.get(selection.id) : undefined;
  const selectedLink = selection?.kind === "edge" ? links.find((l) => l.id === selection.id) : undefined;

  const neverDiscovered = graph !== null && !graph.last_discovery_at;
  const stale =
    graph?.last_discovery_at !== undefined &&
    graph?.last_discovery_at !== null &&
    Date.now() - new Date(graph.last_discovery_at).getTime() > TOPOLOGY_STALE_AFTER_MS;
  const failingNodes = nodes.filter((n) => n.managed && n.topology_last_error);
  const downNodes = nodes.filter((n) => n.managed && n.effective_health === "down");
  const counts = (predicate: (n: Node) => boolean) => nodes.filter(predicate).length;

  return (
    <>
      <PageHeader
        title="Topology"
        subtitle="Live LLDP-discovered network relationships (read-only)."
        lastUpdated={lastLoadedAt}
        actions={
          <>
            <select
              className="filter-select"
              aria-label="Layout"
              value={layout}
              onChange={(event) => {
                setLayout(event.target.value as LayoutName);
                setResetSignal((n) => n + 1);
              }}
              style={{ minWidth: 130 }}
            >
              <option value="breadthfirst">Hierarchical</option>
              <option value="cose">Force-directed</option>
            </select>
            <button type="button" className="secondary-button" onClick={() => setFitSignal((n) => n + 1)}>
              Fit
            </button>
            <button type="button" className="secondary-button" onClick={() => setResetSignal((n) => n + 1)}>
              Reset view
            </button>
            <button
              type="button"
              className="primary-button"
              onClick={handleDiscover}
              disabled={discoverBusy || graph?.discovery_running}
              title="Queue one read-only LLDP discovery of all enabled switches"
            >
              {graph?.discovery_running ? "Discovery running…" : "Discover now"}
            </button>
          </>
        }
      />

      {error && lastLoadedAt && <StaleDataWarning since={lastLoadedAt} error={error} />}
      {error && !lastLoadedAt && <Banner tone="danger" title="Failed to load topology.">{error}</Banner>}
      {discoverMessage && <Banner tone="info" onDismiss={() => setDiscoverMessage(null)}>{discoverMessage}</Banner>}
      {neverDiscovered && (
        <Banner tone="info" title="No topology discovered yet.">
          Discovery runs every 5 minutes, or use Discover now. Only LLDP-reported links are shown.
        </Banner>
      )}
      {stale && graph?.last_discovery_at && (
        <Banner tone="warning" title="Topology may be stale.">
          The last successful discovery was {formatRelative(graph.last_discovery_at)}.
        </Banner>
      )}
      {failingNodes.length > 0 && (
        <Banner tone="warning" title={`LLDP discovery failing on ${failingNodes.length} device(s).`}>
          {failingNodes.map((n) => n.hostname).join(", ")} — their last known links are kept.
        </Banner>
      )}
      {downNodes.length > 0 && (
        <Banner tone="danger" title="Topology may be stale for down devices.">
          {downNodes.map((n) => n.hostname).join(", ")} {downNodes.length === 1 ? "is" : "are"} down; links shown are from the last discovery.
        </Banner>
      )}

      <div className="kpi-grid">
        <KpiCard label="Managed Devices" value={graph ? graph.managed_devices : "—"} tone="info" />
        <KpiCard label="Active Links" value={graph ? graph.active_links : "—"} tone="success" />
        <KpiCard label="Unmanaged Neighbors" value={graph ? graph.unmanaged_neighbors : "—"} tone="neutral" />
        <KpiCard label="Down Devices" value={graph ? downNodes.length : "—"} tone="danger" emphasize={downNodes.length > 0} />
        <KpiCard
          label="Last Discovery"
          value={graph?.last_discovery_at ? formatRelative(graph.last_discovery_at) : "—"}
          tone={stale ? "warning" : "info"}
          hint={graph?.discovery_running ? "Discovery running…" : graph?.last_run ? `${graph.last_run.successful}/${graph.last_run.checked} devices OK` : undefined}
        />
      </div>

      <div className="filters-bar">
        <input
          type="search"
          className="search-input"
          placeholder="Search hostname or IP…"
          aria-label="Search topology"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <FilterChips
          label="Health"
          value={healthFilter}
          onChange={setHealthFilter}
          options={[
            { value: ALL, label: "All", count: nodes.length },
            ...(["healthy", "degraded", "down", "unknown"] as HealthStatus[]).map((s) => ({
              value: s,
              label: s.charAt(0).toUpperCase() + s.slice(1),
              count: counts((n) => n.managed && n.effective_health === s),
              status: s,
            })),
          ]}
        />
        <FilterChips
          label="Management"
          value={managedFilter}
          onChange={setManagedFilter}
          options={[
            { value: ALL, label: "All" },
            { value: "managed", label: "Managed", count: counts((n) => n.managed) },
            { value: "unmanaged", label: "Unmanaged", count: counts((n) => !n.managed) },
          ]}
        />
        <FilterChips
          label="Platform"
          value={platformFilter}
          onChange={setPlatformFilter}
          options={[
            { value: ALL, label: "All" },
            { value: "dell_os6", label: "OS6", count: counts((n) => n.platform === "dell_os6") },
            { value: "dell_os10", label: "OS10", count: counts((n) => n.platform === "dell_os10") },
          ]}
        />
      </div>

      <div className={`topology-layout${selection ? " with-drawer" : ""}`}>
        <div className="panel topology-panel">
          <div className="panel-header">
            <h2>Network Map</h2>
            <span className="panel-header-meta">
              {visibleNodeIds.size}/{nodes.length} nodes · {links.length} links
            </span>
          </div>
          {graph && nodes.length === 0 ? (
            <EmptyState title="No devices to display." />
          ) : (
            <TopologyGraph
              nodes={nodes}
              links={links}
              visibleNodeIds={visibleNodeIds}
              selection={selection}
              onSelect={setSelection}
              layout={layout}
              fitSignal={fitSignal}
              resetSignal={resetSignal}
            />
          )}
          <div className="topology-legend" aria-label="Legend">
            <span><span className="tone-dot dot-healthy" /> Healthy</span>
            <span><span className="tone-dot dot-degraded" /> Degraded</span>
            <span><span className="tone-dot dot-down" /> Down</span>
            <span><span className="tone-dot dot-unknown" /> Unknown</span>
            <span><span className="legend-node dashed" /> Unmanaged neighbor</span>
            <span><span className="legend-line" /> Confirmed both ends</span>
            <span><span className="legend-line dashed" /> One side only</span>
            <span><span className="legend-line dotted" /> To unmanaged</span>
          </div>
        </div>

        {selection && (
          <aside className="panel topology-drawer" aria-label="Selection details">
            <div className="panel-header">
              <h2>{selectedNode ? (selectedNode.managed ? "Device" : "Unmanaged neighbor") : "Link"}</h2>
              <button type="button" className="copy-button" onClick={() => setSelection(null)}>
                Close
              </button>
            </div>
            <div className="modal-body">
              {selectedNode && graph && <NodeDetails node={selectedNode} graph={graph} nodesById={nodesById} />}
              {selectedLink && <EdgeDetails link={selectedLink} nodesById={nodesById} />}
              {!selectedNode && !selectedLink && <EmptyState title="Selection no longer present." />}
              <p className="form-hint">
                Platform: {selectedNode ? platformLabel(selectedNode.platform) : "LLDP link"} · read-only view
              </p>
            </div>
          </aside>
        )}
      </div>
    </>
  );
}

export default Topology;
