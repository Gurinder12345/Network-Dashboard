from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.db.devices import list_devices
from app.db.jobs import list_jobs
from app.db.approvals import list_approvals
from app.db.backups import list_backups
from app.db.audit import list_audit_events
from app.changes import router as changes_router
from app.backup_download import router as backup_download_router
from app.approval_actions import router as approval_actions_router

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
app.include_router(approval_actions_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/devices")
def get_devices():
    try:
        return list_devices()
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
        return list_backups()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/audit")
def get_audit_events():
    try:
        return list_audit_events()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
