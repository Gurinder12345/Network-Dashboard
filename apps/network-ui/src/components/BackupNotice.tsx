import type { BackupRun } from "../hooks/useDeviceBackups";
import { BackupDownloadButton } from "./BackupDownloadButton";
import { Banner } from "./Feedback";

/** Page-level feedback for the most recent manual backup (Devices and Device Detail). */
export function BackupNotice({ run, onDismiss }: { run: BackupRun; onDismiss: () => void }) {
  switch (run.state) {
    case "requesting":
    case "queued":
      return (
        <Banner tone="info" title={`Backup queued for ${run.hostname}.`}>
          Running-config backup is waiting for a worker.
        </Banner>
      );
    case "running":
      return <Banner tone="info" title={`Backing up ${run.hostname}…`}>Reading the running configuration (read-only).</Banner>;
    case "success":
      return (
        <Banner tone="success" title="Backup completed successfully." onDismiss={onDismiss}>
          <span>{run.hostname}</span>
          {run.backupId !== null && run.fileAvailable && (
            <BackupDownloadButton
              backupId={run.backupId}
              label="Download Backup"
              ariaLabel={`Download new backup of ${run.hostname}`}
            />
          )}
        </Banner>
      );
    default:
      return (
        <Banner tone="danger" title="Backup failed." onDismiss={onDismiss}>
          {run.hostname}: {run.error}
        </Banner>
      );
  }
}
