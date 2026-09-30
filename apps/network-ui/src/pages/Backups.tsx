import { useEffect, useMemo, useState } from "react";
import { getBackups, getDevices } from "../api/client";
import type { Backup, Device } from "../api/types";
import { BackupDownloadButton } from "../components/BackupDownloadButton";
import { CopyButton } from "../components/CopyButton";
import { Banner, EmptyState, TableSkeleton } from "../components/Feedback";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { humanize, truncateId } from "../utils/format";

const ALL_DEVICES = "all";

// Which workflow produced the backup (from its job).
function sourceLabel(jobType: string | null | undefined): string {
  if (jobType === "manual_backup") return "Manual";
  if (jobType === "config_backup") return "Pre-change";
  return jobType ? humanize(jobType) : "—";
}

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
      <PageHeader
        title="Backups"
        subtitle="Stored running-configuration backups. Downloads are recorded in the audit log."
      />

      {error && (
        <Banner tone="danger" title="Failed to load backups.">
          {error}
        </Banner>
      )}

      <div className="filters-bar">
        <input
          type="search"
          className="search-input"
          placeholder="Search device, path, or checksum…"
          aria-label="Search backups"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="filter-select"
          aria-label="Device"
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
        <div className="table-wrap">
          {loading ? (
            <TableSkeleton rows={8} columns={9} />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Device</th>
                  <th>Type</th>
                  <th>Source</th>
                  <th>Created</th>
                  <th>Status</th>
                  <th>Checksum (SHA-256)</th>
                  <th>Job ID</th>
                  <th>Download</th>
                  <th>Storage Path</th>
                </tr>
              </thead>
              <tbody>
                {filteredBackups.map((backup) => {
                  // Older APIs do not report availability; assume the file exists then.
                  const available = backup.file_available !== false;
                  const hostname = deviceHostnameById.get(backup.device_id) ?? backup.hostname ?? `Device #${backup.device_id}`;
                  return (
                  <tr key={backup.id}>
                    <td className="cell-primary">
                      {hostname}
                      <span className="cell-sub mono">backup #{backup.id}</span>
                    </td>
                    <td>
                      <span className="tag tag-plain">{humanize(backup.backup_type)}</span>
                    </td>
                    <td className="secondary">{sourceLabel(backup.job_type)}</td>
                    <td>
                      <RelativeTime value={backup.created_at} />
                    </td>
                    <td>
                      {available ? (
                        <StatusBadge status="success" label="stored" />
                      ) : (
                        <StatusBadge status="failed" label="file missing" title="The backup record exists but its file is not readable under /backups." />
                      )}
                    </td>
                    <td>
                      <ChecksumCell checksum={backup.checksum} />
                    </td>
                    <td className="mono muted" title={backup.job_id ?? undefined}>
                      {truncateId(backup.job_id)}
                    </td>
                    <td>
                      {available ? (
                        <BackupDownloadButton backupId={backup.id} ariaLabel={`Download backup ${backup.id} of ${hostname}`} />
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <StoragePathCell path={backup.storage_path} />
                    </td>
                  </tr>
                  );
                })}
                {filteredBackups.length === 0 && (
                  <tr>
                    <td colSpan={9}>
                      <EmptyState
                        title={backups.length === 0 ? "No backups recorded yet." : "No backups match these filters."}
                        hint={backups.length === 0 ? "Use Backup Now on the Devices page, or run a precheck." : undefined}
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
