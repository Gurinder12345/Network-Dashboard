/**
 * Presentation-only topology view model. Nothing here changes topology truth: it never
 * merges nodes, creates managed devices or alters links. It classifies UNMANAGED LLDP
 * neighbors for display and builds UI-only endpoint-group nodes for the Infrastructure view.
 *
 * Classification uses structural evidence from what the neighbor advertised, never name
 * similarity:
 *   unmanaged_network  at least one advertised Port ID has switch-port syntax
 *                      (e.g. Te1/0/3, Gi1/0/1, Tw1/0/4, ethernet1/8) -- a switch port
 *   endpoint           every advertised Port ID is a MAC address (typical of host NICs)
 *   unknown            anything else (numeric "1"/"51", "gi1", no Port ID, ...)
 */

import type { HealthStatus, TopologyLink, TopologyNode } from "../api/types";

export type NodeCategory = "managed_network" | "unmanaged_network" | "endpoint" | "unknown";
export type ViewMode = "infrastructure" | "all";
export type TypeFilter = "all" | "network" | "endpoint" | "unknown";

const SWITCH_PORT = /^(ethernet|eth|gi|gigabitethernet|te|tengigabitethernet|tw|twentyfivegige|fo|fortygige|hu|hundredgige|xe|ge|mgmt)\s?\d+(\/\d+)+(:\d+)?$/i;
const MAC = /^([0-9a-f]{2}[:\-.]?){5}[0-9a-f]{2}$|^([0-9a-f]{4}\.){2}[0-9a-f]{4}$/i;

export function classifyUnmanaged(node: TopologyNode): NodeCategory {
  const ports = (node.remote_port_ids ?? []).map((p) => p.trim()).filter(Boolean);
  if (ports.some((p) => SWITCH_PORT.test(p))) return "unmanaged_network";
  if (ports.length > 0 && ports.every((p) => MAC.test(p))) return "endpoint";
  return "unknown";
}

export interface DisplayNode extends TopologyNode {
  effective_health: HealthStatus;
  category: NodeCategory;
  /** UI-only endpoint-group node: never sent anywhere, never persisted. */
  isGroup?: boolean;
  groupOwner?: string;
  groupMembers?: string[];
}

export interface DisplayEdge extends TopologyLink {
  isGroupEdge?: boolean;
}

export interface DisplayModel {
  nodes: DisplayNode[];
  edges: DisplayEdge[];
  byId: Map<string, DisplayNode>;
  /** managed node id -> endpoint/unknown neighbor ids attached to it */
  collapsible: Map<string, string[]>;
}

export const groupIdFor = (managedId: string) => `group:${managedId}`;

export function buildDisplayModel(
  nodes: (TopologyNode & { effective_health: HealthStatus })[],
  links: TopologyLink[],
): DisplayModel {
  const displayNodes: DisplayNode[] = nodes.map((n) => ({
    ...n,
    category: n.managed ? "managed_network" : classifyUnmanaged(n),
  }));
  const byId = new Map(displayNodes.map((n) => [n.id, n]));

  const collapsible = new Map<string, string[]>();
  for (const node of displayNodes) {
    if (node.category !== "endpoint" && node.category !== "unknown") continue;
    for (const deviceId of node.attached_device_ids ?? []) {
      const owner = `device:${deviceId}`;
      if (!byId.has(owner)) continue;
      collapsible.set(owner, [...(collapsible.get(owner) ?? []), node.id]);
    }
  }

  const groupNodes: DisplayNode[] = [];
  const groupEdges: DisplayEdge[] = [];
  for (const [owner, members] of collapsible) {
    const id = groupIdFor(owner);
    groupNodes.push({
      id,
      device_id: null,
      hostname: `${members.length} LLDP neighbors`,
      management_ip: null,
      platform: null,
      managed: false,
      health_status: "unknown",
      effective_health: "unknown",
      response_time_ms: null,
      neighbor_count: members.length,
      topology_last_seen_at: null,
      category: "endpoint",
      isGroup: true,
      groupOwner: owner,
      groupMembers: members,
    });
    groupEdges.push({
      id: `groupedge:${owner}`,
      source: owner,
      target: id,
      source_interface: null,
      target_interface: null,
      protocol: "lldp",
      first_seen_at: "",
      last_seen_at: "",
      active: true,
      observed_bidirectionally: false,
      relationship: "unmanaged",
      isGroupEdge: true,
    });
  }

  const all = [...displayNodes, ...groupNodes];
  return {
    nodes: all,
    edges: [...links, ...groupEdges],
    byId: new Map(all.map((n) => [n.id, n])),
    collapsible,
  };
}

export interface Filters {
  search: string;
  health: string; // "all" | HealthStatus
  management: string; // "all" | "managed" | "unmanaged"
  type: TypeFilter;
  platform: string; // "all" | platform
}

function matchesFilters(node: DisplayNode, f: Filters): boolean {
  if (f.health !== "all" && (!node.managed || node.effective_health !== f.health)) return false;
  if (f.management === "managed" && !node.managed) return false;
  if (f.management === "unmanaged" && node.managed) return false;
  if (f.platform !== "all" && node.platform !== f.platform) return false;
  if (f.type === "network" && node.category !== "managed_network" && node.category !== "unmanaged_network") return false;
  if (f.type === "endpoint" && node.category !== "endpoint") return false;
  if (f.type === "unknown" && node.category !== "unknown") return false;
  const q = f.search.trim().toLowerCase();
  if (q) {
    const hay = `${node.hostname} ${node.advertised_system_name ?? ""} ${node.management_ip ?? ""} ${node.chassis_id ?? ""}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

export interface Visibility {
  visible: Set<string>;
  /** group id -> number of its members passing filters (shown on the group label) */
  groupCounts: Map<string, number>;
  hiddenEndpoints: number;
}

export function computeVisibility(model: DisplayModel, mode: ViewMode, expanded: Set<string>, f: Filters): Visibility {
  const visible = new Set<string>();
  const groupCounts = new Map<string, number>();
  let hiddenEndpoints = 0;

  // Managed devices: shown unless the user explicitly filters them out (down/idle
  // inventory devices stay visible even with no LLDP links).
  for (const node of model.nodes) {
    if (node.isGroup) continue;
    if (!matchesFilters(node, f)) continue;
    if (node.managed || node.category === "unmanaged_network" || mode === "all") visible.add(node.id);
  }

  if (mode === "infrastructure") {
    const searching = f.search.trim().length > 0;
    for (const [owner, members] of model.collapsible) {
      const groupId = groupIdFor(owner);
      const passing = members.filter((id) => matchesFilters(model.byId.get(id)!, f));
      if (passing.length === 0) continue;

      // A search that matches collapsed neighbors shows them directly, with their switch
      // kept visible as context (only if the switch passes every non-search filter).
      if (searching) {
        const ownerNode = model.byId.get(owner)!;
        if (visible.has(owner) || matchesFilters(ownerNode, { ...f, search: "" })) {
          visible.add(owner);
          passing.forEach((id) => visible.add(id));
          groupCounts.set(groupId, passing.length);
          visible.add(groupId);
        }
        continue;
      }

      if (!visible.has(owner)) continue;
      if (expanded.has(groupId)) {
        passing.forEach((id) => visible.add(id));
      } else {
        hiddenEndpoints += passing.length;
      }
      groupCounts.set(groupId, passing.length);
      visible.add(groupId);
    }
    // A neighbor attached to several switches counts once in "hidden".
    const shown = new Set([...visible]);
    const uniqueHidden = new Set<string>();
    for (const [owner, members] of model.collapsible) {
      if (!visible.has(owner) || expanded.has(groupIdFor(owner))) continue;
      members.filter((id) => !shown.has(id) && matchesFilters(model.byId.get(id)!, f)).forEach((id) => uniqueHidden.add(id));
    }
    hiddenEndpoints = uniqueHidden.size;
  }

  return { visible, groupCounts, hiddenEndpoints };
}
