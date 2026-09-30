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
                WHERE j.id = %s
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
