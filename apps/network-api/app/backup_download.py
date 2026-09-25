import logging
import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.db.audit import create_audit_event
from app.db.backups import get_backup_by_id


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/backups", tags=["backups"])

# Backups are written by the worker under /backups/<hostname>/<timestamp>.cfg.
BACKUP_ROOT = os.path.realpath(os.getenv("BACKUP_ROOT", "/backups"))

SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def resolve_backup_file(storage_path):
    """Resolve a stored path, refusing anything that escapes BACKUP_ROOT (including via symlinks)."""
    if not storage_path:
        return None

    resolved = os.path.realpath(storage_path)

    if os.path.commonpath([resolved, BACKUP_ROOT]) != BACKUP_ROOT or resolved == BACKUP_ROOT:
        return None

    if not os.path.isfile(resolved):
        return None

    # Worker files are 0600; a UID mismatch would otherwise fail mid-response.
    if not os.access(resolved, os.R_OK):
        logger.warning("Backup file exists but is not readable by UID %s: %s", os.getuid(), resolved)
        return None

    return resolved


@router.get("/{backup_id}/download")
def download_backup(backup_id: int):
    # The client supplies only the numeric ID; the path always comes from the database.
    try:
        backup = get_backup_by_id(backup_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if backup is None:
        raise HTTPException(status_code=404, detail=f"Backup {backup_id} not found")

    resolved = resolve_backup_file(backup["storage_path"])

    if resolved is None:
        logger.warning(
            "Backup %s file unavailable or outside %s: %s",
            backup_id,
            BACKUP_ROOT,
            backup["storage_path"],
        )
        raise HTTPException(status_code=404, detail=f"Backup file for {backup_id} not found")

    hostname = backup["hostname"] or f"device-{backup['device_id']}"
    filename = SAFE_FILENAME_CHARS.sub("_", f"{hostname}_{os.path.basename(resolved)}")

    try:
        create_audit_event(
            job_id=None,
            device_id=backup["device_id"],
            event_type="backup_downloaded",
            message=f"Backup {backup_id} downloaded: {backup['storage_path']}",
        )
    except Exception:
        logger.exception("Failed to record backup_downloaded audit event")

    return FileResponse(
        resolved,
        media_type="text/plain",
        filename=filename,
        # Device configs are sensitive; keep them out of browser/proxy caches.
        headers={"Cache-Control": "no-store"},
    )
