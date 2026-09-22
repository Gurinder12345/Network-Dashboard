from app.db.client import get_connection


def list_jobs():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    device_id,
                    job_type,
                    status,
                    requested_by,
                    started_at,
                    finished_at,
                    error_message
                FROM jobs
                ORDER BY started_at DESC
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
                }
                for row in rows
            ]
