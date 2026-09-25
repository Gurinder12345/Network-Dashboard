import logging
import re
import uuid

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.celery_client import PUBLISH_RETRY_POLICY, celery_app
from app.db.audit import create_audit_event
from app.db.devices import get_device_by_id


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/changes/os6", tags=["changes"])

OS6_PLATFORM = "dell_os6"
PRECHECK_TASK = "network_worker.os6_change_precheck"

MAX_PARENTS = 5
MAX_LINES = 50
MAX_LINE_LENGTH = 250

# One CLI command per entry: printable ASCII only, so a single entry can never
# carry a newline/control character that the device would treat as a second command.
PRINTABLE_LINE = re.compile(r"^[\x20-\x7e]+$")
# The playbook enters/exits config mode itself; "do" would run exec-mode commands.
FORBIDDEN_PREFIX = re.compile(r"^(configure|config|conf\s+t|end|exit|do)(\s|$)", re.IGNORECASE)

# Celery states -> states the UI understands.
STATE_MAP = {
    "PENDING": "queued",
    "RECEIVED": "queued",
    "STARTED": "running",
    "RETRY": "running",
    "SUCCESS": "completed",
    "FAILURE": "failed",
    "REVOKED": "failed",
}


def _clean_commands(values, field_name, max_items):
    cleaned = [value.strip() for value in values]

    if len(cleaned) > max_items:
        raise ValueError(f"{field_name} accepts at most {max_items} entries")

    for value in cleaned:
        if not value:
            raise ValueError(f"{field_name} cannot contain empty entries")
        if len(value) > MAX_LINE_LENGTH:
            raise ValueError(f"{field_name} entries must be at most {MAX_LINE_LENGTH} characters")
        if not PRINTABLE_LINE.match(value):
            raise ValueError(f"{field_name} entries must be single-line printable text: {value!r}")
        if FORBIDDEN_PREFIX.match(value):
            raise ValueError(f"{field_name} cannot include mode commands (configure/end/exit/do): {value!r}")

    return cleaned


class Os6PrecheckRequest(BaseModel):
    device_id: int
    config_parents: list[str] = Field(default_factory=list)
    config_lines: list[str]

    @field_validator("config_parents")
    @classmethod
    def validate_parents(cls, value):
        return _clean_commands(value, "config_parents", MAX_PARENTS)

    @field_validator("config_lines")
    @classmethod
    def validate_lines(cls, value):
        cleaned = _clean_commands(value, "config_lines", MAX_LINES)
        if not cleaned:
            raise ValueError("config_lines must contain at least one command")
        return cleaned


def _summarize_result(result):
    """Return only the precheck fields the UI needs from the worker result."""
    if not isinstance(result, dict):
        return None

    dry_run = result.get("dry_run") or {}
    backup = result.get("backup") or {}
    approval = result.get("approval") or {}

    return {
        "status": result.get("status"),
        "target_host": result.get("target_host"),
        "ready_for_approval": result.get("ready_for_approval", False),
        "backup_required": result.get("backup_required", False),
        "dry_run": {
            "would_change": dry_run.get("would_change"),
            "verification_method": dry_run.get("verification_method"),
            "already_present": dry_run.get("already_present", []),
            "proposed_changes": dry_run.get("proposed_changes", []),
            "command_results": dry_run.get("command_results", []),
        },
        "backup": (
            {
                "job_id": backup.get("job_id"),
                "status": backup.get("status"),
                "checksum": backup.get("checksum"),
                "storage_path": backup.get("storage_path"),
            }
            if backup
            else None
        ),
        "approval": (
            {
                "approval_id": approval.get("approval_id"),
                "status": approval.get("status"),
            }
            if approval
            else None
        ),
    }


@router.post("/precheck", status_code=202)
def submit_os6_precheck(request: Os6PrecheckRequest):
    try:
        device = get_device_by_id(request.device_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {request.device_id} not found")

    if device["platform"] != OS6_PLATFORM:
        raise HTTPException(
            status_code=422,
            detail=f"{device['hostname']} is {device['platform']}; only {OS6_PLATFORM} devices accept changes",
        )

    if not device["enabled"]:
        raise HTTPException(status_code=422, detail=f"{device['hostname']} is disabled")

    # The worker resolves credentials from Vault by hostname; the API never handles them.
    try:
        task = celery_app.send_task(
            PRECHECK_TASK,
            kwargs={
                "target_host": device["hostname"],
                "config_lines": request.config_lines,
                "config_parents": request.config_parents or None,
            },
            retry=True,
            retry_policy=PUBLISH_RETRY_POLICY,
        )
    except Exception as exc:
        logger.exception("Failed to enqueue OS6 precheck")
        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {exc}")

    try:
        create_audit_event(
            job_id=None,
            device_id=device["id"],
            event_type="precheck_requested",
            message=(
                f"OS6 precheck requested for {device['hostname']} "
                f"(request_id={task.id}, {len(request.config_lines)} line(s))"
            ),
        )
    except Exception:
        # The precheck is already queued; a missing audit row should not hide that from the user.
        logger.exception("Failed to record precheck_requested audit event")

    return {
        "request_id": task.id,
        "state": "queued",
        "device_id": device["id"],
        "hostname": device["hostname"],
    }


@router.get("/precheck/{request_id}")
def get_os6_precheck(request_id: str):
    try:
        uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    try:
        task = AsyncResult(request_id, app=celery_app)
        celery_state = task.state
        raw_result = task.result
    except Exception as exc:
        logger.exception("Failed to read precheck result")
        raise HTTPException(status_code=503, detail=f"Result backend unavailable: {exc}")

    state = STATE_MAP.get(celery_state, "running")

    response = {
        "request_id": request_id,
        "state": state,
        "result": None,
        "error": None,
    }

    if state == "completed":
        response["result"] = _summarize_result(raw_result)
    elif state == "failed":
        response["error"] = str(raw_result) if raw_result else "Precheck failed"

    return response
