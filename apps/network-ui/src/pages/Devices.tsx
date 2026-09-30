import { useCallback, useEffect, useMemo, useState } from "react";
import { getDevices } from "../api/client";
import { OS6_PLATFORM } from "../api/constants";
import type { Device, HealthStatus } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { formatResponseTime, formatTime, truncateText } from "../utils/format";

const ALL_PLATFORMS = "all";
const ALL_HEALTH = "all";
const HEALTH_POLL_MS = 20000;
const HEALTH_FILTERS: { value: HealthStatus | typeof ALL_HEALTH; label: string }[] = [
  { value: ALL_HEALTH, label: "All health" },
  { value: "healthy", label: "Healthy" },
  { value: "degraded", label: "Degraded" },
  { value: "down", label: "Down" },
  { value: "unknown", label: "Unknown" },
];

function reachability(device: Device): string | null {
  if (device.tcp_reachable === null) return null;

  const mark = (ok: boolean | null) => (ok ? "ok" : "fail");
  return `TCP ${mark(device.tcp_reachable)} · SSH ${mark(device.ssh_reachable)} · CLI ${mark(device.cli_reachable)}`;
}

function HealthErrorCell({ device }: { device: Device }) {
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
      </pre>
    </details>
  );
}

export function Devices() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [platformFilter, setPlatformFilter] = useState(ALL_PLATFORMS);
  const [healthFilter, setHealthFilter] = useState<string>(ALL_HEALTH);
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

  useEffect(() => {
    load();
  }, [load]);

  usePolling(load, HEALTH_POLL_MS);

  const platforms = useMemo(
    () => Array.from(new Set(devices.map((device) => device.platform))).sort(),
    [devices],
  );

  const filteredDevices = useMemo(() => {
    const query = search.trim().toLowerCase();

    return devices.filter((device) => {
      const matchesPlatform =
        platformFilter === ALL_PLATFORMS || device.platform === platformFilter;

      const matchesHealth =
        healthFilter === ALL_HEALTH || (device.health_status ?? "unknown") === healthFilter;

      const matchesSearch =
        query.length === 0 ||
        device.hostname.toLowerCase().includes(query) ||
        device.management_ip.toLowerCase().includes(query);

      return matchesPlatform && matchesHealth && matchesSearch;
    });
  }, [devices, search, platformFilter, healthFilter]);

  return (
    <>
      <div className="page-toolbar">
        <div>
          <h1 className="page-title">Devices</h1>
          <p className="page-subtitle">Device inventory, access level and live health.</p>
        </div>
        <div className="refresh-control">
          <span className="last-refreshed">
            Updated: {lastRefreshedAt ? lastRefreshedAt.toLocaleTimeString() : "—"}
          </span>
          <button type="button" className="refresh-button" onClick={() => load()}>
            Refresh
          </button>
        </div>
      </div>

      {error && <div className="error-banner">Failed to load devices: {error}</div>}

      <div className="filters-bar">
        <input
          type="text"
          className="search-input"
          placeholder="Search hostname or IP..."
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="filter-select"
          value={platformFilter}
          onChange={(event) => setPlatformFilter(event.target.value)}
        >
          <option value={ALL_PLATFORMS}>All platforms</option>
          {platforms.map((platform) => (
            <option key={platform} value={platform}>
              {platform}
            </option>
          ))}
        </select>
        <select
          className="filter-select"
          value={healthFilter}
          onChange={(event) => setHealthFilter(event.target.value)}
        >
          {HEALTH_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Devices</h2>
          <span className="count-tag">{filteredDevices.length}</span>
        </div>
        <div className="panel-body" style={{ maxHeight: "none" }}>
          {loading ? (
            <div className="empty-state">Loading devices&hellip;</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Hostname</th>
                  <th>Management IP</th>
                  <th>Platform</th>
                  <th>Enabled</th>
                  <th>Access</th>
                  <th>Health</th>
                  <th>Response</th>
                  <th>Last Check</th>
                  <th>Issue</th>
                </tr>
              </thead>
              <tbody>
                {filteredDevices.map((device) => (
                  <tr key={device.id}>
                    <td>{device.hostname}</td>
                    <td className="mono">{device.management_ip}</td>
                    <td>
                      <span className="platform-tag">{device.platform}</span>
                    </td>
                    <td>{device.enabled ? "Yes" : "No"}</td>
                    <td>
                      {device.platform === OS6_PLATFORM ? (
                        "Actionable"
                      ) : (
                        <span className="view-only-tag">View only</span>
                      )}
                    </td>
                    <td title={reachability(device) ?? undefined}>
                      <StatusBadge status={device.health_status ?? "unknown"} />
                    </td>
                    <td className="mono">{formatResponseTime(device.response_time_ms)}</td>
                    <td className="mono" title={device.last_check_at ?? undefined}>
                      {formatTime(device.last_check_at)}
                    </td>
                    <td>
                      <HealthErrorCell device={device} />
                    </td>
                  </tr>
                ))}
                {filteredDevices.length === 0 && (
                  <tr>
                    <td colSpan={9} className="empty-state">
                      {devices.length === 0
                        ? "No devices found."
                        : "No devices match your search or filter."}
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
