import { useEffect, useRef } from "react";
import cytoscape, { type Core, type ElementDefinition, type LayoutOptions } from "cytoscape";
import type { HealthStatus, TopologyLink, TopologyNode } from "../api/types";

export type GraphSelection = { kind: "node" | "edge"; id: string } | null;
export type LayoutName = "breadthfirst" | "cose";

interface TopologyGraphProps {
  nodes: (TopologyNode & { effective_health: HealthStatus })[];
  links: TopologyLink[];
  visibleNodeIds: Set<string>;
  selection: GraphSelection;
  onSelect: (selection: GraphSelection) => void;
  layout: LayoutName;
  fitSignal: number;
  resetSignal: number;
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

function nodeElement(node: TopologyGraphProps["nodes"][number]): ElementDefinition {
  const platform = node.platform ? node.platform.replace("dell_", "").toUpperCase() : "";
  return {
    group: "nodes",
    data: {
      id: node.id,
      label: node.managed ? `${node.hostname}\n${platform}` : `${node.hostname}\nUnmanaged`,
      health: node.managed ? node.effective_health : "unmanaged",
      managed: node.managed ? "yes" : "no",
      core: node.platform === "dell_os10" ? "yes" : "no",
    },
  };
}

function edgeElement(link: TopologyLink): ElementDefinition {
  return {
    group: "edges",
    data: {
      id: link.id,
      source: link.source,
      target: link.target,
      sourceLabel: link.source_interface ?? "",
      targetLabel: link.target_interface ?? link.target_port_id ?? "",
      observed: link.observed_bidirectionally ? "both" : "one",
      relationship: link.relationship,
      active: link.active ? "yes" : "no",
    },
  };
}

function buildStyle(): cytoscape.StylesheetJson {
  const text = cssVar("--text-primary", "#e6ebf3");
  const muted = cssVar("--text-secondary", "#9ba6b9");
  const panel = cssVar("--bg-panel-raised", "#161e2b");
  const inset = cssVar("--bg-inset", "#0b1019");
  const edge = cssVar("--border-strong", "#34415a");
  const accent = cssVar("--accent", "#3b9eff");
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
        width: "label",
        height: "label",
        padding: "10px",
        "background-color": panel,
        "border-width": 2,
        label: "data(label)",
        "text-wrap": "wrap",
        "text-valign": "center",
        "text-halign": "center",
        color: text,
        "font-family": font,
        "font-size": 11,
        "font-weight": 600,
        "line-height": 1.35,
      },
    },
    ...healthRules,
    { selector: 'node[health = "down"]', style: { "background-color": "#2a1618" } },
    { selector: 'node[core = "yes"]', style: { "font-size": 12.5, padding: "14px", "border-width": 3 } },
    {
      selector: 'node[managed = "no"]',
      style: {
        "background-color": inset,
        "border-style": "dashed",
        "border-color": cssVar("--tone-neutral", "#7d899e"),
        color: muted,
        "font-weight": 400,
      },
    },
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
        "text-background-opacity": 0.9,
        "text-background-padding": "2px",
        "source-text-offset": 44,
        "target-text-offset": 44,
      },
    },
    { selector: 'edge[observed = "one"]', style: { "line-style": "dashed" } },
    { selector: 'edge[relationship = "unmanaged"]', style: { "line-style": "dotted", width: 1.5 } },
    { selector: 'edge[active = "no"]', style: { opacity: 0.35 } },
    { selector: ".filtered-out", style: { display: "none" } },
    { selector: ".dimmed", style: { opacity: 0.25 } },
    {
      selector: "edge.show-ports",
      style: { "source-label": "data(sourceLabel)", "target-label": "data(targetLabel)", "line-color": accent },
    },
    { selector: "node:selected", style: { "overlay-color": accent, "overlay-opacity": 0.18, "overlay-padding": 6 } },
    { selector: "edge:selected", style: { "line-color": accent, width: 3 } },
  ];
}

function layoutOptions(cy: Core, name: LayoutName): LayoutOptions {
  if (name === "cose") {
    return { name: "cose", animate: false, padding: 40, nodeRepulsion: () => 9000, idealEdgeLength: () => 110 } as LayoutOptions;
  }
  // Ordering hint only: OS10 cores (if present) start at the top. LLDP decides everything else.
  const roots = cy.nodes('[core = "yes"]');
  return {
    name: "breadthfirst",
    directed: false,
    padding: 40,
    spacingFactor: 1.15,
    avoidOverlap: true,
    ...(roots.nonempty() ? { roots } : {}),
  } as LayoutOptions;
}

export function TopologyGraph({
  nodes,
  links,
  visibleNodeIds,
  selection,
  onSelect,
  layout,
  fitSignal,
  resetSignal,
}: TopologyGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const structureRef = useRef("");
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  // Create once. Read-only: no box selection, no edge creation; dragging is visual only.
  useEffect(() => {
    if (!containerRef.current) return;

    const cy = cytoscape({
      container: containerRef.current,
      style: buildStyle(),
      boxSelectionEnabled: false,
      minZoom: 0.2,
      maxZoom: 3,
      wheelSensitivity: 0.25,
    });

    cy.on("tap", "node", (event) => onSelectRef.current({ kind: "node", id: event.target.id() }));
    cy.on("tap", "edge", (event) => onSelectRef.current({ kind: "edge", id: event.target.id() }));
    cy.on("tap", (event) => {
      if (event.target === cy) onSelectRef.current(null);
    });

    cyRef.current = cy;

    // Keep the canvas in sync with its container (e.g. the details drawer opening),
    // without changing zoom/pan.
    const observer = new ResizeObserver(() => cy.resize());
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  // Data: rebuild + relayout only when the set of nodes/links changes. Otherwise update
  // data in place so a 60 s poll never resets the user's zoom/pan.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    const structure = [...nodes.map((n) => n.id), "|", ...links.map((l) => l.id)].sort().join(",");

    if (structure !== structureRef.current) {
      structureRef.current = structure;
      cy.batch(() => {
        cy.elements().remove();
        cy.add([...nodes.map(nodeElement), ...links.map(edgeElement)]);
      });
      cy.layout(layoutOptions(cy, layout)).run();
      cy.fit(undefined, 40);
    } else {
      cy.batch(() => {
        nodes.forEach((node) => cy.getElementById(node.id).data(nodeElement(node).data));
        links.forEach((link) => cy.getElementById(link.id).data(edgeElement(link).data));
      });
    }
  }, [nodes, links, layout]);

  // Client-side filters: hide nodes and any edge touching a hidden node. No refetch.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.batch(() => {
      cy.nodes().forEach((node) => {
        node.toggleClass("filtered-out", !visibleNodeIds.has(node.id()));
      });
      cy.edges().forEach((edge) => {
        edge.toggleClass("filtered-out", !visibleNodeIds.has(edge.source().id()) || !visibleNodeIds.has(edge.target().id()));
      });
    });
  }, [visibleNodeIds, nodes, links]);

  // Selection: highlight the element and show interface labels on its links.
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
      cy.elements().difference(neighborhood).addClass("dimmed");
    });
  }, [selection, nodes, links]);

  useEffect(() => {
    const cy = cyRef.current;
    if (cy && fitSignal > 0) cy.animate({ fit: { eles: cy.elements(":visible"), padding: 40 } }, { duration: 200 });
  }, [fitSignal]);

  useEffect(() => {
    const cy = cyRef.current;
    if (cy && resetSignal > 0) {
      cy.layout(layoutOptions(cy, layout)).run();
      cy.fit(cy.elements(":visible"), 40);
    }
  }, [resetSignal, layout]);

  return <div ref={containerRef} className="topology-canvas" role="img" aria-label="Network topology graph" />;
}
