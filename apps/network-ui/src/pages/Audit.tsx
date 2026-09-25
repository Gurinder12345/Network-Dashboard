import { useEffect, useMemo, useState } from "react";
import { getAuditEvents, getDevices } from "../api/client";
import type { AuditEvent, Device } from "../api/types";
import { formatTimestamp, truncateId, truncateText } from "../utils/format";

const ALL_EVENT_TYPES = "all";
const MESSAGE_PREVIEW_LENGTH = 90;

// Worker event types follow "<action>_<phase>", e.g. apply_started, backup_failed.
// The phase decides the badge colour; unknown phases fall back to neutral.
function eventTone(eventType: string): string {
  const normalized = eventType.toLowerCase();

  if (/(failed|error|rejected|denied)$/.test(normalized)) return "failed";
  if (/(completed|succeeded|success|approved)$/.test(normalized)) return "applied";
  if (/(started|running|requested|queued)$/.test(normalized)) return "applying";
  return "unknown";
}

function EventBadge({ eventType }: { eventType: string }) {
  return (
    <span className={`badge badge-${eventTone(eventType)}`} title={eventType}>
      <span className="badge-dot" />
      {eventType.replace(/_/g, " ")}
    </span>
  );
}

function MessageCell({ message, failed }: { message: string | null; failed: boolean }) {
  if (!message) return <span className="mono">—</span>;

  const isLong = message.length > MESSAGE_PREVIEW_LENGTH || message.includes("\n");
  const summaryClass = failed ? "error-summary" : "message-summary";

  if (!isLong) {
    return <span className={failed ? "audit-message-failed" : "audit-message"}>{message}</span>;
  }

  return (
    <details className="error-details">
      <summary className={`${summaryClass} mono`}>
        {truncateText(message, MESSAGE_PREVIEW_LENGTH)}
      </summary>
      <pre className="error-full">{message}</pre>
    </details>
  );
}

export function Audit() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [eventTypeFilter, setEventTypeFilter] = useState(ALL_EVENT_TYPES);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [eventsData, devicesData] = await Promise.all([getAuditEvents(), getDevices()]);

        if (cancelled) return;

        setEvents(eventsData);
        setDevices(devicesData);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load audit events");
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

  const eventTypes = useMemo(
    () => Array.from(new Set(events.map((event) => event.event_type))).sort(),
    [events],
  );

  function deviceLabel(deviceId: number | null): string {
    if (deviceId === null) return "—";
    return deviceHostnameById.get(deviceId) ?? `Device #${deviceId}`;
  }

  const filteredEvents = useMemo(() => {
    const query = search.trim().toLowerCase();

    return events.filter((event) => {
      const matchesType =
        eventTypeFilter === ALL_EVENT_TYPES || event.event_type === eventTypeFilter;

      if (!matchesType) return false;

      if (query.length === 0) return true;

      const hostname =
        event.device_id !== null ? deviceHostnameById.get(event.device_id) ?? "" : "";

      const haystack = [hostname, event.event_type, event.job_id ?? "", event.message ?? ""]
        .join(" ")
        .toLowerCase();

      return haystack.includes(query);
    });
  }, [events, search, eventTypeFilter, deviceHostnameById]);

  return (
    <>
      <h1 className="page-title">Audit</h1>
      <p className="page-subtitle">Platform event history (read-only).</p>

      {error && <div className="error-banner">Failed to load audit events: {error}</div>}

      <div className="filters-bar">
        <input
          type="text"
          className="search-input"
          placeholder="Search device, event, job ID, or message..."
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="filter-select"
          value={eventTypeFilter}
          onChange={(event) => setEventTypeFilter(event.target.value)}
        >
          <option value={ALL_EVENT_TYPES}>All event types</option>
          {eventTypes.map((eventType) => (
            <option key={eventType} value={eventType}>
              {eventType}
            </option>
          ))}
        </select>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h2>Audit Events</h2>
          <span className="count-tag">{filteredEvents.length}</span>
        </div>
        <div className="panel-body" style={{ maxHeight: "none" }}>
          {loading ? (
            <div className="empty-state">Loading audit events&hellip;</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Event</th>
                  <th>Device</th>
                  <th>Job ID</th>
                  <th>Time</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {filteredEvents.map((event) => (
                  <tr key={event.id}>
                    <td>
                      <EventBadge eventType={event.event_type} />
                    </td>
                    <td>{deviceLabel(event.device_id)}</td>
                    <td className="mono" title={event.job_id ?? undefined}>
                      {truncateId(event.job_id)}
                    </td>
                    <td className="mono" title={event.created_at ?? undefined}>
                      {formatTimestamp(event.created_at)}
                    </td>
                    <td>
                      <MessageCell
                        message={event.message}
                        failed={eventTone(event.event_type) === "failed"}
                      />
                    </td>
                  </tr>
                ))}
                {filteredEvents.length === 0 && (
                  <tr>
                    <td colSpan={5} className="empty-state">
                      {events.length === 0
                        ? "No audit events recorded yet."
                        : "No audit events match your search or filter."}
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
