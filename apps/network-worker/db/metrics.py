from db.client import get_connection


def insert_metric(sample):
    """One collection attempt. Failed attempts carry NULL metrics, never zeros."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_metrics (
                    device_id, collected_at, cpu_percent, memory_percent, memory_used_mb,
                    memory_total_mb, uptime_seconds, source, collection_status, error
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    sample["device_id"],
                    sample["collected_at"],
                    sample["cpu_percent"],
                    sample["memory_percent"],
                    sample["memory_used_mb"],
                    sample["memory_total_mb"],
                    sample["uptime_seconds"],
                    sample["source"],
                    sample["status"],
                    sample["error"],
                ),
            )
            return cur.fetchone()[0]


def last_success_at(device_id):
    """collected_at of the newest success/partial sample (uses the device/time index)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT collected_at
                FROM device_metrics
                WHERE device_id = %s AND collection_status <> 'failed'
                ORDER BY collected_at DESC
                LIMIT 1
                """,
                (device_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
