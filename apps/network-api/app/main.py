from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.db.jobs import list_jobs
from app.db.approvals import list_approvals
from app.db.backups import list_backups
from app.db.audit import list_audit_events
from app.changes import router as changes_router
from app.backup_download import file_available, router as backup_download_router
from app.device_backup import router as device_backup_router
from app.device_detail import router as device_detail_router
from app.approval_actions import router as approval_actions_router
from app.health import devices_with_health, router as health_router
from app.topology import router as topology_router

app = FastAPI(title="Network Management API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://192.168.137.148:5173",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(changes_router)
app.include_router(backup_download_router)
app.include_router(device_backup_router)
app.include_router(device_detail_router)
app.include_router(approval_actions_router)
app.include_router(health_router)
app.include_router(topology_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/devices")
def get_devices():
    try:
        return devices_with_health()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/jobs")
def get_jobs():
    try:
        return list_jobs()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/approvals")
def get_approvals():
    try:
        return list_approvals()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/backups")
def get_backups():
    try:
        backups = list_backups()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Download is offered only when the stored file is actually readable under /backups.
    for backup in backups:
        backup["file_available"] = file_available(backup["storage_path"])

    return backups


@app.get("/api/v1/audit")
def get_audit_events():
    try:
        return list_audit_events()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
