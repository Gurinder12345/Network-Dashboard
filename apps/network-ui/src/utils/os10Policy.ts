// Early feedback only: mirrors the worker's Dell OS10 safe-L2 request grammar
// (apps/network-worker/changes/policy.py) so obvious mistakes are caught before a precheck
// is queued. The worker is authoritative: it re-checks the syntax and decides interface
// safety (LLDP neighbors, port-channel, management path, VLAN existence) against the device.

export type Os10FindingLevel = "error" | "warning" | "info";

export interface Os10Finding {
  level: Os10FindingLevel;
  text: string;
}

type Kind = "description" | "mode" | "access_vlan" | "trunk" | "admin";

interface Intent {
  kind: Kind;
  line: string;
  value: string;
}

export const OS10_PARENT = /^interface\s+ethernet\s*\d+\/\d+\/\d+(:\d+)?$/i;
const DESCRIPTION_TEXT = /^"?[A-Za-z0-9._-]{1,64}"?$/;
const VLAN_ID = /^[1-9][0-9]{0,3}$/;
const VLAN_ITEM = /^([1-9][0-9]{0,3})(?:-([1-9][0-9]{0,3}))?$/;
const MAX_LIST_TEXT = 200;
const MAX_LIST_ITEMS = 32;
const MAX_LIST_VLANS = 256;

// Why a command is outside the safe-L2 allow-list (first match wins; messaging only).
const BLOCKED: [RegExp, string][] = [
  [/(^|\s)(mgmt\S*|management)(\s|$)/i, "management interface / route changes are not allowed"],
  [/(^|\s)(ssh|ssh-server|crypto)(\s|$)/i, "SSH settings are not allowed"],
  [/^(\S+\s+){0,2}(aaa|tacacs-server|radius-server|tacacs|radius)(\s|$)/i, "AAA / TACACS / RADIUS changes are not allowed"],
  [/^(\S+\s+){0,2}(username|password|enable|userrole|role)(\s|$)/i, "user / password changes are not allowed"],
  [/^(reload|reboot|write|delete|copy|restore|boot|erase|image)(\s|$)|factory|startup-config/i,
    "reload / erase / factory-default / file operations are not allowed"],
  [/^default(\s|$)/i, "`default` commands are not allowed"],
  [/^(no\s+)?(router|ip\s+route|ipv6\s+route)(\s|$)|(^|\s)(bgp|ospf|ospfv3|vrf|vrrp|pim)(\s|$)/i,
    "routing / VRF / static route changes are not allowed"],
  [/^(no\s+)?(ip|ipv6)\s+address(\s|$)/i, "IP address changes are not allowed"],
  [/(^|\s)vlt\S*/i, "VLT configuration is not allowed"],
  [/^(no\s+)?vlan(\s|$)|^(no\s+)?interface\s+vlan/i, "VLAN creation/removal is not allowed"],
  [/port-channel|channel-group|(^|\s)lacp(\s|$)/i, "port-channel / channel-group changes are not allowed"],
  [/^(no\s+)?interface(\s|$)/i, "a second interface context is not allowed (use the parent field)"],
  [/^(no\s+)?snmp/i, "SNMP configuration is not allowed"],
  [/access-list|access-group|(^|\s)acl(\s|$)/i, "ACL changes are not allowed"],
  [/^(no\s+)?(qos|service-policy|trust|class-map|policy-map|flowcontrol|priority-flow-control)(\s|$)/i,
    "QoS / flow-control changes are not allowed"],
  [/^(no\s+)?spanning-tree(\s|$)/i, "spanning-tree changes are not allowed"],
  [/^(no\s+)?mtu(\s|$)/i, "MTU changes are not supported in this release"],
  [/^no\s+switchport$/i, "`no switchport` (routed port conversion) is not allowed"],
  [/^switchport\s+trunk\s+native/i, "native VLAN changes are not supported in this release"],
  [/^no\s+switchport\s/i, "use `switchport trunk allowed vlan remove <list>` to remove VLANs from a trunk"],
];

/** Returns an error message, or null if the list is valid. */
export function vlanListError(text: string): string | null {
  if (!text) return "VLAN list is empty";
  if (text.length > MAX_LIST_TEXT) return `VLAN list is longer than ${MAX_LIST_TEXT} characters`;
  const items = text.split(",");
  if (items.length > MAX_LIST_ITEMS) return `VLAN list has more than ${MAX_LIST_ITEMS} items`;
  let count = 0;
  for (const item of items) {
    const match = VLAN_ITEM.exec(item);
    if (!match) return `malformed VLAN list item "${item}"`;
    const start = Number(match[1]);
    const end = match[2] ? Number(match[2]) : start;
    if (start > 4094 || end > 4094) return `VLAN ${Math.max(start, end)} is out of range (1-4094)`;
    if (match[2] && end <= start) return `VLAN range "${item}" must be ascending`;
    count += end - start + 1;
    if (count > MAX_LIST_VLANS) return `VLAN list expands to more than ${MAX_LIST_VLANS} VLANs`;
  }
  return null;
}

function parseLine(line: string): Intent | string {
  const tokens = line.split(/\s+/);
  const low = tokens.map((t) => t.toLowerCase());
  const joined = low.join(" ");

  if (joined === "no description") return { kind: "description", line, value: "" };
  if (low[0] === "description") {
    return tokens.length === 2 && DESCRIPTION_TEXT.test(tokens[1])
      ? { kind: "description", line, value: tokens[1] }
      : "description text must be 1-64 characters of A-Z a-z 0-9 . _ - (no spaces)";
  }
  if (joined === "shutdown" || joined === "no shutdown") return { kind: "admin", line, value: joined };
  if (joined === "switchport mode access" || joined === "switchport mode trunk") {
    return { kind: "mode", line, value: low[2] };
  }
  if (joined.startsWith("switchport access vlan")) {
    if (tokens.length !== 4) return "expected `switchport access vlan <id>` with one VLAN ID 1-4094";
    return VLAN_ID.test(tokens[3]) && Number(tokens[3]) <= 4094
      ? { kind: "access_vlan", line, value: tokens[3] }
      : `invalid VLAN ID "${tokens[3]}" (expected 1-4094)`;
  }
  if (joined.startsWith("switchport trunk allowed vlan")) {
    const operation = low[4] === "add" || low[4] === "remove" ? low[4] : "set";
    const list = operation === "set" ? tokens[4] : tokens[5];
    const expected = operation === "set" ? 5 : 6;
    if (tokens.length !== expected || !list || ["all", "none", "except"].includes(low[4])) {
      return "expected `switchport trunk allowed vlan [add|remove] <list>`, e.g. 10 / 10,20 / 10-20";
    }
    return vlanListError(list) ?? { kind: "trunk", line, value: operation };
  }
  for (const [pattern, reason] of BLOCKED) {
    if (pattern.test(line)) return reason;
  }
  if (low[0] === "switchport") {
    return "unsupported switchport command (supported: mode access|trunk, access vlan, trunk allowed vlan [add|remove])";
  }
  return "not in the Dell OS10 safe-L2 allow-list";
}

const KIND_LABELS: Record<Kind, string> = {
  description: "description",
  mode: "switchport mode",
  access_vlan: "access VLAN",
  trunk: "trunk allowed-VLAN",
  admin: "shutdown / no shutdown",
};

export function validateOs10Request(parents: string[], lines: string[]): Os10Finding[] {
  const findings: Os10Finding[] = [];

  if (parents.length !== 1) {
    findings.push({
      level: "error",
      text: "Dell OS10 needs exactly one parent: interface ethernetX/Y/Z (global configuration is not allowed).",
    });
  } else if (!OS10_PARENT.test(parents[0])) {
    findings.push({
      level: "error",
      text: `"${parents[0]}" — the parent must be a front-panel interface ethernetX/Y/Z (no abbreviations, no mgmt / vlan / port-channel).`,
    });
  }

  const intents: Intent[] = [];
  lines.forEach((line) => {
    const result = parseLine(line);
    if (typeof result === "string") findings.push({ level: "error", text: `"${line}" — ${result}.` });
    else intents.push(result);
  });

  const byKind = new Map<Kind, Intent[]>();
  intents.forEach((intent) => byKind.set(intent.kind, [...(byKind.get(intent.kind) ?? []), intent]));
  byKind.forEach((items, kind) => {
    if (items.length > 1) findings.push({ level: "error", text: `Send one ${KIND_LABELS[kind]} change per request.` });
  });

  const mode = byKind.get("mode")?.[0]?.value;
  if (mode === "access" && byKind.has("trunk")) {
    findings.push({ level: "error", text: "Contradictory: a trunk allowed-VLAN change on a port being set to access mode." });
  }
  if (mode === "trunk" && byKind.has("access_vlan")) {
    findings.push({
      level: "error",
      text: "On OS10, `switchport access vlan` on a trunk sets its untagged (native) VLAN; native VLAN changes are not supported.",
    });
  }
  if (!mode && byKind.has("access_vlan") && byKind.has("trunk")) {
    findings.push({ level: "error", text: "Ambiguous: an access VLAN and a trunk allowed-VLAN change in one request." });
  }

  if (byKind.get("admin")?.[0]?.value === "shutdown") {
    findings.push({
      level: "warning",
      text: "shutdown is disruptive. The precheck accepts it only on an interface with no LLDP neighbor, outside any port-channel, VLT and the management path.",
    });
  }
  if (["mode", "access_vlan", "trunk"].some((kind) => byKind.has(kind as Kind))) {
    findings.push({
      level: "info",
      text: "The precheck verifies VLAN existence and the current port mode, and rejects uplinks, LLDP-connected ports, port-channel members and the management path.",
    });
  }
  return findings;
}

export const OS10_EXAMPLES: { label: string; lines: string[] }[] = [
  { label: "Description", lines: ["description AUTOMATION-TEST"] },
  { label: "Access VLAN", lines: ["switchport mode access", "switchport access vlan 20"] },
  { label: "Trunk", lines: ["switchport mode trunk", "switchport trunk allowed vlan 30,40"] },
  { label: "Add to trunk", lines: ["switchport trunk allowed vlan add 50"] },
  { label: "Remove from trunk", lines: ["switchport trunk allowed vlan remove 50"] },
  { label: "Shut unused port", lines: ["description PARKED", "shutdown"] },
  { label: "Bring up", lines: ["no shutdown"] },
];
