from fastapi import FastAPI, HTTPException

from app.db.devices import list_devices
from app.db.jobs import list_jobs
from app.db.approvals import list_approvals
from app.db.backups import list_backups
from app.db.audit import list_audit_events

app = FastAPI(title="Network Management API")


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
