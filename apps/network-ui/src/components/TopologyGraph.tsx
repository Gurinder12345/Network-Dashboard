import { useEffect, useRef, useState } from "react";
import cytoscape, { type Core, type ElementDefinition, type LayoutOptions, type NodeSingular } from "cytoscape";
import type { HealthStatus } from "../api/types";
import type { DisplayEdge, DisplayNode } from "../topology/viewModel";

export type GraphSelection = { kind: "node" | "edge"; id: string } | null;
export type LayoutName = "breadthfirst" | "cose";

interface TopologyGraphProps {
  nodes: DisplayNode[];
  edges: DisplayEdge[];
  visibleIds: Set<string>;
  groupCounts: Map<string, number>;
  selection: GraphSelection;
  onSelect: (selection: GraphSelection) => void;
  onToggleGroup: (groupId: string) => void;
  tooltipFor: (kind: "node" | "edge", id: string) => string[];
  layout: LayoutName;
  /** Changes when a relayout is wanted (view mode, layout choice, Reset). */
  layoutKey: string;
  fitSignal: number;
}

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

const HEALTH_VAR: Record<HealthStatus, [string, string]> = {
  healthy: ["--tone-success", "#34c77b"],
  degraded: ["--tone-warning", "#e3ac37"],
  down: ["--tone-danger", "#e5534b"],
  unknown: ["--tone-neutral", "#7d899e"],
};

const truncate = (value: string, max: number) => (value.length > max ? `${value.slice(0, max - 1)}…` : value);

function nodeLabel(node: DisplayNode, count?: number): string {
  if (node.isGroup) return `+${count ?? node.groupMembers?.length ?? 0} LLDP neighbors`;
  if (node.managed) {
    const platform = node.platform ? node.platform.replace("dell_", "").toUpperCase() : "";
    // Non-healthy states are spelled out so status never relies on color alone.
    const state = node.effective_health === "healthy" ? "" : ` · ${node.effective_health.toUpperCase()}`;
    return `${node.hostname}\n${platform}${state}`;
  }
  if (node.category === "unmanaged_network") return `${truncate(node.hostname, 22)}\nUnmanaged · network`;
  return truncate(node.hostname, 18);
}

function nodeSize(node: DisplayNode): string {
  if (node.isGroup) return "group";
  if (node.managed) return node.platform === "dell_os10" ? "core" : "managed";
  return node.category === "unmanaged_network" ? "network" : "endpoint";
}

// Explicit node dimensions from the label text (no label measurement). Cytoscape caches
// visibility using the node's width; with width:"label" a node that starts hidden has an
// unmeasured (zero) width and stays invisible after being shown. Sizes also enforce the
// hierarchy: core > managed > unmanaged network > endpoint.
const SIZE_METRICS: Record<string, { char: number; pad: number; min: number; height: number }> = {
  core: { char: 8.6, pad: 40, min: 176, height: 66 },
  managed: { char: 7.1, pad: 28, min: 128, height: 46 },
  network: { char: 6.4, pad: 22, min: 96, height: 40 },
  endpoint: { char: 5.5, pad: 14, min: 40, height: 22 },
  group: { char: 5.9, pad: 26, min: 60, height: 26 },
};

function nodeDimensions(label: string, size: string): { w: number; h: number } {
  const m = SIZE_METRICS[size] ?? SIZE_METRICS.managed;
  const longest = Math.max(...label.split("\n").map((line) => line.length));
  return { w: Math.round(Math.max(m.min, longest * m.char + m.pad)), h: m.height };
}

function nodeElement(node: DisplayNode, count?: number): ElementDefinition {
  const label = nodeLabel(node, count);
  const size = nodeSize(node);
  const { w, h } = nodeDimensions(label, size);
  return {
    group: "nodes",
    data: {
      id: node.id,
      label,
      health: node.managed ? node.effective_health : "unmanaged",
      size,
      category: node.category,
      w,
      h,
    },
  };
}

function edgeElement(edge: DisplayEdge): ElementDefinition {
  return {
    group: "edges",
    data: {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceLabel: edge.source_interface ?? "",
      targetLabel: edge.target_interface ?? edge.target_port_id ?? "",
      observed: edge.observed_bidirectionally ? "both" : "one",
      relationship: edge.isGroupEdge ? "group" : edge.relationship,
      active: edge.active ? "yes" : "no",
    },
  };
}

function buildStyle(): cytoscape.StylesheetJson {
  const text = cssVar("--text-primary", "#e6ebf3");
  const muted = cssVar("--text-secondary", "#9ba6b9");
  const faint = cssVar("--text-muted", "#6d798f");
  const panel = cssVar("--bg-panel-raised", "#161e2b");
  const inset = cssVar("--bg-inset", "#0b1019");
  const edge = cssVar("--border-strong", "#34415a");
  const accent = cssVar("--accent", "#3b9eff");
  const neutral = cssVar("--tone-neutral", "#7d899e");
  const font = cssVar("--font-sans", "sans-serif");

  const healthRules = (Object.keys(HEALTH_VAR) as HealthStatus[]).map((status) => ({
    selector: `node[health = "${status}"]`,
    style: { "border-color": cssVar(...HEALTH_VAR[status]) },
  }));

  return [
    {
      selector: "node",
      style: {
        shape: "round-rectangle",
        width: "data(w)",
        height: "data(h)",
        "background-color": panel,
        "border-width": 2,
        label: "data(label)",
        "text-wrap": "wrap",
        "text-valign": "center",
        "text-halign": "center",
        color: text,
        "font-family": font,
        "line-height": 1.35,
      },
    },
    // Size hierarchy: core > managed > unmanaged network > endpoint / group.
    { selector: 'node[size = "core"]', style: { "font-size": 14, "font-weight": 700, "border-width": 3 } },
    { selector: 'node[size = "managed"]', style: { "font-size": 11.5, "font-weight": 600, "border-width": 2.5 } },
    {
      selector: 'node[size = "network"]',
      style: {
        "font-size": 10.5,
        "border-style": "dashed",
        "border-color": neutral,
        "background-color": inset,
        color: muted,
      },
    },
    {
      selector: 'node[size = "endpoint"]',
      style: {
        "font-size": 9,
        "border-width": 1,
        "border-style": "dashed",
        "border-color": neutral,
        "background-color": inset,
        color: faint,
      },
    },
    {
      selector: 'node[size = "group"]',
      style: {
        shape: "round-tag",
        "font-size": 9.5,
        "border-width": 1,
        "border-style": "dotted",
        "border-color": neutral,
        "background-color": inset,
        color: muted,
      },
    },
    ...healthRules,
    { selector: 'node[health = "down"]', style: { "background-color": "#2a1618" } },
    {
      selector: "edge",
      style: {
        width: 2,
        "curve-style": "bezier",
        "line-color": edge,
        "font-family": font,
        "font-size": 9.5,
        color: muted,
        "text-background-color": inset,
        "text-background-opacity": 0.92,
        "text-background-padding": "2px",
        "source-text-offset": 46,
        "target-text-offset": 46,
      },
    },
    { selector: 'edge[observed = "one"]', style: { "line-style": "dashed" } },
    { selector: 'edge[relationship = "unmanaged"]', style: { "line-style": "dotted", width: 1.5 } },
    { selector: 'edge[relationship = "group"]', style: { "line-style": "dotted", width: 1, opacity: 0.6 } },
    { selector: 'edge[active = "no"]', style: { opacity: 0.35 } },
    { selector: ".hidden", style: { display: "none" } },
    { selector: ".dimmed", style: { opacity: 0.22 } },
    // Interface labels only on hover / selection -- never permanently.
    {
      selector: "edge.show-ports",
      style: { "source-label": "data(sourceLabel)", "target-label": "data(targetLabel)", "line-color": accent },
    },
    { selector: "node:selected", style: { "overlay-color": accent, "overlay-opacity": 0.18, "overlay-padding": 6, "border-width": 4 } },
    { selector: "edge:selected", style: { "line-color": accent, width: 3 } },
  ];
}

function layoutOptions(name: LayoutName, eles: cytoscape.Collection): LayoutOptions {
  if (name === "cose") {
    return { name: "cose", eles, animate: false, padding: 40, nodeRepulsion: () => 12000, idealEdgeLength: () => 120 } as LayoutOptions;
  }
  // Ordering hint only: OS10 cores (if present) are roots at the top; LLDP edges decide the rest.
  const roots = eles.nodes('[size = "core"]');
  return {
    name: "breadthfirst",
    eles,
    directed: false,
    padding: 40,
    spacingFactor: 1.1,
    avoidOverlap: true,
    ...(roots.nonempty() ? { roots } : {}),
  } as LayoutOptions;
}

/** Place newly revealed group members in an arc below their group node (no global relayout). */
function placeAround(anchor: NodeSingular, members: cytoscape.NodeCollection) {
  const center = anchor.position();
  const count = members.length;
  const radius = Math.max(90, count * 11);
  members.forEach((member, index) => {
    const angle = Math.PI * (0.15 + (0.7 * (index + 0.5)) / count);
    member.position({ x: center.x + radius * Math.cos(angle) * 1.6, y: center.y + radius * Math.sin(angle) });
  });
}

export function TopologyGraph(props: TopologyGraphProps) {
  const { nodes, edges, visibleIds, groupCounts, selection, layout, layoutKey, fitSignal } = props;
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const structureRef = useRef("");
  const laidOutRef = useRef<Set<string>>(new Set());
  const lastLayoutKey = useRef("");
  const handlers = useRef(props);
  handlers.current = props;
  const [tooltip, setTooltip] = useState<{ x: number; y: number; lines: string[] } | null>(null);

  // Create once. Read-only: no box selection or edge creation; dragging is visual only.
  useEffect(() => {
    if (!containerRef.current) return;

    const cy = cytoscape({
      container: containerRef.current,
      style: buildStyle(),
      boxSelectionEnabled: false,
      minZoom: 0.15,
      maxZoom: 3,
      wheelSensitivity: 0.25,
    });

    cy.on("tap", "node", (event) => {
      const id = event.target.id();
      if (id.startsWith("group:")) handlers.current.onToggleGroup(id);
      else handlers.current.onSelect({ kind: "node", id });
    });
    cy.on("tap", "edge", (event) => {
      if (!event.target.id().startsWith("groupedge:")) handlers.current.onSelect({ kind: "edge", id: event.target.id() });
    });
    cy.on("tap", (event) => {
      if (event.target === cy) handlers.current.onSelect(null);
    });

    cy.on("mouseover", "node, edge", (event) => {
      const el = event.target;
      if (el.isEdge()) el.addClass("show-ports");
      const kind = el.isNode() ? "node" : "edge";
      const lines = handlers.current.tooltipFor(kind, el.id());
      const pos = el.isNode() ? el.renderedPosition() : el.renderedMidpoint();
      if (lines.length) setTooltip({ x: pos.x, y: pos.y, lines });
    });
    cy.on("mouseout", "node, edge", (event) => {
      const el = event.target;
      if (el.isEdge() && !el.selected()) el.removeClass("show-ports");
      setTooltip(null);
    });
    cy.on("pan zoom", () => setTooltip(null));

    const observer = new ResizeObserver(() => cy.resize());
    observer.observe(containerRef.current);

    cyRef.current = cy;
    return () => {
      observer.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  // Elements: rebuild only when the set of nodes/edges changes; otherwise update data in
  // place so polling never resets zoom/pan.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    const structure = [...nodes.map((n) => n.id), "|", ...edges.map((e) => e.id)].sort().join(",");
    if (structure !== structureRef.current) {
      structureRef.current = structure;
      laidOutRef.current = new Set();
      lastLayoutKey.current = "";
      cy.batch(() => {
        cy.elements().remove();
        cy.add([...nodes.map((n) => nodeElement(n, groupCounts.get(n.id))), ...edges.map(edgeElement)]);
      });
    } else {
      cy.batch(() => {
        nodes.forEach((n) => cy.getElementById(n.id).data(nodeElement(n, groupCounts.get(n.id)).data));
        edges.forEach((e) => cy.getElementById(e.id).data(edgeElement(e).data));
      });
    }
  }, [nodes, edges, groupCounts]);

  // Visibility (filters, view mode, expanded groups) + layout decisions.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.batch(() => {
      cy.nodes().forEach((n) => {
        n.toggleClass("hidden", !visibleIds.has(n.id()));
      });
      cy.edges().forEach((e) => {
        e.toggleClass("hidden", !visibleIds.has(e.source().id()) || !visibleIds.has(e.target().id()));
      });
    });

    const visibleEles = cy.elements().not(".hidden");
    const key = `${layoutKey}|${structureRef.current.length}`;

    if (key !== lastLayoutKey.current) {
      lastLayoutKey.current = key;
      cy.layout(layoutOptions(layout, visibleEles)).run();
      laidOutRef.current = new Set(visibleEles.nodes().map((n) => n.id()));
      cy.fit(visibleEles, 40);
      return;
    }

    // Newly revealed nodes that were never laid out: place them near their group anchor.
    const fresh = visibleEles.nodes().filter((n) => !laidOutRef.current.has(n.id()));
    if (fresh.nonempty()) {
      const byAnchor = new Map<string, cytoscape.NodeCollection>();
      fresh.forEach((n) => {
        const anchorId = handlers.current.nodes.find((g) => g.isGroup && g.groupMembers?.includes(n.id()) && visibleIds.has(g.id))?.id;
        if (!anchorId) return;
        byAnchor.set(anchorId, (byAnchor.get(anchorId) ?? cy.collection()).union(n));
      });
      byAnchor.forEach((members, anchorId) => placeAround(cy.getElementById(anchorId), members));
      fresh.forEach((n) => {
        laidOutRef.current.add(n.id());
      });
    }
  }, [visibleIds, layout, layoutKey, nodes, edges]);

  // Selection highlight; interface labels on the selected edge / the selected node's links.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.batch(() => {
      cy.elements().unselect();
      cy.edges().removeClass("show-ports");
      cy.elements().removeClass("dimmed");
      if (!selection) return;
      const element = cy.getElementById(selection.id);
      if (element.empty()) return;
      element.select();
      const related = selection.kind === "node" ? element.connectedEdges() : element;
      related.addClass("show-ports");
      const neighborhood = selection.kind === "node" ? element.closedNeighborhood() : element.union(element.connectedNodes());
      cy.elements().not(".hidden").difference(neighborhood).addClass("dimmed");
    });
  }, [selection, nodes, edges, visibleIds]);

  useEffect(() => {
    const cy = cyRef.current;
    if (cy && fitSignal > 0) cy.animate({ fit: { eles: cy.elements().not(".hidden"), padding: 40 } }, { duration: 200 });
  }, [fitSignal]);

  return (
    <div className="topology-canvas-wrap">
      <div ref={containerRef} className="topology-canvas" role="img" aria-label="Network topology graph" />
      {tooltip && (
        <div className="topology-tooltip" style={{ left: tooltip.x + 14, top: tooltip.y + 14 }} role="tooltip">
          {tooltip.lines.map((line, i) => (
            <div key={i} className={i === 0 ? "tooltip-title" : undefined}>
              {line}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
