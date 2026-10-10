import type { InterfaceItem, InterfaceStatus } from "../../api/types";
import type { Tone } from "../StatusBadge";

// Display-only levels for link utilization bars (never change any status).
export const UTIL_THRESHOLDS = { warning: 70, critical: 90 };

export function utilLevel(value: number): "normal" | "warning" | "critical" {
  if (value >= UTIL_THRESHOLDS.critical) return "critical";
  if (value >= UTIL_THRESHOLDS.warning) return "warning";
  return "normal";
}

export const STATUS_LABELS: Record<InterfaceStatus, string> = {
  up: "Up",
  down: "Down",
  admin_down: "Admin down",
  unknown: "Unknown",
};

export const STATUS_TONES: Record<InterfaceStatus, Tone> = {
  up: "success",
  down: "danger",
  admin_down: "neutral",
  unknown: "neutral",
};

export const ROLE_LABELS: Record<NonNullable<InterfaceItem["role"]>, { label: string; title: string }> = {
  inter_switch: { label: "inter-switch", title: "LLDP neighbor on this port is another managed switch (topology discovery)" },
  uplink: { label: "uplink", title: "The port description names it an uplink" },
  lag: { label: "LAG", title: "Port-channel (link aggregation)" },
};

export function formatSpeed(bps: number | null): string {
  if (!bps) return "—";
  if (bps >= 1e9) return `${+(bps / 1e9).toFixed(1)}G`;
  if (bps >= 1e6) return `${+(bps / 1e6).toFixed(0)}M`;
  return `${Math.round(bps / 1e3)}K`;
}

export function formatUtil(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(value < 10 ? 2 : 1)}%`;
}

export function formatCount(value: number | null): string {
  return value === null ? "—" : value.toLocaleString();
}

export function formatBytes(value: number | null): string {
  if (value === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let n = value;
  let unit = 0;
  while (n >= 1000 && unit < units.length - 1) {
    n /= 1000;
    unit += 1;
  }
  return `${value.toLocaleString()} (${n.toFixed(unit ? 1 : 0)} ${units[unit]})`;
}

export function vlanText(i: InterfaceItem): string {
  if (i.mode === "access") return i.access_vlan ? `Access · VLAN ${i.access_vlan}` : "Access";
  if (i.mode === "trunk") {
    const parts = ["Trunk"];
    if (i.native_vlan) parts.push(`native ${i.native_vlan}`);
    if (i.allowed_vlans) parts.push(i.allowed_vlans);
    return parts.join(" · ");
  }
  if (i.mode === "general") return "General";
  if (i.mode === "routed") return "Routed";
  return "—";
}
