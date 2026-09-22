import { useEffect, useMemo, useState } from "react";
import { getDevices } from "../api/client";
import { OS6_PLATFORM } from "../api/constants";
import type { Device } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";

const ALL_PLATFORMS = "all";

export function Devices() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [platformFilter, setPlatformFilter] = useState(ALL_PLATFORMS);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const data = await getDevices();
        if (cancelled) return;
        setDevices(data);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load devices");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => {
      cancelled = true;
    };
  }, []);

  const platforms = useMemo(
    () => Array.from(new Set(devices.map((device) => device.platform))).sort(),
    [devices],
  );

  const filteredDevices = useMemo(() => {
    const query = search.trim().toLowerCase();

    return devices.filter((device) => {
      const matchesPlatform =
        platformFilter === ALL_PLATFORMS || device.platform === platformFilter;

      const matchesSearch =
        query.length === 0 ||
        device.hostname.toLowerCase().includes(query) ||
        device.management_ip.toLowerCase().includes(query);

      return matchesPlatform && matchesSearch;
    });
  }, [devices, search, platformFilter]);

  return (
    <>
      <h1 className="page-title">Devices</h1>
      <p className="page-subtitle">Device inventory and access level.</p>

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
                    <td>
                      <StatusBadge status="unknown" />
                    </td>
                  </tr>
                ))}
                {filteredDevices.length === 0 && (
                  <tr>
                    <td colSpan={6} className="empty-state">
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
