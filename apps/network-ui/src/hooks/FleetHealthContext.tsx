import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getFleetHealth } from "../api/client";
import type { FleetHealth } from "../api/types";
import { usePolling } from "./usePolling";

const FLEET_POLL_MS = 20000;
// Beat runs every 60 s; three missed sweeps means the health pipeline is not updating.
export const HEALTH_STALE_AFTER_MS = 180000;

interface FleetHealthState {
  fleet: FleetHealth | null;
  error: string | null;
  lastSuccessAt: Date | null;
  refresh: () => Promise<void>;
}

const FleetHealthContext = createContext<FleetHealthState | null>(null);

/**
 * One shared poll of GET /api/v1/health/devices for the header and Overview.
 * Only reads stored health; it never triggers switch checks.
 */
export function FleetHealthProvider({ children }: { children: ReactNode }) {
  const [fleet, setFleet] = useState<FleetHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastSuccessAt, setLastSuccessAt] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      setFleet(await getFleetHealth());
      setError(null);
      setLastSuccessAt(new Date());
    } catch (err) {
      // Keep the last good data on screen; consumers show a stale warning.
      setError(err instanceof Error ? err.message : "Failed to load health");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  usePolling(refresh, FLEET_POLL_MS);

  const value = useMemo(
    () => ({ fleet, error, lastSuccessAt, refresh }),
    [fleet, error, lastSuccessAt, refresh],
  );

  return <FleetHealthContext.Provider value={value}>{children}</FleetHealthContext.Provider>;
}

export function useFleetHealth(): FleetHealthState {
  const value = useContext(FleetHealthContext);
  if (!value) throw new Error("useFleetHealth must be used inside FleetHealthProvider");
  return value;
}

/** True when the newest health check is older than HEALTH_STALE_AFTER_MS. */
export function isHealthStale(lastUpdated: string | null, now: number = Date.now()): boolean {
  if (!lastUpdated) return false;
  return now - new Date(lastUpdated).getTime() > HEALTH_STALE_AFTER_MS;
}
