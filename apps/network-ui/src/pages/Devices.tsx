import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getBackups, getDevices } from "../api/client";
import { HEALTH_SLOW_THRESHOLD_MS, OS6_PLATFORM } from "../api/constants";
import type { Backup, Device, HealthStatus } from "../api/types";
import { BackupDownloadButton } from "../components/BackupDownloadButton";
import { Banner, EmptyState, StaleDataWarning, TableSkeleton } from "../components/Feedback";
import { BackupNotice } from "../components/BackupNotice";
import { isActive, useDeviceBackups, type BackupRun } from "../hooks/useDeviceBackups";
import { FilterChips } from "../components/FilterChips";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { formatResponseTime, truncateText } from "../utils/format";

const ALL = "all";
const HEALTH_POLL_MS = 20000;
const HEALTH_STATES: HealthStatus[] = ["healthy", "degraded", "down", "unknown"];

function reachability(device: Device): string | null {
  if (device.tcp_reachable === null) return null;

  const mark = (ok: boolean | null) => (ok ? "ok" : "fail");
  return `TCP ${mark(device.tcp_reachable)} · SSH ${mark(device.ssh_reachable)} · CLI ${mark(device.cli_reachable)}`;
}

/** Response time against the worker's slow threshold (the only threshold health defines). */
function ResponseCell({ ms }: { ms: number | null }) {
  if (ms === null || ms === undefined) return <span className="muted">—</span>;

  const slow = ms > HEALTH_SLOW_THRESHOLD_MS;
  const fill = Math.min(ms / HEALTH_SLOW_THRESHOLD_MS, 1) * 100;

  return (
    <span
      className="response-cell"
      title={`${ms} ms · slow threshold ${HEALTH_SLOW_THRESHOLD_MS / 1000} s (health check)`}
    >
      <span className="response-meter" aria-hidden="true">
        <span className={`response-meter-fill${slow ? " slow" : ""}`} style={{ width: `${fill}%` }} />
      </span>
      <span className="cell-num">{formatResponseTime(ms)}</span>
      {slow && <span className="response-label slow">slow</span>}
    </span>
  );
}

function HealthIssueCell({ device }: { device: Device }) {
  const status = device.health_status;

  if (!device.last_error || (status !== "degraded" && status !== "down")) {
    return <span className="muted">—</span>;
  }

  return (
    <details className="error-details">
      <summary className={`${status === "down" ? "error-summary" : "message-summary"} mono`}>
        {truncateText(device.last_error, 48)}
      </summary>
      <pre className="error-full">
        {device.last_error}
        {reachability(device) ? `\n\n${reachability(device)}` : ""}
        {device.last_success_at ? `\nLast success: ${new Date(device.last_success_at).toLocaleString()}` : ""}
      </pre>
    </details>
  );
}

interface BackupCellProps {
  device: Device;
  latest: Backup | undefined;
  run: BackupRun | undefined;
  onStart: (deviceId: number, hostname: string) => void;
}

/** Read-only running-config backup for one switch; independent of every other row. */
function BackupCell({ device, latest, run, onStart }: BackupCellProps) {
  const active = isActive(run);
  const justBackedUp = run?.state === "success" && run.backupId !== null && run.fileAvailable;

  return (
    <div className="backup-cell">
      <span className="backup-last">
        Last backup: {latest ? <RelativeTime value={latest.created_at} /> : <span className="muted">Never</span>}
      </span>
      <div className="backup-actions">
        <button
          type="button"
          className="secondary-button small-button"
          disabled={active || !device.enabled}
          aria-busy={active}
          title={device.enabled ? "Read-only running-config backup; no approval needed" : "Device is disabled"}
          aria-label={active ? `Backing up ${device.hostname}` : `Backup ${device.hostname} now`}
          onClick={() => onStart(device.id, device.hostname)}
        >
          {active ? "Backing up…" : "Backup Now"}
        </button>
        {justBackedUp ? (
          <BackupDownloadButton
            backupId={run.backupId!}
            label="Download"
            ariaLabel={`Download new backup of ${device.hostname}`}
          />
        ) : (
          latest && (
            <BackupDownloadButton
              backupId={latest.id}
              label="Download latest"
              ariaLabel={`Download latest backup of ${device.hostname}`}
            />
          )
        )}
      </div>
      {run?.state === "success" && <span className="backup-result ok">Backup completed</span>}
      {(run?.state === "failed" || run?.state === "lost") && (
        <details className="error-details">
          <summary className="error-summary">Backup failed</summary>
          <pre className="error-full">{run.error}</pre>
        </details>
      )}
    </div>
  );
}

export function Devices() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [platformFilter, setPlatformFilter] = useState(ALL);
  const [healthFilter, setHealthFilter] = useState<string>(ALL);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  // Re-reads inventory + stored health from the API; never starts switch checks.
  const load = useCallback(async () => {
    try {
      const data = await getDevices();
      setDevices(data);
      setError(null);
      setLastRefreshedAt(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load devices");
    } finally {
      setLoading(false);
    }
  }, []);

  // Backup metadata only (never file contents): used for "Last backup" and Download latest.
  const [backups, setBackups] = useState<Backup[]>([]);

  const loadBackups = useCallback(async () => {
    try {
      setBackups(await getBackups());
    } catch {
      // Not fatal for the Devices page; the backup column shows "Never" until it loads.
    }
  }, []);

  const { runs, start, lastRun, dismiss } = useDeviceBackups(loadBackups);

  useEffect(() => {
    load();
    loadBackups();
  }, [load, loadBackups]);

  usePolling(load, HEALTH_POLL_MS);

  const latestBackupByDevice = useMemo(() => {
    const latest = new Map<number, Backup>();
    backups.forEach((backup) => {
      if (backup.file_available === false) return;
      const current = latest.get(backup.device_id);
      if (!current || (backup.created_at ?? "") > (current.created_at ?? "")) latest.set(backup.device_id, backup);
    });
    return latest;
  }, [backups]);

  const platforms = useMemo(
    () => Array.from(new Set(devices.map((device) => device.platform))).sort(),
    [devices],
  );

  const healthCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    devices.forEach((d) => {
      const status = d.health_status ?? "unknown";
      counts[status] = (counts[status] ?? 0) + 1;
    });
    return counts;
  }, [devices]);

  const filteredDevices = useMemo(() => {
    const query = search.trim().toLowerCase();

    return devices.filter((device) => {
      const matchesPlatform = platformFilter === ALL || device.platform === platformFilter;
      const matchesHealth = healthFilter === ALL || (device.health_status ?? "unknown") === healthFilter;
      const matchesSearch =
        query.length === 0 ||
        device.hostname.toLowerCase().includes(query) ||
        device.management_ip.toLowerCase().includes(query);

      return matchesPlatform && matchesHealth && matchesSearch;
    });
  }, [devices, search, platformFilter, healthFilter]);

  return (
    <>
      <PageHeader
        title="Devices"
        subtitle="Inventory, access level and live health (refreshes every 20 s)."
        lastUpdated={lastRefreshedAt}
        actions={
          <button type="button" className="refresh-button" onClick={() => load()}>
            Refresh
          </button>
        }
      />

      {error && lastRefreshedAt && <StaleDataWarning since={lastRefreshedAt} error={error} />}
      {error && !lastRefreshedAt && (
        <Banner tone="danger" title="Failed to load devices.">
          {error}
        </Banner>
      )}

      {lastRun && <BackupNotice run={lastRun} onDismiss={dismiss} />}

      <div className="filters-bar">
        <input
          type="search"
          className="search-input"
          placeholder="Search hostname or IP…"
          aria-label="Search devices"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="filter-select"
          aria-label="Platform"
          value={platformFilter}
          onChange={(event) => setPlatformFilter(event.target.value)}
        >
          <option value={ALL}>All platforms</option>
          {platforms.map((platform) => (
            <option key={platform} value={platform}>
              {platform}
            </option>
          ))}
        </select>
        <FilterChips
          label="Health"
          value={healthFilter}
          onChange={setHealthFilter}
          options={[
            { value: ALL, label: "All", count: devices.length },
            ...HEALTH_STATES.map((status) => ({
              value: status,
              label: status.charAt(0).toUpperCase() + status.slice(1),
              count: healthCounts[status] ?? 0,
              status,
            })),
          ]}
        />
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Devices</h2>
          <span className="count-tag">{filteredDevices.length}</span>
        </div>
        <div className="table-wrap">
          {loading ? (
            <TableSkeleton rows={8} columns={9} />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Hostname</th>
                  <th>Management IP</th>
                  <th>Platform</th>
                  <th>Access</th>
                  <th>Health</th>
                  <th>Response</th>
                  <th>Last Check</th>
                  <th>Issue</th>
                  <th>Backup</th>
                </tr>
              </thead>
              <tbody>
                {filteredDevices.map((device) => {
                  const status = device.health_status ?? "unknown";
                  return (
                    <tr
                      key={device.id}
                      className={status === "down" ? "row-alert" : status === "degraded" ? "row-warn" : undefined}
                    >
                      <td className="cell-primary">
                        <Link className="device-link" to={`/devices/${device.id}`}>
                          {device.hostname}
                        </Link>
                        {!device.enabled && <span className="cell-sub">Disabled · not monitored</span>}
                      </td>
                      <td className="mono secondary">{device.management_ip}</td>
                      <td>
                        <span className="platform-tag">{device.platform}</span>
                      </td>
                      <td>
                        {device.platform === OS6_PLATFORM ? (
                          <span className="access-tag actionable">Actionable</span>
                        ) : (
                          <span className="view-only-tag">View only</span>
                        )}
                      </td>
                      <td title={reachability(device) ?? undefined}>
                        <StatusBadge status={status} />
                      </td>
                      <td>
                        <ResponseCell ms={device.response_time_ms} />
                      </td>
                      <td>
                        <RelativeTime value={device.last_check_at} />
                      </td>
                      <td>
                        <HealthIssueCell device={device} />
                      </td>
                      <td>
                        <BackupCell
                          device={device}
                          latest={latestBackupByDevice.get(device.id)}
                          run={runs[device.id]}
                          onStart={start}
                        />
                      </td>
                    </tr>
                  );
                })}
                {filteredDevices.length === 0 && (
                  <tr>
                    <td colSpan={9}>
                      <EmptyState
                        title={devices.length === 0 ? "No devices found." : "No devices match these filters."}
                        hint={devices.length === 0 ? undefined : "Clear the search or choose another filter."}
                      />
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}
