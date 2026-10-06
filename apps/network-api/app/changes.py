import logging
import uuid

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from app.celery_client import PUBLISH_RETRY_POLICY, celery_app
from app.config_blocks import blocks_from_legacy, validate_blocks, validate_verification_commands
from app.db.audit import create_audit_event
from app.db.devices import get_device_by_id


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/changes", tags=["changes"])

OS6_PLATFORM = "dell_os6"
# Platforms the shared worker change workflow supports. Both use ordered configuration
# blocks; there is no configuration command policy (the device is the syntax authority).
SUPPORTED_CHANGE_PLATFORMS = {"dell_os6": "Dell OS6", "dell_os10": "Dell OS10"}
PRECHECK_TASK = "network_worker.change_precheck"
# Legacy OS6-only route keeps its original task.
OS6_PRECHECK_TASK = "network_worker.os6_change_precheck"

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


class ChangePrecheckRequest(BaseModel):
    """
    One change request = ordered configuration blocks for one device:
        {"device_id": 12, "blocks": [{"parent": "interface ethernet1/1/18", "commands": [...]},
                                     {"parent": null, "commands": ["ip routing"]}],
         "verification_commands": ["show vlan"]}
    Order comes from the array order. The single-parent form
        {"device_id": 1, "config_parents": [...], "config_lines": [...]}
    is still accepted and converted to one block.
    """

    model_config = ConfigDict(extra="forbid")

    device_id: int
    blocks: list[Any] | None = None
    config_parents: list[Any] | None = None
    config_lines: list[Any] | None = None
    verification_commands: list[Any] | None = None

    @model_validator(mode="after")
    def to_blocks(self):
        if self.blocks is not None and (self.config_lines is not None or self.config_parents):
            raise ValueError("send either blocks or config_lines/config_parents, not both")
        if self.blocks is None:
            if self.config_lines is None:
                raise ValueError("blocks is required")
            if not isinstance(self.config_parents or [], list):
                raise ValueError("config_parents must be a list")
            for value in (self.config_parents or []):
                if not isinstance(value, str):
                    raise ValueError("config_parents entries must be strings")
            raw = blocks_from_legacy(self.config_lines, self.config_parents)
        else:
            raw = self.blocks
        self.blocks = validate_blocks(raw)
        self.verification_commands = validate_verification_commands(self.verification_commands)
        self.config_lines = None
        self.config_parents = None
        return self


# Backwards-compatible name.
Os6PrecheckRequest = ChangePrecheckRequest


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
        "platform": result.get("platform"),
        "ready_for_approval": result.get("ready_for_approval", False),
        "backup_required": result.get("backup_required", False),
        # Per-block reasons when status is "precheck_failed".
        "rejection_reasons": result.get("rejection_reasons", []),
        "block_count": result.get("block_count"),
        "command_count": result.get("command_count"),
        "dry_run": {
            "would_change": dry_run.get("would_change"),
            "verification_method": dry_run.get("verification_method"),
            "already_present": dry_run.get("already_present", []),
            "proposed_changes": dry_run.get("proposed_changes", []),
            "command_results": dry_run.get("command_results", []),
            # Multi-block precheck: overall result, the four check categories, per-block
            # status (config capture + semantic pre-validation per command) and the CLI preview.
            "overall": dry_run.get("overall"),
            "checks": dry_run.get("checks"),
            "blocks": dry_run.get("blocks", []),
            "cli_preview": dry_run.get("cli_preview"),
            "verification_commands": dry_run.get("verification_commands", []),
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


def _submit_precheck(request, allowed_platforms, task_name):
    try:
        device = get_device_by_id(request.device_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {request.device_id} not found")

    if device["platform"] not in allowed_platforms:
        supported = ", ".join(allowed_platforms)
        raise HTTPException(
            status_code=422,
            detail=f"{device['hostname']} is {device['platform']}; configuration changes are supported for {supported}",
        )

    if not device["enabled"]:
        raise HTTPException(status_code=422, detail=f"{device['hostname']} is disabled")

    label = SUPPORTED_CHANGE_PLATFORMS.get(device["platform"], device["platform"])

    # The worker resolves credentials from Vault by hostname; the API never handles them.
    try:
        task = celery_app.send_task(
            task_name,
            kwargs={
                "target_host": device["hostname"],
                "config_blocks": request.blocks,
                "verification_commands": request.verification_commands,
            },
            retry=True,
            retry_policy=PUBLISH_RETRY_POLICY,
        )
    except Exception as exc:
        logger.exception("Failed to enqueue precheck")
        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {exc}")

    try:
        create_audit_event(
            job_id=None,
            device_id=device["id"],
            event_type="precheck_requested",
            message=(
                f"{label} precheck requested for {device['hostname']} (request_id={task.id}, "
                f"{len(request.blocks)} block(s), {sum(len(b['commands']) for b in request.blocks)} command(s))"
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
        "platform": device["platform"],
    }


@router.post("/precheck", status_code=202)
def submit_precheck(request: ChangePrecheckRequest):
    """Platform-generic precheck (Dell OS6 / Dell OS10); the worker dispatches by platform."""
    return _submit_precheck(request, tuple(SUPPORTED_CHANGE_PLATFORMS), PRECHECK_TASK)


@router.post("/os6/precheck", status_code=202)
def submit_os6_precheck(request: ChangePrecheckRequest):
    """Legacy OS6-only route (unchanged behaviour)."""
    return _submit_precheck(request, (OS6_PLATFORM,), OS6_PRECHECK_TASK)


@router.get("/precheck/{request_id}")
@router.get("/os6/precheck/{request_id}")
def get_precheck(request_id: str):
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
