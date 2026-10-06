import type {
  ApplyStatus,
  ApplySubmitted,
  Approval,
  AuditEvent,
  Backup,
  BackupRequested,
  Device,
  DeviceDetail,
  FleetHealth,
  HealthCheckRequested,
  Job,
  JobDetail,
  MetricsHistory,
  MetricsRange,
  ChangeRequest,
  PcapAnalysis,
  PcapMode,
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

export function submitOs6Precheck(request: ChangeRequest): Promise<Os6PrecheckSubmitted> {
  return postJson<Os6PrecheckSubmitted>("/api/v1/changes/os6/precheck", request);
}

// Platform-generic precheck (Dell OS6 / Dell OS10) for ordered configuration blocks; the
// worker dispatches by platform. Only the device id, blocks and read-only verification
// commands are sent.
export function submitChangePrecheck(request: ChangeRequest): Promise<Os6PrecheckSubmitted> {
  return postJson<Os6PrecheckSubmitted>("/api/v1/changes/precheck", request);
}

export function getChangePrecheck(requestId: string): Promise<Os6PrecheckStatus> {
  return getJson<Os6PrecheckStatus>(`/api/v1/changes/precheck/${encodeURIComponent(requestId)}`);
}

export function getOs6Precheck(requestId: string): Promise<Os6PrecheckStatus> {
  return getJson<Os6PrecheckStatus>(`/api/v1/changes/os6/precheck/${encodeURIComponent(requestId)}`);
}

export type BackupRequestResult =
  | { kind: "queued"; response: BackupRequested }
  | { kind: "already_running"; jobId: string | null; message: string };

// Read-only running-config backup; no approval. Only the device ID is sent. A 409 means a
// backup is already running for that device and carries its job ID so the UI can follow it.
export async function requestDeviceBackup(deviceId: number): Promise<BackupRequestResult> {
  const path = `/api/v1/devices/${deviceId}/backup`;
  const response = await fetch(`${API_BASE_URL}${path}`, { method: "POST" });

  if (response.status === 409) {
    const body = await response.json().catch(() => ({}));
    return {
      kind: "already_running",
      jobId: typeof body?.job_id === "string" ? body.job_id : null,
      message: typeof body?.detail === "string" ? body.detail : "Backup already running for this device.",
    };
  }

  if (!response.ok) {
    throw new Error(await errorMessage(response, path));
  }

  return { kind: "queued", response: (await response.json()) as BackupRequested };
}

export function getJob(jobId: string): Promise<JobDetail> {
  return getJson<JobDetail>(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
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

// Device detail + telemetry: reads stored data only; never starts switch polling.
export function getDeviceDetail(deviceId: number): Promise<DeviceDetail> {
  return getJson<DeviceDetail>(`/api/v1/devices/${deviceId}`);
}

export function getDeviceMetrics(deviceId: number, range: MetricsRange): Promise<MetricsHistory> {
  return getJson<MetricsHistory>(`/api/v1/devices/${deviceId}/metrics?range=${range}`);
}

// ---- PCAP Analyzer ----------------------------------------------------------------------
// Upload with XMLHttpRequest so the page can show upload progress. The API answers 202 as
// soon as the files are stored; analysis status is then polled. Files go to the API only.
export function uploadPcapAnalysis(
  mode: PcapMode,
  clientFile: File,
  serverFile: File | null,
  onProgress: (fraction: number) => void,
): Promise<{ analysis_id: string; status: string }> {
  const form = new FormData();
  form.append("mode", mode);
  form.append("client_file", clientFile, clientFile.name);
  if (serverFile) form.append("server_file", serverFile, serverFile.name);

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE_URL}/api/v1/pcap-analysis`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new Error("Cannot reach the API. Check the connection and try again."));
    xhr.onload = () => {
      let body: { detail?: unknown; analysis_id?: string; status?: string } = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // non-JSON error body
      }
      if (xhr.status === 202 && body.analysis_id) {
        resolve({ analysis_id: body.analysis_id, status: body.status ?? "queued" });
      } else {
        reject(new Error(typeof body.detail === "string" ? body.detail : `Upload failed (${xhr.status})`));
      }
    };
    xhr.send(form);
  });
}

export function getPcapAnalysis(analysisId: string): Promise<PcapAnalysis> {
  return getJson<PcapAnalysis>(`/api/v1/pcap-analysis/${encodeURIComponent(analysisId)}`);
}

export function listPcapAnalyses(): Promise<PcapAnalysis[]> {
  return getJson<PcapAnalysis[]>("/api/v1/pcap-analysis");
}

export async function deletePcapAnalysis(analysisId: string): Promise<void> {
  const path = `/api/v1/pcap-analysis/${encodeURIComponent(analysisId)}`;
  const response = await fetch(`${API_BASE_URL}${path}`, { method: "DELETE" });
  if (!response.ok) throw new Error(await errorMessage(response, path));
}

/** Error carrying the HTTP status, for callers that word 404/409 differently. */
export class ApiRequestError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

// Removes a finished job from the Jobs list (server-side soft delete). Related backups,
// approvals and audit history are kept; the server re-checks the job's current status.
export async function deleteJob(jobId: string): Promise<void> {
  const path = `/api/v1/jobs/${encodeURIComponent(jobId)}`;
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { method: "DELETE" });
  } catch {
    throw new ApiRequestError("Cannot reach the API.", 0);
  }
  if (!response.ok) {
    throw new ApiRequestError(await errorMessage(response, path), response.status);
  }
}
