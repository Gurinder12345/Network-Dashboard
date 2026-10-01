import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { getBackups, getDeviceDetail, getDeviceMetrics, getJobs } from "../api/client";
import type { Backup, DeviceDetail as Detail, DeviceTelemetry, Job, MetricSample, MetricsHistory, MetricsRange } from "../api/types";
import { BackupDownloadButton } from "../components/BackupDownloadButton";
import { BackupNotice } from "../components/BackupNotice";
import { Banner, EmptyState, StaleDataWarning, TableSkeleton } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { KpiCard } from "../components/KpiCard";
import { MetricChart } from "../components/MetricChart";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { isActive, useDeviceBackups } from "../hooks/useDeviceBackups";
import {
  formatDuration,
  formatMegabytes,
  formatPercent,
  formatRelative,
  formatResponseTime,
  formatUptime,
  humanize,
  platformLabel,
} from "../utils/format";
import { LEVEL_TONE, METRIC_THRESHOLDS, metricLevel, type MetricKind } from "../utils/thresholds";

const DETAIL_POLL_MS = 20000; // latest telemetry + health
const HISTORY_POLL_MS = 60000; // chart data: on range change and once a minute

const RANGES: { value: MetricsRange; label: string; seconds: number }[] = [
  { value: "1h", label: "1H", seconds: 3600 },
  { value: "6h", label: "6H", seconds: 6 * 3600 },
  { value: "24h", label: "24H", seconds: 24 * 3600 },
  { value: "7d", label: "7D", seconds: 7 * 24 * 3600 },
];

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "interfaces", label: "Interfaces" },
  { id: "vlans", label: "VLANs" },
  { id: "stp", label: "STP" },
  { id: "backups", label: "Backups" },
  { id: "jobs", label: "Jobs" },
] as const;

type TabId = (typeof TABS)[number]["id"];

const PLANNED: Record<string, string> = {
  interfaces: "Interface detail integration is planned.",
  vlans: "VLAN detail integration is planned.",
  stp: "Spanning-tree detail integration is planned.",
};

function sourceLabel(jobType: string | null | undefined) {
  if (jobType === "manual_backup") return "Manual";
  if (jobType === "config_backup") return "Pre-change";
  return jobType ? humanize(jobType) : "—";
}

function MetricCard({ kind, label, value, extra }: { kind: MetricKind; label: string; value: number | null; extra?: string }) {
  const level = metricLevel(kind, value);
  const hint = [level ? humanize(level) : "No current value", extra].filter(Boolean).join(" · ");
  return <KpiCard label={label} value={formatPercent(value)} hint={hint} tone={level ? LEVEL_TONE[level] : undefined} dim={value === null} />;
}

/** One line per telemetry state; telemetry problems never imply the switch is down. */
function TelemetryNotice({ telemetry }: { telemetry: DeviceTelemetry }) {
  if (telemetry.status === "not_collected") {
    return (
      <Banner tone="info" title="No telemetry collected yet.">
        CPU and memory are collected by the worker (every {telemetry.interval_seconds} s once enabled); this page only reads stored data.
      </Banner>
    );
  }
  if (telemetry.status === "failed") {
    return (
      <Banner tone="warning" title={`Last telemetry collection failed: ${telemetry.error ?? "unknown error"}.`}>
        {telemetry.last_success_at
          ? `Last successful sample ${formatRelative(telemetry.last_success_at)}. History below is kept.`
          : "No successful sample yet."}{" "}
        Device health is tracked separately.
      </Banner>
    );
  }
  if (telemetry.stale) {
    return (
      <Banner tone="warning" title="Telemetry stale.">
        No sample since {formatRelative(telemetry.last_success_at)} (expected every {telemetry.interval_seconds} s). This does not
        change device health.
      </Banner>
    );
  }
  if (telemetry.status === "partial") {
    const other = telemetry.cpu_percent === null ? "memory" : "CPU";
    return <Banner tone="info" title={`${telemetry.error ?? "One metric unavailable"}; ${other} data is current.`} />;
  }
  return null;
}

export default function DeviceDetail() {
  const rawId = useParams().deviceId ?? "";
  const deviceId = Number(rawId);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const tab: TabId = TABS.some((t) => t.id === requestedTab) ? (requestedTab as TabId) : "overview";

  const [detail, setDetail] = useState<Detail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const [range, setRange] = useState<MetricsRange>("24h");
  const [history, setHistory] = useState<{ data: MetricsHistory; endMs: number } | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const [backups, setBackups] = useState<Backup[] | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);

  const validId = Number.isInteger(deviceId) && deviceId > 0;

  const loadDetail = useCallback(async () => {
    if (!validId) return;
    try {
      setDetail(await getDeviceDetail(deviceId));
      setError(null);
      setNotFound(false);
      setLastRefreshedAt(new Date());
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load device";
      if (/not found/i.test(message)) setNotFound(true);
      else setError(message);
    }
  }, [deviceId, validId]);

  const loadHistory = useCallback(async () => {
    if (!validId) return;
    try {
      const data = await getDeviceMetrics(deviceId, range);
      setHistory({ data, endMs: Date.now() });
      setHistoryError(null);
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : "Failed to load telemetry history");
    }
  }, [deviceId, range, validId]);

  // Backups/jobs come from the existing list APIs, filtered to this device.
  const loadRecords = useCallback(async () => {
    try {
      const [allBackups, allJobs] = await Promise.all([getBackups(), getJobs()]);
      setBackups(allBackups.filter((b) => b.device_id === deviceId));
      setJobs(allJobs.filter((j) => j.device_id === deviceId));
    } catch {
      // Tabs show their own empty/loading state; the overview does not depend on these.
    }
  }, [deviceId]);

  const afterBackup = useCallback(() => {
    void loadDetail();
    void loadRecords();
  }, [loadDetail, loadRecords]);

  const { runs, start, lastRun, dismiss } = useDeviceBackups(afterBackup);
  const run = runs[deviceId];

  useEffect(() => {
    setDetail(null);
    setHistory(null);
    void loadDetail();
    void loadRecords();
  }, [loadDetail, loadRecords]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  usePolling(loadDetail, DETAIL_POLL_MS);
  usePolling(loadHistory, HISTORY_POLL_MS);

  function refresh() {
    void loadDetail();
    void loadHistory();
    void loadRecords();
  }

  const step = history ? history.data.bucket_seconds ?? history.data.interval_seconds : 60;
  const windowSeconds = RANGES.find((r) => r.value === range)!.seconds;
  const memoryDetail = useCallback(
    (s: MetricSample) =>
      s.memory_used_mb !== null && s.memory_total_mb !== null
        ? `${formatMegabytes(s.memory_used_mb)} / ${formatMegabytes(s.memory_total_mb)}`
        : null,
    [],
  );
  const tableRows = useMemo(() => (history ? [...history.data.samples].reverse() : []), [history]);

  if (!validId || notFound) {
    return (
      <>
        <Link className="back-link" to="/devices">← Devices</Link>
        <Banner tone="danger" title="Device not found.">
          No managed device has ID {rawId}.
        </Banner>
      </>
    );
  }

  if (!detail) {
    return (
      <>
        <Link className="back-link" to="/devices">← Devices</Link>
        {error ? (
          <Banner tone="danger" title="Failed to load device.">{error}</Banner>
        ) : (
          <TableSkeleton rows={4} columns={4} />
        )}
      </>
    );
  }

  const { health, telemetry } = detail;
  const latest = detail.latest_backup;
  const backupActive = isActive(run);

  const backupActions = (
    <div className="detail-actions">
      <button
        type="button"
        className="secondary-button"
        disabled={backupActive || !detail.enabled}
        aria-busy={backupActive}
        title={detail.enabled ? "Read-only running-config backup; no approval needed" : "Device is disabled"}
        onClick={() => start(detail.id, detail.hostname)}
      >
        {backupActive ? "Backing up…" : "Backup Now"}
      </button>
      {latest?.file_available && (
        <BackupDownloadButton
          backupId={latest.backup_id}
          label="Download latest"
          className="secondary-button"
          ariaLabel={`Download latest backup of ${detail.hostname}`}
        />
      )}
    </div>
  );

  return (
    <>
      <Link className="back-link" to="/devices">← Devices</Link>

      <header className="detail-header">
        <div className="detail-title">
          <h1>{detail.hostname}</h1>
          <div className="detail-sub">
            <span className="mono">{detail.management_ip}</span> · {platformLabel(detail.platform)}
            {!detail.enabled && " · Disabled"}
          </div>
          <StatusBadge status={health.status} title={health.last_error ?? undefined} />
        </div>
        <div className="detail-header-actions">
          <span className="last-refreshed">
            {lastRefreshedAt ? `Updated ${lastRefreshedAt.toLocaleTimeString()}` : ""}
          </span>
          <button type="button" className="refresh-button" onClick={refresh} title="Re-read stored data; does not poll the switch">
            Refresh
          </button>
          {backupActions}
        </div>
      </header>

      {error && <StaleDataWarning since={lastRefreshedAt} error={error} />}
      {lastRun && <BackupNotice run={lastRun} onDismiss={dismiss} />}

      <div className="detail-tabs" role="tablist" aria-label="Device sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            className="detail-tab"
            onClick={() => setSearchParams(t.id === "overview" ? {} : { tab: t.id }, { replace: true })}
          >
            {t.label}
          </button>
        ))}
      </div>

      <section role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "overview" && (
          <>
            <TelemetryNotice telemetry={telemetry} />

            <div className="kpi-grid detail-kpis">
              <MetricCard kind="cpu" label="CPU" value={telemetry.cpu_percent} />
              <MetricCard
                kind="memory"
                label="Memory"
                value={telemetry.memory_percent}
                extra={
                  telemetry.memory_used_mb !== null && telemetry.memory_total_mb !== null
                    ? `${formatMegabytes(telemetry.memory_used_mb)} / ${formatMegabytes(telemetry.memory_total_mb)}`
                    : undefined
                }
              />
              <KpiCard
                label="Response"
                value={formatResponseTime(health.response_time_ms)}
                hint={health.last_check_at ? `Health check ${formatRelative(health.last_check_at)}` : "Not checked yet"}
                dim={health.response_time_ms === null}
              />
              <KpiCard
                label="Uptime"
                value={formatUptime(telemetry.uptime_seconds)}
                hint={telemetry.uptime_seconds === null ? "Unavailable" : "Reported by the switch"}
                dim={telemetry.uptime_seconds === null}
              />
            </div>

            <div className="telemetry-meta">
              {telemetry.collected_at ? (
                <>
                  Telemetry updated <RelativeTime value={telemetry.collected_at} />
                  {telemetry.stale && <StatusBadge status="warning" label="telemetry stale" />}
                </>
              ) : (
                <span className="muted">No telemetry collected yet.</span>
              )}
              <span className="muted"> · Thresholds are display-only and never change device health.</span>
            </div>

            <div className="filters-bar">
              <FilterChips
                label="Range"
                value={range}
                onChange={(value) => setRange(value as MetricsRange)}
                options={RANGES.map((r) => ({ value: r.value, label: r.label }))}
              />
              {history?.data.downsampled && (
                <span className="muted small-note">7D shows 10-minute averages.</span>
              )}
            </div>

            {historyError && <Banner tone="warning" title="Telemetry history unavailable.">{historyError}</Banner>}

            <div className="chart-grid">
              {(["cpu", "memory"] as const).map((kind) => (
                <div className="panel" key={kind}>
                  <div className="panel-header">
                    <h2>{kind === "cpu" ? "CPU Usage" : "Memory Usage"}</h2>
                    <span className="panel-header-meta">{history ? `${history.data.samples.length} samples` : "Loading…"}</span>
                  </div>
                  <div className="chart-body">
                    {history ? (
                      <MetricChart
                        label={kind === "cpu" ? "CPU" : "Memory"}
                        valueKey={kind === "cpu" ? "cpu_percent" : "memory_percent"}
                        samples={history.data.samples}
                        windowSeconds={windowSeconds}
                        stepSeconds={step}
                        endMs={history.endMs}
                        thresholds={METRIC_THRESHOLDS[kind]}
                        detail={kind === "memory" ? memoryDetail : undefined}
                      />
                    ) : (
                      <TableSkeleton rows={3} columns={1} />
                    )}
                  </div>
                </div>
              ))}
            </div>

            {history && history.data.samples.length > 0 && (
              <details className="chart-table">
                <summary>Show data table ({history.data.samples.length} samples)</summary>
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>CPU</th>
                        <th>Memory</th>
                        <th>Memory used</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tableRows.map((s) => (
                        <tr key={s.collected_at}>
                          <td className="mono">{new Date(s.collected_at).toLocaleString()}</td>
                          <td className="cell-num">{formatPercent(s.cpu_percent)}</td>
                          <td className="cell-num">{formatPercent(s.memory_percent)}</td>
                          <td className="cell-num">{memoryDetail(s) ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}

            <div className="panel detail-health">
              <div className="panel-header">
                <h2>Health</h2>
                <span className="panel-header-meta">Connectivity / CLI check (independent of telemetry)</span>
              </div>
              <dl className="detail-list">
                <dt>Status</dt>
                <dd><StatusBadge status={health.status} /></dd>
                <dt>Last check</dt>
                <dd><RelativeTime value={health.last_check_at} /></dd>
                <dt>Last success</dt>
                <dd><RelativeTime value={health.last_success_at} /></dd>
                <dt>Reachability</dt>
                <dd>
                  {health.tcp_reachable === null
                    ? "—"
                    : `TCP ${health.tcp_reachable ? "ok" : "fail"} · SSH ${health.ssh_reachable ? "ok" : "fail"} · CLI ${health.cli_reachable ? "ok" : "fail"}`}
                </dd>
                <dt>Last error</dt>
                <dd className="mono">{health.last_error ?? "—"}</dd>
                <dt>Last backup</dt>
                <dd>{latest ? <RelativeTime value={latest.created_at} /> : "Never"}</dd>
              </dl>
            </div>
          </>
        )}

        {tab in PLANNED && (
          <div className="panel">
            <EmptyState title={PLANNED[tab]} hint="No data is shown here until a read-only API exists for it." />
          </div>
        )}

        {tab === "backups" && (
          <div className="panel">
            <div className="panel-header">
              <h2>Backups</h2>
              <span className="count-tag">{backups?.length ?? 0}</span>
              {backupActions}
            </div>
            <div className="table-wrap">
              {backups === null ? (
                <TableSkeleton rows={4} columns={4} />
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Created</th>
                      <th>Source</th>
                      <th>Status</th>
                      <th>Download</th>
                    </tr>
                  </thead>
                  <tbody>
                    {backups.map((b) => {
                      const available = b.file_available !== false;
                      return (
                        <tr key={b.id}>
                          <td>
                            <RelativeTime value={b.created_at} />
                            <span className="cell-sub mono">backup #{b.id}</span>
                          </td>
                          <td className="secondary">{sourceLabel(b.job_type)}</td>
                          <td>
                            {available ? <StatusBadge status="success" label="stored" /> : <StatusBadge status="failed" label="file missing" />}
                          </td>
                          <td>
                            {available ? (
                              <BackupDownloadButton backupId={b.id} ariaLabel={`Download backup ${b.id} of ${detail.hostname}`} />
                            ) : (
                              <span className="muted">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                    {backups.length === 0 && (
                      <tr>
                        <td colSpan={4}>
                          <EmptyState title="No backups for this device yet." hint="Use Backup Now above." />
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        )}

        {tab === "jobs" && (
          <div className="panel">
            <div className="panel-header">
              <h2>Jobs</h2>
              <span className="count-tag">{jobs?.length ?? 0}</span>
            </div>
            <div className="table-wrap">
              {jobs === null ? (
                <TableSkeleton rows={4} columns={5} />
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Status</th>
                      <th>Started</th>
                      <th>Duration</th>
                      <th>Result / error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.map((j) => (
                      <tr key={j.id} className={j.status === "failed" ? "row-alert" : undefined}>
                        <td>
                          {humanize(j.job_type)}
                          <span className="cell-sub mono">{j.job_type}</span>
                        </td>
                        <td>
                          <StatusBadge status={j.status} />
                        </td>
                        <td>
                          <RelativeTime value={j.started_at} />
                        </td>
                        <td className="duration">
                          {j.finished_at ? formatDuration(j.started_at, j.finished_at) : <span className="muted">running</span>}
                        </td>
                        <td className="mono">{j.error_message ?? (j.backup_id ? `backup #${j.backup_id}` : "—")}</td>
                      </tr>
                    ))}
                    {jobs.length === 0 && (
                      <tr>
                        <td colSpan={5}>
                          <EmptyState title="No jobs for this device yet." />
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        )}
      </section>
    </>
  );
}
