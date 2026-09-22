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
