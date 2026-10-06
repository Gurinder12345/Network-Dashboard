import { useState } from "react";
import { downloadBackup } from "../api/client";
import { Icon } from "./Icon";

// The API streams the stored file as an attachment; it is handed straight to the browser's
// save flow and never shown or kept in React state.
function saveFile(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

interface BackupDownloadButtonProps {
  backupId: number;
  label?: string;
  /** Accessible name; defaults to "Download backup <id>". */
  ariaLabel?: string;
  className?: string;
}

export function BackupDownloadButton({ backupId, label = "Download", ariaLabel, className }: BackupDownloadButtonProps) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDownload() {
    setDownloading(true);
    setError(null);

    try {
      const { blob, filename } = await downloadBackup(backupId);
      saveFile(blob, filename);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="download-cell">
      <button
        type="button"
        className={className ?? "secondary-button small-button"}
        disabled={downloading}
        onClick={handleDownload}
        aria-label={ariaLabel ?? `Download backup ${backupId}`}
      >
        <Icon name="download" size={13} />
        {downloading ? "Downloading…" : label}
      </button>
      {error && (
        <span className="form-error" title={error}>
          {error}
        </span>
      )}
    </div>
  );
}
