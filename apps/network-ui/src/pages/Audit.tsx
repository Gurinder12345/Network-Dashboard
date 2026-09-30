import { useEffect, useMemo, useState } from "react";
import { getAuditEvents, getDevices } from "../api/client";
import type { AuditEvent, Device } from "../api/types";
import { Banner, EmptyState, TableSkeleton } from "../components/Feedback";
import { FilterChips } from "../components/FilterChips";
import { PageHeader } from "../components/PageHeader";
import { RelativeTime } from "../components/RelativeTime";
import { StatusBadge, type Tone } from "../components/StatusBadge";
import { truncateId, truncateText } from "../utils/format";

const ALL = "all";
const ERRORS = "errors";
const MESSAGE_PREVIEW_LENGTH = 90;

type Category = "approval" | "apply" | "backup" | "precheck" | "health" | "cancellation" | "other";

const CATEGORY_LABELS: Record<Category, string> = {
  approval: "Approval",
  apply: "Apply",
  backup: "Backup",
  precheck: "Precheck",
  health: "Health",
  cancellation: "Cancellation",
  other: "Other",
};

// Event types follow "<action>_<phase>", e.g. apply_started, backup_failed, approval_cancelled.
function eventCategory(eventType: string): Category {
  const t = eventType.toLowerCase();
  if (t.endsWith("_cancelled")) return "cancellation";
  if (t.startsWith("approval")) return "approval";
  if (t.startsWith("apply")) return "apply";
  if (t.startsWith("backup")) return "backup";
  if (t.startsWith("precheck")) return "precheck";
  if (t.startsWith("health") || t.startsWith("device_health")) return "health";
  return "other";
}

// The phase decides the outcome color; unknown phases fall back to neutral.
function eventTone(eventType: string): Tone {
  const t = eventType.toLowerCase();
  if (/(failed|error|rejected|denied)$/.test(t)) return "danger";
  if (/(cancelled)$/.test(t)) return "neutral";
  if (/(completed|succeeded|success|approved|downloaded)$/.test(t)) return "success";
  if (/(started|running|requested|queued)$/.test(t)) return "info";
  return "neutral";
}

function eventPhase(eventType: string): string {
  const parts = eventType.split("_");
  return parts.length > 1 ? parts.slice(1).join(" ") : eventType;
}

function MessageCell({ message, failed }: { message: string | null; failed: boolean }) {
  if (!message) return <span className="muted">—</span>;

  const isLong = message.length > MESSAGE_PREVIEW_LENGTH || message.includes("\n");

  if (!isLong) {
    return <span className={failed ? "audit-message-failed" : "audit-message"}>{message}</span>;
  }

  return (
    <details className="error-details" style={{ maxWidth: 520 }}>
      <summary className={`${failed ? "error-summary" : "message-summary"} mono`}>
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
  const [categoryFilter, setCategoryFilter] = useState(ALL);
  const [eventTypeFilter, setEventTypeFilter] = useState(ALL);

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

  const categoryCounts = useMemo(() => {
    const counts: Record<string, number> = { [ERRORS]: 0 };
    events.forEach((event) => {
      const category = eventCategory(event.event_type);
      counts[category] = (counts[category] ?? 0) + 1;
      if (eventTone(event.event_type) === "danger") counts[ERRORS] += 1;
    });
    return counts;
  }, [events]);

  const filteredEvents = useMemo(() => {
    const query = search.trim().toLowerCase();

    return events.filter((event) => {
      if (eventTypeFilter !== ALL && event.event_type !== eventTypeFilter) return false;
      if (categoryFilter === ERRORS && eventTone(event.event_type) !== "danger") return false;
      if (categoryFilter !== ALL && categoryFilter !== ERRORS && eventCategory(event.event_type) !== categoryFilter) {
        return false;
      }

      if (query.length === 0) return true;

      const hostname = event.device_id !== null ? deviceHostnameById.get(event.device_id) ?? "" : "";
      const haystack = [hostname, event.event_type, event.job_id ?? "", event.message ?? ""]
        .join(" ")
        .toLowerCase();

      return haystack.includes(query);
    });
  }, [events, search, categoryFilter, eventTypeFilter, deviceHostnameById]);

  const presentCategories = (Object.keys(CATEGORY_LABELS) as Category[]).filter((c) => (categoryCounts[c] ?? 0) > 0);

  return (
    <>
      <PageHeader title="Audit" subtitle="Read-only platform event history." />

      {error && (
        <Banner tone="danger" title="Failed to load audit events.">
          {error}
        </Banner>
      )}

      <div className="filters-bar">
        <input
          type="search"
          className="search-input"
          placeholder="Search device, event, job ID, or message…"
          aria-label="Search audit events"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <FilterChips
          label="Event category"
          value={categoryFilter}
          onChange={setCategoryFilter}
          options={[
            { value: ALL, label: "All", count: events.length },
            ...presentCategories.map((category) => ({
              value: category,
              label: CATEGORY_LABELS[category],
              count: categoryCounts[category],
            })),
            { value: ERRORS, label: "Errors", count: categoryCounts[ERRORS], status: "failed" },
          ]}
        />
        <select
          className="filter-select"
          aria-label="Event type"
          value={eventTypeFilter}
          onChange={(event) => setEventTypeFilter(event.target.value)}
        >
          <option value={ALL}>All event types</option>
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
        <div className="table-wrap">
          {loading ? (
            <TableSkeleton rows={10} columns={6} />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Category</th>
                  <th>Event</th>
                  <th>Device</th>
                  <th>Job ID</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {filteredEvents.map((event) => {
                  const tone = eventTone(event.event_type);
                  return (
                    <tr key={event.id} className={tone === "danger" ? "row-alert" : undefined}>
                      <td>
                        <RelativeTime value={event.created_at} />
                      </td>
                      <td>
                        <span className="tag tag-plain">{CATEGORY_LABELS[eventCategory(event.event_type)]}</span>
                      </td>
                      <td>
                        <StatusBadge
                          status={event.event_type}
                          label={eventPhase(event.event_type)}
                          tone={tone}
                          title={event.event_type}
                        />
                      </td>
                      <td className="cell-primary">
                        {event.device_id === null ? (
                          <span className="muted">—</span>
                        ) : (
                          deviceHostnameById.get(event.device_id) ?? `Device #${event.device_id}`
                        )}
                      </td>
                      <td className="mono muted" title={event.job_id ?? undefined}>
                        {truncateId(event.job_id)}
                      </td>
                      <td>
                        <MessageCell message={event.message} failed={tone === "danger"} />
                      </td>
                    </tr>
                  );
                })}
                {filteredEvents.length === 0 && (
                  <tr>
                    <td colSpan={6}>
                      <EmptyState
                        title={events.length === 0 ? "No audit events recorded yet." : "No events match these filters."}
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
