import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.approvals import approve_pending_approval, get_approval
from app.db.audit import create_audit_event
from app.db.devices import get_device_by_id


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

OS6_PLATFORM = "dell_os6"


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

    if device is None or device["platform"] != OS6_PLATFORM:
        raise HTTPException(status_code=422, detail="Only OS6 change approvals can be approved")

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
                f"{request.approved_by} ({len(updated['config_lines'] or [])} line(s)); not applied"
            ),
        )
    except Exception:
        logger.exception("Failed to record approval_approved audit event")

    return updated
