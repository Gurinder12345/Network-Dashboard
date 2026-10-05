from app.db.client import get_connection


def list_jobs():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    j.id,
                    j.device_id,
                    j.job_type,
                    j.status,
                    j.requested_by,
                    j.started_at,
                    j.finished_at,
                    j.error_message,
                    (SELECT MAX(b.id) FROM backups b WHERE b.job_id = j.id)
                FROM jobs j
                WHERE j.deleted_at IS NULL
                ORDER BY j.started_at DESC
                """
            )

            rows = cur.fetchall()

            return [
                {
                    "id": str(row[0]),
                    "device_id": row[1],
                    "job_type": row[2],
                    "status": row[3],
                    "requested_by": row[4],
                    "started_at": row[5].isoformat() if row[5] else None,
                    "finished_at": row[6].isoformat() if row[6] else None,
                    "error_message": row[7],
                    "backup_id": row[8],
                }
                for row in rows
            ]


def create_queued_job(job_id, device_id, job_type, requested_by):
    """Job row for work the API enqueues; the worker moves it queued -> running -> success/failed."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (id, device_id, job_type, status, requested_by, started_at)
                VALUES (%s, %s, %s, 'queued', %s, NOW())
                """,
                (job_id, device_id, job_type, requested_by),
            )


def mark_job_failed(job_id, error_message):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    finished_at = NOW(),
                    error_message = %s
                WHERE id = %s
                """,
                (error_message, job_id),
            )


def get_job(job_id):
    """One job with its device and (for backup jobs) the backup it produced."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    j.id,
                    j.device_id,
                    d.hostname,
                    j.job_type,
                    j.status,
                    j.requested_by,
                    j.started_at,
                    j.finished_at,
                    j.error_message,
                    b.id,
                    b.storage_path,
                    b.created_at
                FROM jobs j
                LEFT JOIN devices d ON d.id = j.device_id
                LEFT JOIN LATERAL (
                    SELECT id, storage_path, created_at
                    FROM backups
                    WHERE job_id = j.id
                    ORDER BY id DESC
                    LIMIT 1
                ) b ON TRUE
                WHERE j.id = %s AND j.deleted_at IS NULL
                """,
                (job_id,),
            )

            row = cur.fetchone()

            if row is None:
                return None

            return {
                "id": str(row[0]),
                "device_id": row[1],
                "hostname": row[2],
                "job_type": row[3],
                "status": row[4],
                "requested_by": row[5],
                "started_at": row[6].isoformat() if row[6] else None,
                "finished_at": row[7].isoformat() if row[7] else None,
                "error_message": row[8],
                "backup_id": row[9],
                "backup_storage_path": row[10],
                "backup_created_at": row[11].isoformat() if row[11] else None,
            }


# Finished states. Anything else (queued, running, or an unknown/transitional value) is
# treated as active and cannot be deleted. The worker/API only write queued -> running ->
# success | failed; completed/cancelled are accepted in case older rows use them.
DELETABLE_STATUSES = ("success", "failed", "completed", "cancelled")
# A job that is the pre-change backup of an approval still in flight must stay visible:
# apply verifies that backup through the job.
ACTIVE_APPROVAL_STATUSES = ("pending", "approved", "applying")


def delete_job(job_id, deleted_by):
    """
    Soft-delete one finished job: hide it from job lists, keep every related row.

    One transaction: lock the job row (FOR UPDATE), re-check its status and approval use
    against the current database state (never the UI's copy), write the job_deleted audit
    event, then set deleted_at. A concurrent status change either commits first (and is
    seen here) or waits for this transaction.

    Returns {"outcome": "deleted" | "not_found" | "active" | "in_use", ...}.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_type, status, device_id FROM jobs WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"outcome": "not_found"}

            job_type, status, device_id = row
            if status not in DELETABLE_STATUSES:
                return {"outcome": "active", "status": status}

            cur.execute(
                "SELECT id, status FROM change_approvals WHERE backup_job_id = %s AND status = ANY(%s) LIMIT 1",
                (job_id, list(ACTIVE_APPROVAL_STATUSES)),
            )
            approval = cur.fetchone()
            if approval is not None:
                return {"outcome": "in_use", "approval_id": str(approval[0]), "approval_status": approval[1]}

            cur.execute("SELECT NOW()")
            deleted_at = cur.fetchone()[0]

            # The job row stays (soft delete), so the audit FK to it remains valid.
            cur.execute(
                "INSERT INTO audit_events (job_id, device_id, event_type, message) VALUES (%s, %s, 'job_deleted', %s)",
                (
                    job_id,
                    device_id,
                    f"Job {job_id} deleted from the Jobs list (job_type={job_type}, previous_status={status}, "
                    f"device_id={device_id}, deleted_at={deleted_at.isoformat()}, deleted_by={deleted_by}). "
                    "Related backups, approvals and audit history are kept.",
                ),
            )
            cur.execute(
                "UPDATE jobs SET deleted_at = %s, deleted_by = %s WHERE id = %s",
                (deleted_at, deleted_by, job_id),
            )

    return {"outcome": "deleted", "job_type": job_type, "previous_status": status, "device_id": device_id,
            "deleted_at": deleted_at.isoformat()}
