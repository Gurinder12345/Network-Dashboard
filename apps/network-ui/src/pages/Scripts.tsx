import { useEffect, useMemo, useState } from "react";
import { getDevices } from "../api/client";
import { OS10_PLATFORM, OS6_PLATFORM } from "../api/constants";
import type { Device } from "../api/types";

// Parameters are typed and bounded — there is deliberately no free-text/command parameter type.
type ScriptParam =
  | { name: string; label: string; kind: "select"; options: string[]; default: string }
  | { name: string; label: string; kind: "number"; min: number; max: number; default: number | null }
  | { name: string; label: string; kind: "boolean"; default: boolean };

interface ApprovedScript {
  id: string;
  name: string;
  description: string;
  platforms: string[];
  params: ScriptParam[];
}

type ParamValue = string | number | boolean | null;

// Approved script catalog (CLAUDE.md §19). Fixed in the UI until the API serves the
// Git-managed list from apps/network-worker/scripts/. All entries are read-only audits.
const APPROVED_SCRIPTS: ApprovedScript[] = [
  {
    id: "interface_audit",
    name: "Interface Audit",
    description: "Report interface status, descriptions, and error counters.",
    platforms: [OS6_PLATFORM, OS10_PLATFORM],
    params: [
      { name: "scope", label: "Interface scope", kind: "select", options: ["all", "access", "trunk"], default: "all" },
      { name: "include_down", label: "Include down interfaces", kind: "boolean", default: true },
    ],
  },
  {
    id: "vlan_audit",
    name: "VLAN Audit",
    description: "List VLANs and their port membership.",
    platforms: [OS6_PLATFORM, OS10_PLATFORM],
    params: [{ name: "vlan_id", label: "VLAN ID (optional)", kind: "number", min: 1, max: 4094, default: null }],
  },
  {
    id: "config_validation",
    name: "Config Validation",
    description: "Check the running config against the platform baseline.",
    platforms: [OS6_PLATFORM],
    params: [{ name: "ruleset", label: "Ruleset", kind: "select", options: ["baseline"], default: "baseline" }],
  },
  {
    id: "backup_check",
    name: "Backup Check",
    description: "Verify a recent configuration backup exists for the device.",
    platforms: [OS6_PLATFORM],
    params: [{ name: "max_age_hours", label: "Max backup age (hours)", kind: "number", min: 1, max: 720, default: 24 }],
  },
];

function defaultParams(script: ApprovedScript): Record<string, ParamValue> {
  return Object.fromEntries(script.params.map((param) => [param.name, param.default]));
}

function paramError(param: ScriptParam, value: ParamValue): string | null {
  if (param.kind === "number" && value !== null) {
    const number = Number(value);
    if (!Number.isInteger(number) || number < param.min || number > param.max) {
      return `Must be a whole number from ${param.min} to ${param.max}.`;
    }
  }
  if (param.kind === "select" && !param.options.includes(String(value))) {
    return "Choose one of the listed options.";
  }
  return null;
}

function canTarget(script: ApprovedScript, device: Device): boolean {
  return device.enabled && script.platforms.includes(device.platform);
}

function ParamField({
  param,
  value,
  onChange,
}: {
  param: ScriptParam;
  value: ParamValue;
  onChange: (value: ParamValue) => void;
}) {
  const error = paramError(param, value);

  return (
    <label className={param.kind === "boolean" ? "form-field form-field-inline" : "form-field"}>
      {param.kind === "boolean" ? (
        <>
          <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />
          <span className="form-label">{param.label}</span>
        </>
      ) : (
        <>
          <span className="form-label">{param.label}</span>
          {param.kind === "select" ? (
            <select className="filter-select" value={String(value)} onChange={(event) => onChange(event.target.value)}>
              {param.options.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          ) : (
            <input
              type="number"
              className="search-input mono"
              min={param.min}
              max={param.max}
              step={1}
              value={value === null ? "" : String(value)}
              onChange={(event) => onChange(event.target.value === "" ? null : Number(event.target.value))}
            />
          )}
          {error && <span className="form-error">{error}</span>}
        </>
      )}
    </label>
  );
}

export function Scripts() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scriptId, setScriptId] = useState(APPROVED_SCRIPTS[0].id);
  const [deviceId, setDeviceId] = useState("");
  const [params, setParams] = useState<Record<string, ParamValue>>(defaultParams(APPROVED_SCRIPTS[0]));
  const [runPreview, setRunPreview] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const devicesData = await getDevices();

        if (cancelled) return;

        setDevices(devicesData);
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

  const script = APPROVED_SCRIPTS.find((entry) => entry.id === scriptId) ?? APPROVED_SCRIPTS[0];

  const sortedDevices = useMemo(
    () => [...devices].sort((a, b) => a.hostname.localeCompare(b.hostname)),
    [devices],
  );
  const eligibleDevices = sortedDevices.filter((device) => canTarget(script, device));
  const ineligibleDevices = sortedDevices.filter((device) => !canTarget(script, device));

  const selectedDevice = devices.find((device) => String(device.id) === deviceId) ?? null;
  const paramsValid = script.params.every((param) => paramError(param, params[param.name]) === null);
  const canRun = selectedDevice !== null && canTarget(script, selectedDevice) && paramsValid;

  function selectScript(next: ApprovedScript) {
    setScriptId(next.id);
    setParams(defaultParams(next));
    setRunPreview(null);
    if (selectedDevice && !canTarget(next, selectedDevice)) setDeviceId("");
  }

  function handleRun() {
    if (!selectedDevice || !canRun) return;

    setRunPreview(
      JSON.stringify({ script_id: script.id, target_host: selectedDevice.hostname, params }, null, 2),
    );
  }

  return (
    <>
      <h1 className="page-title">Scripts</h1>
      <p className="page-subtitle">Run approved automation scripts. Only predefined scripts are available.</p>

      {error && <div className="error-banner">Failed to load devices: {error}</div>}

      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header">
          <h2>Approved Scripts</h2>
          <span className="count-tag">{APPROVED_SCRIPTS.length}</span>
        </div>
        <div className="panel-body" style={{ maxHeight: "none" }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Script</th>
                <th>Description</th>
                <th>Platforms</th>
                <th>Mode</th>
              </tr>
            </thead>
            <tbody>
              {APPROVED_SCRIPTS.map((entry) => (
                <tr
                  key={entry.id}
                  className={entry.id === script.id ? "selectable-row selected" : "selectable-row"}
                  onClick={() => selectScript(entry)}
                >
                  <td>
                    <div>{entry.name}</div>
                    <div className="mono muted">{entry.id}</div>
                  </td>
                  <td>{entry.description}</td>
                  <td>
                    <div className="tag-row">
                      {entry.platforms.map((platform) => (
                        <span key={platform} className="platform-tag">
                          {platform}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>
                    <span className="view-only-tag">Read-only</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="changes-grid">
        <div className="panel">
          <div className="panel-header">
            <h2>Run {script.name}</h2>
            <span className="count-tag">{eligibleDevices.length} eligible</span>
          </div>
          <div className="change-form">
            <label className="form-field">
              <span className="form-label">Target device</span>
              <select
                className="filter-select"
                value={deviceId}
                disabled={loading}
                onChange={(event) => {
                  setDeviceId(event.target.value);
                  setRunPreview(null);
                }}
              >
                <option value="">{loading ? "Loading devices…" : "Select a device"}</option>
                <optgroup label="Eligible">
                  {eligibleDevices.map((device) => (
                    <option key={device.id} value={device.id}>
                      {device.hostname} ({device.platform})
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Not supported by this script">
                  {ineligibleDevices.map((device) => (
                    <option key={device.id} value={device.id} disabled>
                      {device.hostname} — {device.platform}
                      {device.enabled ? "" : ", disabled"}
                    </option>
                  ))}
                </optgroup>
              </select>
            </label>

            <div className="form-field">
              <span className="form-label">Parameters</span>
              <div className="params-area">
                {script.params.map((param) => (
                  <ParamField
                    key={`${script.id}-${param.name}`}
                    param={param}
                    value={params[param.name] ?? null}
                    onChange={(value) => {
                      setParams((current) => ({ ...current, [param.name]: value }));
                      setRunPreview(null);
                    }}
                  />
                ))}
              </div>
            </div>

            <div className="form-actions">
              <button type="button" className="primary-button" disabled={!canRun} onClick={handleRun}>
                Run
              </button>
              <span className="view-only-tag">Execution is not wired yet.</span>
            </div>

            {runPreview && (
              <div className="form-field">
                <span className="form-label">Run request (not submitted)</span>
                <pre className="config-preview">{runPreview}</pre>
              </div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <h2>Recent Script Runs</h2>
            <span className="count-tag">0</span>
          </div>
          <div className="panel-body" style={{ maxHeight: "none" }}>
            <div className="empty-state">
              Script run history will appear here once execution is wired to the API.
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
