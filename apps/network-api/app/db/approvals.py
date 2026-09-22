from app.db.client import get_connection


def list_approvals():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    device_id,
                    backup_job_id,
                    requested_by,
                    approved_by,
                    status,
                    config_lines,
                    config_parents,
                    created_at,
                    approved_at
                FROM change_approvals
                ORDER BY created_at DESC
                """
            )

            rows = cur.fetchall()

            return [
                {
                    "id": str(row[0]),
                    "device_id": row[1],
                    "backup_job_id": str(row[2]) if row[2] else None,
                    "requested_by": row[3],
                    "approved_by": row[4],
                    "status": row[5],
                    "config_lines": row[6],
                    "config_parents": row[7],
                    "created_at": row[8].isoformat() if row[8] else None,
                    "approved_at": row[9].isoformat() if row[9] else None,
                }
                for row in rows
            ]
