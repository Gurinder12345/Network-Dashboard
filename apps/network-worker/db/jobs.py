import uuid
from db.client import get_connection


def create_job(device_id, job_type, requested_by="system"):
    job_id = uuid.uuid4()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    id,
                    device_id,
                    job_type,
                    status,
                    requested_by,
                    started_at
                )
                VALUES (%s, %s, %s, %s, %s, NOW())
                """,
                (
                    job_id,
                    device_id,
                    job_type,
                    "running",
                    requested_by,
                ),
            )

    return str(job_id)


def mark_job_running(job_id):
    """queued -> running for a job the API created. False if it is not (or no longer) queued."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    started_at = NOW()
                WHERE id = %s
                  AND status = 'queued'
                """,
                (job_id,),
            )

            return cur.rowcount == 1


def mark_job_success(job_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'success',
                    finished_at = NOW()
                WHERE id = %s
                """,
                (job_id,),
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
                (
                    error_message,
                    job_id,
                ),
            )


def create_audit_event(
    job_id,
    device_id,
    event_type,
    message,
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_events (
                    job_id,
                    device_id,
                    event_type,
                    message
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    job_id,
                    device_id,
                    event_type,
                    message,
                ),
            )


def create_backup_record(
    job_id,
    device_id,
    storage_path,
    checksum,
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO backups (
                    job_id,
                    device_id,
                    backup_type,
                    storage_path,
                    checksum
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    job_id,
                    device_id,
                    "running-config",
                    storage_path,
                    checksum,
                ),
            )

            backup_id, created_at = cur.fetchone()

    return {
        "backup_id": backup_id,
        "created_at": created_at.isoformat() if created_at else None,
    }
