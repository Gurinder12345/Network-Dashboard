// Health reason codes (worker health/checks.py classify) -> operator-facing text, and the
// per-check evidence. Text always accompanies the status colour.

export const HEALTH_REASON_LABELS: Record<string, string> = {
  ok: "All checks passed",
  slow_response: "Slow response",
  ssh_management_unavailable: "SSH management unavailable",
  ssh_authentication_failed: "SSH authentication failed",
  ssh_timeout: "SSH timeout",
  ssh_session_failed: "SSH session failed",
  cli_verification_failed: "CLI verification failed",
  network_unreachable: "Network unreachable (no ping reply, no TCP/22)",
  tcp22_unreachable: "TCP/22 unreachable (ping not available)",
  unsupported_platform: "Unsupported platform",
};

export function healthReasonLabel(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return HEALTH_REASON_LABELS[reason] ?? reason.replace(/_/g, " ");
}

export interface HealthEvidence {
  icmp_reachable?: boolean | null;
  tcp_reachable: boolean | null; // TCP port 22 only
  ssh_reachable: boolean | null; // SSH session established and authenticated
  cli_reachable: boolean | null; // verification command succeeded
}

/** [label, value] rows for a detail list. ICMP is supplemental (it may be blocked). */
export function evidenceRows(e: HealthEvidence): [string, string][] {
  if (e.tcp_reachable === null) return [];
  return [
    ["Reachability (ICMP)", e.icmp_reachable === true ? "Reachable" : e.icmp_reachable === false ? "No reply" : "Not tested"],
    ["TCP/22", e.tcp_reachable ? "Reachable" : "Unreachable"],
    ["SSH", e.ssh_reachable ? "Authenticated" : "Unavailable"],
    ["CLI", e.cli_reachable ? "Verified" : "Unavailable"],
  ];
}

/** One-line summary, e.g. "ICMP ok · TCP/22 fail · SSH fail · CLI fail". */
export function evidenceSummary(e: HealthEvidence): string | null {
  if (e.tcp_reachable === null) return null;
  const mark = (ok: boolean | null | undefined) => (ok ? "ok" : "fail");
  const icmp = e.icmp_reachable === true ? "ok" : e.icmp_reachable === false ? "no reply" : "n/a";
  return `ICMP ${icmp} · TCP/22 ${mark(e.tcp_reachable)} · SSH ${mark(e.ssh_reachable)} · CLI ${mark(e.cli_reachable)}`;
}
