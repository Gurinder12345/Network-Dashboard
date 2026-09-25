from app.db.client import get_connection


def list_audit_events():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    job_id,
                    device_id,
                    event_type,
                    message,
                    created_at
                FROM audit_events
                ORDER BY created_at DESC
                """
            )

            rows = cur.fetchall()

            return [
                {
                    "id": row[0],
                    "job_id": str(row[1]) if row[1] else None,
                    "device_id": row[2],
                    "event_type": row[3],
                    "message": row[4],
                    "created_at": row[5].isoformat() if row[5] else None,
                }
                for row in rows
            ]


def create_audit_event(job_id, device_id, event_type, message):
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
