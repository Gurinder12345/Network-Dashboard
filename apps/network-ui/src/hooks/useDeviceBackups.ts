import { useCallback, useEffect, useRef, useState } from "react";
import { getJob, requestDeviceBackup } from "../api/client";

export type BackupRunState = "requesting" | "queued" | "running" | "success" | "failed" | "lost";

export interface BackupRun {
  deviceId: number;
  hostname: string;
  jobId: string | null;
  state: BackupRunState;
  error: string | null;
  backupId: number | null;
  fileAvailable: boolean;
  requestedAt: number;
}

const POLL_MS = 2000;
// Matches the API's per-device lock TTL: after this the job is reported as unresolved.
const GIVE_UP_MS = 15 * 60 * 1000;

export const isActive = (run: BackupRun | undefined) =>
  !!run && (run.state === "requesting" || run.state === "queued" || run.state === "running");

/**
 * Per-device manual backups: POST, then poll GET /api/v1/jobs/{id} (the job row the worker
 * updates) every 2 s while any backup is in flight. Each device is tracked independently.
 */
export function useDeviceBackups(onCompleted: () => void) {
  const [runs, setRuns] = useState<Record<number, BackupRun>>({});
  const [lastDeviceId, setLastDeviceId] = useState<number | null>(null);
  const runsRef = useRef(runs);
  runsRef.current = runs;
  const completedRef = useRef(onCompleted);
  completedRef.current = onCompleted;

  const update = useCallback((deviceId: number, patch: Partial<BackupRun>) => {
    setRuns((current) => (current[deviceId] ? { ...current, [deviceId]: { ...current[deviceId], ...patch } } : current));
  }, []);

  const start = useCallback(
    async (deviceId: number, hostname: string) => {
      if (isActive(runsRef.current[deviceId])) return;

      setLastDeviceId(deviceId);
      setRuns((current) => ({
        ...current,
        [deviceId]: {
          deviceId,
          hostname,
          jobId: null,
          state: "requesting",
          error: null,
          backupId: null,
          fileAvailable: false,
          requestedAt: Date.now(),
        },
      }));

      try {
        const result = await requestDeviceBackup(deviceId);

        if (result.kind === "queued") {
          update(deviceId, { state: "queued", jobId: result.response.job_id });
        } else if (result.jobId) {
          // Someone else's backup of this device is in flight: follow that job instead.
          update(deviceId, { state: "running", jobId: result.jobId });
        } else {
          update(deviceId, { state: "failed", error: result.message });
        }
      } catch (err) {
        update(deviceId, { state: "failed", error: err instanceof Error ? err.message : "Backup request failed" });
      }
    },
    [update],
  );

  const pending = Object.values(runs).some((run) => (run.state === "queued" || run.state === "running") && run.jobId);

  useEffect(() => {
    if (!pending) return;

    const timer = window.setInterval(async () => {
      const active = Object.values(runsRef.current).filter(
        (run) => (run.state === "queued" || run.state === "running") && run.jobId,
      );

      await Promise.all(
        active.map(async (run) => {
          if (Date.now() - run.requestedAt > GIVE_UP_MS) {
            update(run.deviceId, { state: "lost", error: "No result after 15 minutes. Check the Jobs page." });
            return;
          }

          try {
            const job = await getJob(run.jobId!);

            if (job.status === "success") {
              update(run.deviceId, {
                state: "success",
                backupId: job.backup?.backup_id ?? null,
                fileAvailable: job.backup?.file_available ?? false,
              });
              completedRef.current();
            } else if (job.status === "failed") {
              update(run.deviceId, { state: "failed", error: job.error_message ?? "Backup failed" });
            } else if (job.status === "running" && run.state !== "running") {
              update(run.deviceId, { state: "running" });
            }
          } catch {
            // Transient API error: keep polling until the give-up deadline.
          }
        }),
      );
    }, POLL_MS);

    return () => window.clearInterval(timer);
  }, [pending, update]);

  const dismiss = useCallback(() => setLastDeviceId(null), []);

  return {
    runs,
    start,
    lastRun: lastDeviceId !== null ? runs[lastDeviceId] ?? null : null,
    dismiss,
  };
}
