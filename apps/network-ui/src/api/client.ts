import type { Approval, Backup, Device, Job } from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);

  if (!response.ok) {
    throw new Error(`${path} failed with status ${response.status}`);
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
