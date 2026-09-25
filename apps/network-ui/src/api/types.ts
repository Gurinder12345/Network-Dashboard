export interface Device {
  id: number;
  hostname: string;
  management_ip: string;
  platform: string;
  enabled: boolean;
}

export interface Job {
  id: string;
  device_id: number;
  job_type: string;
  status: string;
  requested_by: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
}

export interface Backup {
  id: number;
  job_id: string | null;
  device_id: number;
  backup_type: string;
  storage_path: string;
  checksum: string;
  created_at: string | null;
}

export interface Approval {
  id: string;
  device_id: number;
  backup_job_id: string | null;
  requested_by: string;
  approved_by: string | null;
  status: string;
  config_lines: string[] | null;
  config_parents: string[] | null;
  created_at: string | null;
  approved_at: string | null;
}

export interface AuditEvent {
  id: number;
  job_id: string | null;
  device_id: number | null;
  event_type: string;
  message: string | null;
  created_at: string | null;
}

export interface Os6PrecheckRequest {
  device_id: number;
  config_parents: string[];
  config_lines: string[];
}

export interface Os6PrecheckSubmitted {
  request_id: string;
  state: "queued";
  device_id: number;
  hostname: string;
}

export type PrecheckState = "queued" | "running" | "completed" | "failed";

export interface PrecheckCommandResult {
  command: string;
  type: string;
  verification_method: string;
  desired_state_present: boolean;
}

export interface Os6PrecheckResult {
  status: "no_change_required" | "pending_approval" | string;
  target_host: string | null;
  ready_for_approval: boolean;
  backup_required: boolean;
  dry_run: {
    would_change: boolean | null;
    verification_method: string | null;
    already_present: string[];
    proposed_changes: string[];
    command_results: PrecheckCommandResult[];
  };
  backup: { job_id: string | null; status: string | null; checksum: string | null; storage_path: string | null } | null;
  approval: { approval_id: string | null; status: string | null } | null;
}

export interface Os6PrecheckStatus {
  request_id: string;
  state: PrecheckState;
  result: Os6PrecheckResult | null;
  error: string | null;
}

export interface ApplySubmitted {
  approval_id: string;
  request_id: string;
  status: "applying";
  device_id: number;
  hostname: string;
}

export interface ApplyStatus {
  approval_id: string;
  request_id: string;
  approval_status: string;
  task_state: "queued" | "running" | "succeeded" | "failed";
  error: string | null;
}
