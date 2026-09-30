import type {
  ApplyStatus,
  ApplySubmitted,
  Approval,
  AuditEvent,
  Backup,
  Device,
  FleetHealth,
  HealthCheckRequested,
  Job,
  Os6PrecheckRequest,
  Os6PrecheckStatus,
  Os6PrecheckSubmitted,
  TopologyGraph,
} from "./types";

// Every path below already starts with /api/v1, so the default (empty) base keeps requests
// same-origin: Traefik routes /api to network-api in K3s, and the Vite dev server proxies
// /api locally. Set VITE_API_BASE_URL only to call an API on another origin.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

// FastAPI returns `detail` as a string, or as a list of validation errors.
async function errorMessage(response: Response, path: string): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail
        .map((item: { msg?: string }) => (item.msg ?? "").replace(/^Value error, /, ""))
        .join("; ");
    }
  } catch {
    // Non-JSON error body; fall through to the generic message.
  }
  return `${path} failed with status ${response.status}`;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);

  if (!response.ok) {
    throw new Error(await errorMessage(response, path));
  }

  return response.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(await errorMessage(response, path));
  }

  return response.json() as Promise<T>;
}

export function getDevices(): Promise<Device[]> {
  return getJson<Device[]>("/api/v1/devices");
}

export function getJobs(): Promise<Job[]> {
  return getJson<Job[]>("/api/v1/jobs");
}

export function getApprovals(): Promise<Approval[]> {
  return getJson<Approval[]>("/api/v1/approvals");
}

export function getBackups(): Promise<Backup[]> {
  return getJson<Backup[]>("/api/v1/backups");
}

export function getAuditEvents(): Promise<AuditEvent[]> {
  return getJson<AuditEvent[]>("/api/v1/audit");
}

export function submitOs6Precheck(request: Os6PrecheckRequest): Promise<Os6PrecheckSubmitted> {
  return postJson<Os6PrecheckSubmitted>("/api/v1/changes/os6/precheck", request);
}

export function getOs6Precheck(requestId: string): Promise<Os6PrecheckStatus> {
  return getJson<Os6PrecheckStatus>(`/api/v1/changes/os6/precheck/${encodeURIComponent(requestId)}`);
}

export interface DownloadedFile {
  blob: Blob;
  filename: string;
}

// Only the numeric backup ID is sent; the server resolves the stored path itself.
export async function downloadBackup(backupId: number): Promise<DownloadedFile> {
  const path = `/api/v1/backups/${backupId}/download`;
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`);
  } catch {
    throw new Error("Cannot reach the API. Check the connection and try again.");
  }

  if (response.status === 404) {
    throw new Error(`Not found: ${await errorMessage(response, path)}`);
  }

  if (!response.ok) {
    throw new Error(`Download failed (${response.status}): ${await errorMessage(response, path)}`);
  }

  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = disposition.match(/filename="?([^";]+)"?/);

  return {
    blob: await response.blob(),
    filename: match?.[1] ?? `backup-${backupId}.cfg`,
  };
}

// Only the approver identity is sent; the API approves the configuration already stored
// on the approval record and never applies it.
export function approveApproval(approvalId: string, approvedBy: string): Promise<Approval> {
  return postJson<Approval>(`/api/v1/approvals/${encodeURIComponent(approvalId)}/approve`, {
    approved_by: approvedBy,
  });
}

// No configuration is sent: the API applies the stored approved record only.
export function applyApproval(approvalId: string): Promise<ApplySubmitted> {
  return postJson<ApplySubmitted>(`/api/v1/approvals/${encodeURIComponent(approvalId)}/apply`, {});
}

export function getApplyStatus(approvalId: string, requestId: string): Promise<ApplyStatus> {
  return getJson<ApplyStatus>(
    `/api/v1/approvals/${encodeURIComponent(approvalId)}/apply/${encodeURIComponent(requestId)}`,
  );
}

// Reads cached/stored health only; never triggers switch checks.
export function getFleetHealth(): Promise<FleetHealth> {
  return getJson<FleetHealth>("/api/v1/health/devices");
}

// Queues one read-only fleet sweep. 409 = already running, 429 = cooldown (message in the error).
export function requestHealthCheck(): Promise<HealthCheckRequested> {
  return postJson<HealthCheckRequested>("/api/v1/health/check", {});
}

// Status change only (pending/approved -> cancelled). Sends identity and an optional reason,
// never configuration; nothing is sent to a switch.
export function cancelApproval(
  approvalId: string,
  cancelledBy: string,
  reason: string | null,
): Promise<Approval> {
  return postJson<Approval>(`/api/v1/approvals/${encodeURIComponent(approvalId)}/cancel`, {
    cancelled_by: cancelledBy,
    reason,
  });
}

// Reads stored topology only; never triggers LLDP collection.
export function getTopology(includeInactive = false): Promise<TopologyGraph> {
  return getJson<TopologyGraph>(`/api/v1/topology${includeInactive ? "?include_inactive=true" : ""}`);
}

// Queues one read-only fleet LLDP discovery. 409 = already running, 429 = cooldown.
export function requestTopologyDiscovery(): Promise<HealthCheckRequested> {
  return postJson<HealthCheckRequested>("/api/v1/topology/discover", {});
}
