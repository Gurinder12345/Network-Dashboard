import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getTopology, requestTopologyDiscovery } from "../api/client";
import type { HealthStatus, TopologyGraph as TopologyData, TopologyLink } from "../api/types";
import { Banner, EmptyState, StaleDataWarning } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { KpiCard } from "../components/KpiCard";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { TopologyGraph, type GraphSelection, type LayoutName } from "../components/TopologyGraph";
import { useFleetHealth } from "../hooks/FleetHealthContext";
import { usePolling } from "../hooks/usePolling";
import {
  buildDisplayModel,
  computeVisibility,
  groupIdFor,
  type DisplayModel,
  type DisplayNode,
  type Filters,
  type NodeCategory,
  type TypeFilter,
  type ViewMode,
} from "../topology/viewModel";
import { formatRelative, formatResponseTime } from "../utils/format";

const TOPOLOGY_POLL_MS = 60000;
const DISCOVER_COOLDOWN_MS = 45000;
// Beat discovers every 5 min; three missed runs means topology is not being refreshed.
const TOPOLOGY_STALE_AFTER_MS = 15 * 60 * 1000;
const ALL = "all";
const DEFAULT_LAYOUT: Record<ViewMode, LayoutName> = { infrastructure: "breadthfirst", all: "cose" };

const CATEGORY_LABEL: Record<NodeCategory, string> = {
  managed_network: "Managed network",
  unmanaged_network: "Unmanaged network",
  endpoint: "Endpoints",
  unknown: "Unclassified",
};

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function neighborsOf(node: DisplayNode, model: DisplayModel) {
  return model.edges
    .filter((e) => !e.isGroupEdge && (e.source === node.id || e.target === node.id))
    .map((link) => {
      const local = link.source === node.id;
      const other = model.byId.get(local ? link.target : link.source)!;
      return {
        link,
        other,
        localIf: local ? link.source_interface : link.target_interface,
        remoteIf: local ? link.target_interface ?? link.target_port_id : link.source_interface,
      };
    });
}

function ManagedDetails({
  node,
  model,
  expanded,
  onToggleGroup,
}: {
  node: DisplayNode;
  model: DisplayModel;
  expanded: boolean;
  onToggleGroup: () => void;
}) {
  const neighbors = neighborsOf(node, model);
  const grouped = (["managed_network", "unmanaged_network", "endpoint", "unknown"] as NodeCategory[]).map((category) => ({
    category,
    items: neighbors.filter((n) => n.other.category === category),
  }));
  const count = (c: NodeCategory) => grouped.find((g) => g.category === c)!.items.length;
  const collapsible = model.collapsible.get(node.id)?.length ?? 0;

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
        <DetailRow label="Managed neighbors">{count("managed_network")}</DetailRow>
        <DetailRow label="Unmanaged network">{count("unmanaged_network")}</DetailRow>
        <DetailRow label="Endpoints">{count("endpoint")}{count("unknown") ? ` (+${count("unknown")} unclassified)` : ""}</DetailRow>
      </dl>

      {node.topology_last_error && (
        <Banner tone="warning" title="Last LLDP discovery failed.">
          <span className="mono">{node.topology_last_error}</span> Links shown are from the last successful discovery.
        </Banner>
      )}
      {node.effective_health === "down" && (
        <Banner tone="danger" title="Device is down.">
          Topology for this device may be stale.
        </Banner>
      )}

      {collapsible > 0 && (
        <button type="button" className="secondary-button small-button" onClick={onToggleGroup} aria-pressed={expanded}>
          {expanded ? "Collapse" : "Show"} {collapsible} LLDP neighbors on map
        </button>
      )}

      {grouped
        .filter((g) => g.items.length > 0)
        .map((g) => (
          <div className="form-field" key={g.category}>
            <span className="form-label">
              {CATEGORY_LABEL[g.category]} ({g.items.length})
            </span>
            <ul className="neighbor-list">
              {g.items.map(({ link, other, localIf, remoteIf }) => (
                <li key={link.id} title={other.chassis_id ?? undefined}>
                  <span className="mono">{localIf ?? "—"}</span>
                  <span className="muted"> → </span>
                  <strong>{other.hostname}</strong> <span className="mono secondary">{remoteIf ?? ""}</span>
                  {!link.observed_bidirectionally && other.managed && <span className="tag tag-plain">one side</span>}
                </li>
              ))}
            </ul>
          </div>
        ))}

      <div className="detail-links">
        <Link to="/devices">Devices</Link>
        <Link to="/jobs">Jobs</Link>
        <Link to="/backups">Backups</Link>
        <Link to="/audit">Audit</Link>
      </div>
    </>
  );
}

function UnmanagedDetails({ node, model }: { node: DisplayNode; model: DisplayModel }) {
  const neighbors = neighborsOf(node, model);
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
        <DetailRow label="Display type">
          {CATEGORY_LABEL[node.category]} <span className="muted">(presentation only)</span>
        </DetailRow>
        <DetailRow label="Advertised name">{node.advertised_system_name ?? <span className="muted">not advertised</span>}</DetailRow>
        <DetailRow label="Management IP">{node.management_ip ? <span className="mono">{node.management_ip}</span> : <span className="muted">not advertised</span>}</DetailRow>
        <DetailRow label="Chassis ID">{node.chassis_id ? <span className="mono">{node.chassis_id}</span> : <span className="muted">—</span>}</DetailRow>
        <DetailRow label="Remote ports">
          {node.remote_port_ids?.length ? <span className="mono">{node.remote_port_ids.join(", ")}</span> : <span className="muted">—</span>}
        </DetailRow>
        <DetailRow label="Last seen"><RelativeTime value={node.topology_last_seen_at} /></DetailRow>
      </dl>
      <div className="form-field">
        <span className="form-label">Attached to</span>
        <ul className="neighbor-list">
          {neighbors.map(({ link, other, localIf, remoteIf }) => (
            <li key={link.id}>
              <strong>{other.hostname}</strong> <span className="mono">{remoteIf ?? "—"}</span>
              <span className="muted"> → </span>
              <span className="mono secondary">{localIf ?? node.remote_port_ids?.join(", ") ?? ""}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="form-hint">Health is not monitored for unmanaged neighbors.</p>
    </>
  );
}

function EdgeDetails({ link, model }: { link: TopologyLink; model: DisplayModel }) {
  const source = model.byId.get(link.source);
  const target = model.byId.get(link.target);

  return (
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
  );
}

export function Topology() {
  const { fleet } = useFleetHealth();
  const [graph, setGraph] = useState<TopologyData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const [selection, setSelection] = useState<GraphSelection>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("infrastructure");
  const [layout, setLayout] = useState<LayoutName>(DEFAULT_LAYOUT.infrastructure);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [fitSignal, setFitSignal] = useState(0);
  const [resetCount, setResetCount] = useState(0);
  const [filters, setFilters] = useState<Filters>({ search: "", health: ALL, management: ALL, type: ALL, platform: ALL });
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

  // Health from the shared 20 s fleet poll when available (fresher, no extra request).
  const fleetHealth = useMemo(() => {
    const map = new Map<number, HealthStatus>();
    fleet?.devices.forEach((d) => map.set(d.device_id, d.status));
    return map;
  }, [fleet]);

  const model = useMemo(
    () =>
      buildDisplayModel(
        (graph?.nodes ?? []).map((node) => ({
          ...node,
          effective_health: node.managed
            ? (node.device_id !== null && fleetHealth.get(node.device_id)) || node.health_status
            : "unknown",
        })),
        graph?.links ?? [],
      ),
    [graph, fleetHealth],
  );

  const visibility = useMemo(() => computeVisibility(model, viewMode, expanded, filters), [model, viewMode, expanded, filters]);

  const toggleGroup = useCallback((groupId: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  }, []);

  const setFilter = (key: keyof Filters) => (value: string) => setFilters((f) => ({ ...f, [key]: value }));

  const tooltipFor = useCallback(
    (kind: "node" | "edge", id: string): string[] => {
      if (kind === "edge") {
        const e = model.edges.find((x) => x.id === id);
        if (!e || e.isGroupEdge) return [];
        return [
          `${model.byId.get(e.source)?.hostname} ${e.source_interface ?? ""} ↔ ${model.byId.get(e.target)?.hostname} ${e.target_interface ?? e.target_port_id ?? ""}`,
          e.observed_bidirectionally ? "Confirmed from both ends" : "Observed from one side",
          `Last seen ${formatRelative(e.last_seen_at)}`,
        ];
      }
      const n = model.byId.get(id);
      if (!n) return [];
      if (n.isGroup) {
        const owner = model.byId.get(n.groupOwner!)!;
        const open = expanded.has(n.id);
        return [`${visibility.groupCounts.get(n.id) ?? 0} LLDP neighbors on ${owner.hostname}`, open ? "Click to collapse" : "Click to expand"];
      }
      if (n.managed) {
        return [
          n.hostname,
          `${n.management_ip ?? ""} · ${n.platform ?? ""}`,
          `Health: ${n.effective_health}`,
          `${n.neighbor_count} LLDP neighbors (${model.collapsible.get(n.id)?.length ?? 0} endpoint/unclassified)`,
        ];
      }
      const attached = (n.attached_device_ids ?? []).map((d) => model.byId.get(`device:${d}`)?.hostname).filter(Boolean).join(", ");
      return [
        n.advertised_system_name ?? n.hostname,
        `${CATEGORY_LABEL[n.category]} · unmanaged`,
        `Chassis ${n.chassis_id ?? "—"}`,
        `On ${attached || "—"} · port ${n.remote_port_ids?.join(", ") || "—"}`,
      ];
    },
    [model, expanded, visibility],
  );

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

  function changeView(mode: ViewMode) {
    setViewMode(mode);
    setLayout(DEFAULT_LAYOUT[mode]);
    setSelection(null);
  }

  const selectedNode = selection?.kind === "node" ? model.byId.get(selection.id) : undefined;
  const selectedLink = selection?.kind === "edge" ? model.edges.find((l) => l.id === selection.id) : undefined;

  const real = model.nodes.filter((n) => !n.isGroup);
  const managedNodes = real.filter((n) => n.managed);
  const downNodes = managedNodes.filter((n) => n.effective_health === "down");
  const failingNodes = managedNodes.filter((n) => n.topology_last_error);
  const visibleReal = real.filter((n) => visibility.visible.has(n.id));
  const visibleLinks = model.edges.filter(
    (e) => !e.isGroupEdge && visibility.visible.has(e.source) && visibility.visible.has(e.target),
  ).length;
  const count = (predicate: (n: DisplayNode) => boolean) => real.filter(predicate).length;

  const neverDiscovered = graph !== null && !graph.last_discovery_at;
  const stale =
    !!graph?.last_discovery_at && Date.now() - new Date(graph.last_discovery_at).getTime() > TOPOLOGY_STALE_AFTER_MS;
  const allGroupIds = [...model.collapsible.keys()].map(groupIdFor);
  const allExpanded = allGroupIds.length > 0 && allGroupIds.every((id) => expanded.has(id));

  const summary =
    viewMode === "infrastructure"
      ? `${visibleReal.filter((n) => n.managed).length} managed · ${visibleReal.filter((n) => n.category === "unmanaged_network").length} unmanaged network · ${visibility.hiddenEndpoints} endpoints collapsed · ${visibleLinks} links`
      : `${visibleReal.length}/${real.length} nodes · ${visibleLinks} links`;

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
              onChange={(event) => setLayout(event.target.value as LayoutName)}
              style={{ minWidth: 130 }}
            >
              <option value="breadthfirst">Hierarchical</option>
              <option value="cose">Force-directed</option>
            </select>
            <button type="button" className="secondary-button" onClick={() => setFitSignal((n) => n + 1)}>
              Fit
            </button>
            <button type="button" className="secondary-button" onClick={() => setResetCount((n) => n + 1)}>
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
      {graph?.discovery_running && graph.links.length === 0 && (
        <Banner tone="info" title="Topology discovery is running.">
          Managed inventory is shown while LLDP relationships are collected.
        </Banner>
      )}
      {neverDiscovered && !graph?.discovery_running && (
        <Banner tone="info" title="No topology discovered yet.">
          Managed inventory is shown. Discovery runs every 5 minutes, or use Discover now.
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

      <div className="kpi-grid">
        <KpiCard label="Managed Devices" value={graph ? managedNodes.length : "—"} tone="info" />
        <KpiCard label="Active Links" value={graph ? graph.active_links : "—"} tone="success" />
        <KpiCard
          label="Unmanaged Neighbors"
          value={graph ? graph.unmanaged_neighbors : "—"}
          tone="neutral"
          hint={graph ? `${count((n) => n.category === "unmanaged_network")} network · ${count((n) => n.category === "endpoint")} endpoints` : undefined}
        />
        <KpiCard label="Down Devices" value={graph ? downNodes.length : "—"} tone="danger" emphasize={downNodes.length > 0} />
        <KpiCard
          label="Last Discovery"
          value={graph?.last_discovery_at ? formatRelative(graph.last_discovery_at) : "—"}
          tone={stale ? "warning" : "info"}
          hint={graph?.discovery_running ? "Discovery running…" : graph?.last_run ? `${graph.last_run.successful}/${graph.last_run.checked} devices OK` : undefined}
        />
      </div>

      <div className="filters-bar">
        <FilterChips
          label="View"
          value={viewMode}
          onChange={(v) => changeView(v as ViewMode)}
          options={[
            { value: "infrastructure", label: "Infrastructure" },
            { value: "all", label: "All LLDP", count: real.length },
          ]}
        />
        <input
          type="search"
          className="search-input"
          placeholder="Search hostname, advertised name or IP…"
          aria-label="Search topology"
          value={filters.search}
          onChange={(event) => setFilter("search")(event.target.value)}
        />
        {viewMode === "infrastructure" && allGroupIds.length > 0 && (
          <button
            type="button"
            className="secondary-button small-button"
            aria-pressed={allExpanded}
            onClick={() => setExpanded(allExpanded ? new Set() : new Set(allGroupIds))}
          >
            {allExpanded ? "Collapse all neighbors" : "Expand all neighbors"}
          </button>
        )}
      </div>
      <div className="filters-bar">
        <FilterChips
          label="Health"
          value={filters.health}
          onChange={setFilter("health")}
          options={[
            { value: ALL, label: "All" },
            ...(["healthy", "degraded", "down", "unknown"] as HealthStatus[]).map((s) => ({
              value: s,
              label: s.charAt(0).toUpperCase() + s.slice(1),
              count: count((n) => n.managed && n.effective_health === s),
              status: s,
            })),
          ]}
        />
        <FilterChips
          label="Management"
          value={filters.management}
          onChange={setFilter("management")}
          options={[
            { value: ALL, label: "All" },
            { value: "managed", label: "Managed", count: managedNodes.length },
            { value: "unmanaged", label: "Unmanaged", count: real.length - managedNodes.length },
          ]}
        />
        <FilterChips
          label="Type"
          value={filters.type}
          onChange={(v) => setFilter("type")(v as TypeFilter)}
          options={[
            { value: ALL, label: "All" },
            { value: "network", label: "Network", count: count((n) => n.category === "managed_network" || n.category === "unmanaged_network") },
            { value: "endpoint", label: "Endpoints", count: count((n) => n.category === "endpoint") },
            { value: "unknown", label: "Unclassified", count: count((n) => n.category === "unknown") },
          ]}
        />
        <FilterChips
          label="Platform"
          value={filters.platform}
          onChange={setFilter("platform")}
          options={[
            { value: ALL, label: "All" },
            { value: "dell_os6", label: "OS6", count: count((n) => n.platform === "dell_os6") },
            { value: "dell_os10", label: "OS10", count: count((n) => n.platform === "dell_os10") },
          ]}
        />
      </div>

      <div className={`topology-layout${selection ? " with-drawer" : ""}`}>
        <div className="panel topology-panel">
          <div className="panel-header">
            <h2>Network Map</h2>
            <span className="panel-header-meta">{summary}</span>
          </div>
          {graph && real.length === 0 ? (
            <EmptyState title="No devices to display." />
          ) : (
            <TopologyGraph
              nodes={model.nodes}
              edges={model.edges}
              visibleIds={visibility.visible}
              groupCounts={visibility.groupCounts}
              selection={selection}
              onSelect={setSelection}
              onToggleGroup={toggleGroup}
              tooltipFor={tooltipFor}
              layout={layout}
              layoutKey={`${viewMode}|${layout}|${resetCount}`}
              fitSignal={fitSignal}
            />
          )}
          <div className="topology-legend" aria-label="Legend">
            <span><span className="tone-dot dot-healthy" /> Healthy</span>
            <span><span className="tone-dot dot-degraded" /> Degraded</span>
            <span><span className="tone-dot dot-down" /> Down</span>
            <span><span className="tone-dot dot-unknown" /> Unknown</span>
            <span><span className="legend-node dashed" /> Unmanaged</span>
            <span><span className="legend-node group" /> Collapsed neighbors (click)</span>
            <span><span className="legend-line" /> Both ends</span>
            <span><span className="legend-line dashed" /> One side</span>
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
              {selectedNode?.managed && (
                <ManagedDetails
                  node={selectedNode}
                  model={model}
                  expanded={expanded.has(groupIdFor(selectedNode.id))}
                  onToggleGroup={() => {
                    if (viewMode !== "infrastructure") changeView("infrastructure");
                    toggleGroup(groupIdFor(selectedNode.id));
                  }}
                />
              )}
              {selectedNode && !selectedNode.managed && <UnmanagedDetails node={selectedNode} model={model} />}
              {selectedLink && <EdgeDetails link={selectedLink} model={model} />}
              {!selectedNode && !selectedLink && <EmptyState title="Selection no longer present." />}
            </div>
          </aside>
        )}
      </div>
    </>
  );
}

export default Topology;
