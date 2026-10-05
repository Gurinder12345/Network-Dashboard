"""
Manual job deletion (cleanup only).

DELETE /api/v1/jobs/{job_id}
    204  finished job hidden from the Jobs list (soft delete; see db/migrations/007)
    404  no such (visible) job
    409  job still active, or it is the backup of an approval still in flight
    400  malformed job id

Nothing else is removed: backups, backup files, approvals, audit events, PCAP analyses
and devices are untouched, and task execution is not affected. When dashboard auth/RBAC
exists, protect this route with a router dependency (e.g. require an operator role) and
pass the signed-in user as deleted_by.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Response

from app.db.jobs import delete_job

logger = logging.getLogger("network_api.jobs")

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

REQUESTED_BY = "dashboard"  # no dashboard authentication yet


@router.delete("/{job_id}", status_code=204)
def delete_job_route(job_id: str):
    try:
        job_id = str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job id")

    try:
        result = delete_job(job_id, REQUESTED_BY)
    except Exception:
        logger.exception("job deletion failed job_id=%s", job_id)
        raise HTTPException(status_code=500, detail="Unable to delete job")

    outcome = result["outcome"]
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Job no longer exists")
    if outcome == "active":
        raise HTTPException(status_code=409, detail=f"Job cannot be deleted while status is {result['status']}.")
    if outcome == "in_use":
        raise HTTPException(
            status_code=409,
            detail=f"Job cannot be deleted: it is the pre-change backup of approval {result['approval_id']}, "
                   f"which is still {result['approval_status']}.",
        )

    logger.info("job_deleted job_id=%s job_type=%s previous_status=%s", job_id, result["job_type"], result["previous_status"])
    return Response(status_code=204)
