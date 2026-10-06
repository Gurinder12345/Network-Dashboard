import logging
import uuid

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.celery_client import PUBLISH_RETRY_POLICY, celery_app
from app.changes import SUPPORTED_CHANGE_PLATFORMS
from app.db.approvals import (
    approve_pending_approval,
    cancel_open_approval,
    claim_approval_for_apply,
    get_approval,
    release_apply_claim,
)
from app.db.audit import create_audit_event
from app.db.devices import get_device_by_id


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

# Shared worker task; it dispatches on the device platform (Dell OS6 / Dell OS10).
APPLY_TASK = "network_worker.apply_approved_change"
MAX_ERROR_LENGTH = 20000

APPLY_STATE_MAP = {
    "PENDING": "queued",
    "RECEIVED": "queued",
    "STARTED": "running",
    "RETRY": "running",
    "SUCCESS": "succeeded",
    "FAILURE": "failed",
    "REVOKED": "failed",
}


class ApproveRequest(BaseModel):
    # Only the approver identity is accepted; the configuration always comes from the
    # stored approval record. There is no authentication yet, so this is self-asserted.
    approved_by: str = Field(pattern=r"^[A-Za-z0-9._@-]{2,64}$")


@router.post("/{approval_id}/approve")
def approve(approval_id: uuid.UUID, request: ApproveRequest):
    approval_key = str(approval_id)

    try:
        approval = get_approval(approval_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_key} not found")

    if approval["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Approval is {approval['status']}; only pending approvals can be approved",
        )

    if request.approved_by.lower() == (approval["requested_by"] or "").lower():
        raise HTTPException(status_code=422, detail="Approver must be different from the requester")

    device = get_device_by_id(approval["device_id"])

    if device is None or device["platform"] not in SUPPORTED_CHANGE_PLATFORMS:
        raise HTTPException(status_code=422, detail="Configuration changes are not supported for this device's platform")

    # The UPDATE only matches while still pending, so concurrent clicks cannot both succeed.
    updated = approve_pending_approval(approval_key, request.approved_by)

    if updated is None:
        raise HTTPException(status_code=409, detail="Approval is no longer pending")

    try:
        create_audit_event(
            job_id=updated["backup_job_id"],
            device_id=updated["device_id"],
            event_type="approval_approved",
            message=(
                f"Change approval {approval_key} for {device['hostname']} approved by "
                f"{request.approved_by} ({updated['block_count']} block(s), {updated['command_count']} command(s)); not applied"
            ),
        )
    except Exception:
        logger.exception("Failed to record approval_approved audit event")

    return updated


def _audit(approval, event_type, message):
    try:
        create_audit_event(
            job_id=approval["backup_job_id"],
            device_id=approval["device_id"],
            event_type=event_type,
            message=message,
        )
    except Exception:
        logger.exception("Failed to record %s audit event", event_type)


@router.post("/{approval_id}/apply", status_code=202)
def apply(approval_id: uuid.UUID):
    # No request body: the device, commands and backup all come from the stored approval.
    approval_key = str(approval_id)

    try:
        approval = get_approval(approval_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_key} not found")

    if approval["status"] != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"Approval is {approval['status']}; only approved changes can be applied",
        )

    device = get_device_by_id(approval["device_id"])

    if device is None or device["platform"] not in SUPPORTED_CHANGE_PLATFORMS:
        raise HTTPException(status_code=422, detail="Configuration changes are not supported for this device's platform")

    if not device["enabled"]:
        raise HTTPException(status_code=422, detail=f"{device['hostname']} is disabled")

    if not approval["backup_job_id"] or not approval["command_count"]:
        raise HTTPException(status_code=422, detail="Approval is missing its backup reference or configuration")

    # approved -> applying. Only one request can win this UPDATE, so only one task is enqueued.
    claimed = claim_approval_for_apply(approval_key)

    if claimed is None:
        raise HTTPException(status_code=409, detail="Approval is no longer approved (already applying?)")

    _audit(
        claimed,
        "apply_requested",
        f"Apply requested for approval {approval_key} on {device['hostname']} "
        f"(approved by {claimed['approved_by']}, {claimed['block_count']} block(s), {claimed['command_count']} command(s))",
    )

    try:
        task = celery_app.send_task(
            APPLY_TASK,
            kwargs={"approval_id": approval_key, "claimed_by_api": True},
            retry=True,
            retry_policy=PUBLISH_RETRY_POLICY,
        )
    except Exception as exc:
        logger.exception("Failed to enqueue apply for approval %s", approval_key)

        # Nothing reached the worker, so return the approval to "approved" for a clean retry.
        # If the message was in fact delivered, the worker rejects it: the status is no longer
        # "applying", so it will not touch the device.
        released = release_apply_claim(approval_key)

        _audit(
            claimed,
            "apply_enqueue_failed",
            f"Apply for approval {approval_key} could not be queued; "
            f"status {'returned to approved' if released else 'unchanged'}",
        )

        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {exc}")

    return {
        "approval_id": approval_key,
        "request_id": task.id,
        "status": "applying",
        "device_id": device["id"],
        "hostname": device["hostname"],
    }


@router.get("/{approval_id}/apply/{request_id}")
def apply_status(approval_id: uuid.UUID, request_id: uuid.UUID):
    approval = get_approval(str(approval_id))

    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")

    try:
        task = AsyncResult(str(request_id), app=celery_app)
        task_state = APPLY_STATE_MAP.get(task.state, "running")
        raw_result = task.result
    except Exception as exc:
        logger.exception("Failed to read apply task state")
        raise HTTPException(status_code=503, detail=f"Result backend unavailable: {exc}")

    error = None
    if task_state == "failed":
        error = str(raw_result)[:MAX_ERROR_LENGTH] if raw_result else "Apply failed"

    # The approval status in PostgreSQL is the source of truth; the task state adds detail.
    return {
        "approval_id": str(approval_id),
        "request_id": str(request_id),
        "approval_status": approval["status"],
        "task_state": task_state,
        "error": error,
        # Per-block execution progress / partial apply / post-check (stored by the worker).
        "execution_result": approval.get("execution_result"),
    }


CANCELLABLE_STATUSES = ("pending", "approved")


class CancelRequest(BaseModel):
    # Identity is self-asserted until authentication exists (same rule as approved_by).
    cancelled_by: str = Field(pattern=r"^[A-Za-z0-9._@-]{2,64}$")
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value):
        if value is None:
            return None
        value = value.strip()
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
            raise ValueError("reason contains control characters")
        return value or None


@router.post("/{approval_id}/cancel")
def cancel(approval_id: uuid.UUID, request: CancelRequest):
    # Status change only: never enqueues a task and never touches a switch.
    approval_key = str(approval_id)

    try:
        approval = get_approval(approval_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_key} not found")

    if approval["status"] not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Approval is {approval['status']}; only pending or approved changes can be cancelled",
        )

    # Only one of cancel / cancel / apply-claim can win this row (status IN pending, approved).
    updated = cancel_open_approval(approval_key, request.cancelled_by, request.reason)

    if updated is None:
        current = get_approval(approval_key)
        status = current["status"] if current else "unknown"
        raise HTTPException(
            status_code=409,
            detail=f"Approval is {status}; only pending or approved changes can be cancelled",
        )

    device = get_device_by_id(updated["device_id"])
    hostname = device["hostname"] if device else f"device {updated['device_id']}"

    _audit(
        updated,
        "approval_cancelled",
        f"Change approval {approval_key} for {hostname} cancelled by {request.cancelled_by} "
        f"(was {approval['status']})"
        + (f": {request.reason}" if request.reason else "")
        + "; no configuration was applied",
    )

    return updated
