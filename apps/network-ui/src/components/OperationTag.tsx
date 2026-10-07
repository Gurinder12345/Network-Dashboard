import type { DeviceOperation } from "../api/types";
import { formatRelative, truncateId } from "../utils/format";

/**
 * Subtle operational state shown next to (never instead of) the health badge: an approved
 * configuration change is executing on the device. Health status itself is unchanged;
 * polling is suppressed and a health failure may be inside the short change grace window.
 */
export function OperationTag({ operation }: { operation: DeviceOperation | null | undefined }) {
  if (!operation || operation.operation_state !== "change_in_progress") return null;
  const title = [
    `Configuration change ${operation.active_change_id ?? ""} in progress`,
    operation.change_started_at ? `since ${formatRelative(operation.change_started_at)}` : "",
    "Telemetry and topology polling are paused for this device.",
    operation.health_grace
      ? "A failed health check is inside the change grace window; normal health rules resume after it."
      : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <span className="operation-tag" title={title}>
      Change in progress
      {operation.active_change_id && <span className="mono"> #{truncateId(operation.active_change_id)}</span>}
    </span>
  );
}
