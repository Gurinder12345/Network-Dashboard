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
                """,
                (
                    job_id,
                    device_id,
                    "running-config",
                    storage_path,
                    checksum,
                ),
            )
