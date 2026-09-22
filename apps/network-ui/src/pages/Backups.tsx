import { useEffect, useMemo, useState } from "react";
import { getBackups, getDevices } from "../api/client";
import type { Backup, Device } from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { formatTimestamp, truncateId } from "../utils/format";

const ALL_DEVICES = "all";

function StoragePathCell({ path }: { path: string }) {
  const lastSlash = path.lastIndexOf("/");
  const dir = lastSlash >= 0 ? path.slice(0, lastSlash + 1) : "";
  const filename = lastSlash >= 0 ? path.slice(lastSlash + 1) : path;

  return (
    <div className="path-cell">
      <div className="path-inner">
        <div className="path-dir mono" title={path}>
          {dir}
        </div>
        <div className="path-file mono">{filename}</div>
      </div>
      <CopyButton value={path} />
    </div>
  );
}

function ChecksumCell({ checksum }: { checksum: string }) {
  const preview =
    checksum.length > 16 ? `${checksum.slice(0, 8)}…${checksum.slice(-6)}` : checksum;

  return (
    <div className="checksum-cell">
      <span className="mono" title={checksum}>
        {preview}
      </span>
      <CopyButton value={checksum} />
    </div>
  );
}

export function Backups() {
  const [backups, setBackups] = useState<Backup[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [deviceFilter, setDeviceFilter] = useState(ALL_DEVICES);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [backupsData, devicesData] = await Promise.all([
          getBackups(),
          getDevices(),
        ]);

        if (cancelled) return;

        setBackups(backupsData);
        setDevices(devicesData);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load backups");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => {
      cancelled = true;
    };
  }, []);

  const deviceHostnameById = useMemo(() => {
    const map = new Map<number, string>();
    devices.forEach((device) => map.set(device.id, device.hostname));
    return map;
  }, [devices]);

  const deviceOptions = useMemo(() => {
    const ids = Array.from(new Set(backups.map((backup) => backup.device_id)));

    return ids
      .map((id) => ({ id, hostname: deviceHostnameById.get(id) ?? `Device #${id}` }))
      .sort((a, b) => a.hostname.localeCompare(b.hostname));
  }, [backups, deviceHostnameById]);

  const filteredBackups = useMemo(() => {
    const query = search.trim().toLowerCase();

    return backups.filter((backup) => {
      const matchesDevice =
        deviceFilter === ALL_DEVICES || String(backup.device_id) === deviceFilter;

      if (!matchesDevice) return false;

      if (query.length === 0) return true;

      const hostname = deviceHostnameById.get(backup.device_id) ?? "";

      const haystack = [
        hostname,
        backup.backup_type,
        backup.storage_path,
        backup.checksum,
        backup.job_id ?? "",
      ]
        .join(" ")
        .toLowerCase();

      return haystack.includes(query);
    });
  }, [backups, search, deviceFilter, deviceHostnameById]);

  return (
    <>
      <h1 className="page-title">Backups</h1>
      <p className="page-subtitle">Stored running-configuration backups.</p>

      {error && <div className="error-banner">Failed to load backups: {error}</div>}

      <div className="filters-bar">
        <input
          type="text"
          className="search-input"
          placeholder="Search device, path, or checksum..."
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="filter-select"
          value={deviceFilter}
          onChange={(event) => setDeviceFilter(event.target.value)}
        >
          <option value={ALL_DEVICES}>All devices</option>
          {deviceOptions.map((device) => (
            <option key={device.id} value={String(device.id)}>
              {device.hostname}
            </option>
          ))}
        </select>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Backups</h2>
          <span className="count-tag">{filteredBackups.length}</span>
        </div>
        <div className="panel-body" style={{ maxHeight: "none" }}>
          {loading ? (
            <div className="empty-state">Loading backups&hellip;</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Device</th>
                  <th>Backup Type</th>
                  <th>Created</th>
                  <th>Storage Path</th>
                  <th>Checksum</th>
                  <th>Job ID</th>
                </tr>
              </thead>
              <tbody>
                {filteredBackups.map((backup) => (
                  <tr key={backup.id}>
                    <td>
                      {deviceHostnameById.get(backup.device_id) ??
                        `Device #${backup.device_id}`}
                    </td>
                    <td>{backup.backup_type}</td>
                    <td className="mono">{formatTimestamp(backup.created_at)}</td>
                    <td>
                      <StoragePathCell path={backup.storage_path} />
                    </td>
                    <td>
                      <ChecksumCell checksum={backup.checksum} />
                    </td>
                    <td className="mono" title={backup.job_id ?? undefined}>
                      {truncateId(backup.job_id)}
                    </td>
                  </tr>
                ))}
                {filteredBackups.length === 0 && (
                  <tr>
                    <td colSpan={6} className="empty-state">
                      {backups.length === 0
                        ? "No backups recorded yet."
                        : "No backups match your search or filter."}
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
