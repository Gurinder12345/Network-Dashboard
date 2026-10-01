export type HealthStatus = "healthy" | "degraded" | "down" | "unknown";

export interface Device {
  id: number;
  hostname: string;
  management_ip: string;
  platform: string;
  enabled: boolean;
  // Current health (Redis cache, PostgreSQL fallback). "unknown" until the first check.
  health_status: HealthStatus;
  last_check_at: string | null;
  last_success_at: string | null;
  response_time_ms: number | null;
  tcp_reachable: boolean | null;
  ssh_reachable: boolean | null;
  cli_reachable: boolean | null;
  last_error: string | null;
}

export interface DeviceHealthEntry {
  device_id: number;
  hostname: string;
  management_ip: string;
  platform: string;
  enabled: boolean;
  status: HealthStatus;
  last_check_at: string | null;
  last_success_at: string | null;
  response_time_ms: number | null;
  tcp_reachable: boolean | null;
  ssh_reachable: boolean | null;
  cli_reachable: boolean | null;
  last_error: string | null;
  consecutive_failures: number;
  last_status_change_at: string | null;
}

export interface FleetHealth {
  total: number;
  healthy: number;
  degraded: number;
  down: number;
  unknown: number;
  last_updated: string | null;
  check_running: boolean;
  devices: DeviceHealthEntry[];
}

export interface HealthCheckRequested {
  status: "queued";
  request_id: string;
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
  /** Backup produced by this job (backup jobs only). */
  backup_id?: number | null;
}

/** GET /api/v1/jobs/{id}: status plus the backup it produced (no filesystem path). */
export interface JobDetail extends Omit<Job, "backup_id"> {
  hostname: string | null;
  backup: {
    backup_id: number;
    device_id: number;
    filename: string;
    created_at: string | null;
    file_available: boolean;
  } | null;
}

/** 202 from POST /api/v1/devices/{id}/backup. */
export interface BackupRequested {
  status: "queued";
  device_id: number;
  hostname: string;
  job_id: string;
  request_id: string;
  message: string;
}

export interface Backup {
  id: number;
  job_id: string | null;
  device_id: number;
  backup_type: string;
  storage_path: string;
  checksum: string;
  created_at: string | null;
  /** Type of the job that produced it: manual_backup or config_backup (pre-change). */
  job_type?: string | null;
  hostname?: string | null;
  /** The stored file exists and is readable by the API. */
  file_available?: boolean;
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
  cancelled_by: string | null;
  cancelled_at: string | null;
  cancellation_reason: string | null;
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

export interface TopologyNode {
  id: string;
  device_id: number | null;
  hostname: string;
  management_ip: string | null;
  platform: string | null;
  managed: boolean;
  enabled?: boolean;
  health_status: HealthStatus;
  response_time_ms: number | null;
  last_check_at?: string | null;
  last_error?: string | null;
  neighbor_count: number;
  topology_last_seen_at: string | null;
  topology_last_attempt_at?: string | null;
  topology_last_success_at?: string | null;
  topology_last_error?: string | null;
  // Unmanaged-only fields (as advertised over LLDP)
  advertised_system_name?: string | null;
  chassis_id?: string | null;
  remote_port_ids?: string[];
  attached_device_ids?: number[];
}

export interface TopologyLink {
  id: string;
  source: string;
  target: string;
  source_interface: string | null;
  target_interface: string | null;
  target_port_id?: string | null;
  protocol: string;
  first_seen_at: string;
  last_seen_at: string;
  active: boolean;
  observed_bidirectionally: boolean;
  relationship: "managed" | "unmanaged";
}

export interface TopologyRunSummary {
  checked: number;
  successful: number;
  failed: number;
  finished_at: string;
  failed_devices: { device_id: number; hostname: string; error: string | null }[];
}

export interface TopologyGraph {
  last_discovery_at: string | null;
  last_attempt_at: string | null;
  managed_devices: number;
  active_links: number;
  unmanaged_neighbors: number;
  failing_devices: number;
  down_devices: number;
  discovery_running: boolean;
  last_run: TopologyRunSummary | null;
  nodes: TopologyNode[];
  links: TopologyLink[];
}

// ---- Device detail + telemetry (read-only; telemetry never changes health) ----------
export type TelemetryStatus = "success" | "partial" | "failed" | "not_collected";

export interface DeviceTelemetry {
  device_id: number;
  hostname: string;
  status: TelemetryStatus;
  cpu_percent: number | null;
  memory_percent: number | null;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
  uptime_seconds: number | null;
  collected_at: string | null;
  last_success_at: string | null;
  stale: boolean;
  error: string | null;
  interval_seconds: number;
  stale_after_seconds: number;
}

export interface DeviceHealthSummary {
  status: HealthStatus;
  last_check_at: string | null;
  last_success_at: string | null;
  response_time_ms: number | null;
  tcp_reachable: boolean | null;
  ssh_reachable: boolean | null;
  cli_reachable: boolean | null;
  last_error: string | null;
}

export interface DeviceDetail {
  id: number;
  hostname: string;
  management_ip: string;
  platform: string;
  enabled: boolean;
  site: string | null;
  role: string | null;
  health: DeviceHealthSummary;
  telemetry: DeviceTelemetry;
  latest_backup: {
    backup_id: number;
    created_at: string | null;
    source: string | null;
    filename: string;
    file_available: boolean;
  } | null;
}

export type MetricsRange = "1h" | "6h" | "24h" | "7d";

export interface MetricSample {
  collected_at: string;
  cpu_percent: number | null;
  memory_percent: number | null;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
  status: string | null;
}

export interface MetricsHistory {
  device_id: number;
  range: MetricsRange;
  interval_seconds: number;
  bucket_seconds: number | null;
  downsampled: boolean;
  samples: MetricSample[];
}

// ---- PCAP Analyzer (structured, payload-free results) -------------------------------
export type PcapStatus = "queued" | "validating" | "extracting" | "analyzing" | "correlating" | "completed" | "failed";
export type PcapMode = "single" | "dual";
export type Severity = "critical" | "warning" | "info";

export interface PcapFinding {
  id: string;
  severity: Severity;
  category: string;
  cause: string | null;
  strength: "strong" | "supporting" | null;
  title: string;
  observed: string;
  evidence: Record<string, unknown>;
  interpretation: string;
  confidence: "high" | "medium" | "low";
  flows: string[];
  capture?: "client" | "server" | "correlation";
}

export interface PcapEndpoint {
  ip: string;
  port: number | null;
}

export interface PcapFlow {
  id: string;
  protocol: "tcp" | "udp";
  stream: number;
  client: PcapEndpoint;
  server: PcapEndpoint;
  client_inferred_by: string;
  key: string;
  start_s: number;
  duration_s: number;
  packets: number;
  bytes: number;
  client_to_server: { packets: number; bytes: number; payload_bytes?: number };
  server_to_client: { packets: number; bytes: number; payload_bytes?: number };
  handshake?: {
    state: string;
    syn_packets: number;
    syn_to_synack_ms: number | null;
    synack_to_ack_ms: number | null;
    rtt_ms: number | null;
  };
  tcp?: Record<string, number | Array<{ at_s: number; from: string }> | Record<string, number>>;
  rtt?: { count: number; min_ms: number; median_ms: number; max_ms: number } | null;
  timing?: {
    request_to_first_response_ms: number | null;
    first_request_at_s: number | null;
    first_response_at_s: number | null;
    server_spoke_first: boolean;
    retransmissions_before_response: number | null;
  };
  tls?: {
    sni: string | null;
    offered_versions: string[];
    negotiated_version: string | null;
    client_hello_to_server_hello_ms: number | null;
    alerts: Array<{ at_s: number; from: string; level: string | null; description: string }>;
  } | null;
  dns_transactions?: number;
  correlation?: {
    matched_by: string;
    delivered_client_to_server: number;
    delivered_server_to_client: number;
    missing_at_server: number;
    missing_at_client: number;
    server_side_response_ms: number | null;
    client_side_response_ms: number | null;
    network_share_of_response_ms: number | null;
  };
  issues: string[];
}

export interface PcapCaptureSummary {
  packets: number;
  bytes: number;
  duration_s: number;
  flows: number;
  tcp_streams: number;
  udp_flows: number;
  dns_queries: number;
  tls_sessions: number;
  icmp_events: number;
}

export interface PcapCaptureMetadata {
  file_type: string | null;
  encapsulation: string | null;
  packets: number | null;
  size_bytes: number;
  duration_s: number | null;
  first_packet: string | null;
  last_packet: string | null;
  avg_packet_rate: number | null;
  avg_byte_rate: number | null;
}

export interface PcapAssessment {
  label: "likely" | "possible" | "multiple" | "none";
  cause: string | null;
  issue: string | null;
  summary: string;
  strong_evidence: Array<{ finding: string; text: string }>;
  supporting_evidence: Array<{ finding: string; text: string }>;
  no_evidence_for: string[];
  candidates?: Array<{ cause: string; issue: string; strong_evidence: Array<{ finding: string; text: string }> }>;
}

export interface PcapResult {
  version: number;
  mode: PcapMode;
  observations: { captures: Record<string, { metadata: PcapCaptureMetadata; summary: PcapCaptureSummary }> };
  metrics: Record<string, { tcp: Record<string, unknown>; dns: Record<string, unknown> }>;
  flows: PcapFlow[];
  flow_total: number;
  dns: { transactions: Array<{ at_s: number; name: string | null; type: string | null; server: string; answered: boolean;
                               rcode: string | null; latency_ms: number | null; repeats: number }> };
  tls: { sessions: Array<{ flow: string; key: string; sni: string | null; negotiated_version: string | null;
                           client_hello_to_server_hello_ms: number | null; alerts: Array<{ description: string; from: string }> }> };
  icmp: { events: Array<{ at_s: number; name: string; detail: string | null; from: string; to: string; mtu: number | null }> };
  correlation: {
    matched_flows: number;
    only_client_flows: number;
    only_server_flows: number;
    clock: { estimated_clock_offset_ms: number | null; confidence: string; path_rtt_between_capture_points_ms?: number | null;
             forward_samples: number; reverse_samples: number };
  } | null;
  findings: PcapFinding[];
  assessment: PcapAssessment;
  recommendations: Array<{ cause: string; text: string; findings: string[] }>;
  performance?: { analysis_ms: number; result_bytes: number };
}

export interface PcapAnalysis {
  id: string;
  status: PcapStatus;
  mode: PcapMode;
  created_at: string;
  completed_at: string | null;
  client_filename: string;
  server_filename: string | null;
  client_size_bytes: number;
  server_size_bytes: number | null;
  capture_duration_seconds: number | null;
  packet_count: number | null;
  flow_count: number | null;
  likely_issue: string | null;
  finding_counts: Record<Severity, number> | null;
  error: string | null;
  expires_at: string;
  files_deleted_at: string | null;
  result?: PcapResult | null;
}
