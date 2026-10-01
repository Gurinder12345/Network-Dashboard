from app.db.client import get_connection


def _num(value):
    return float(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value else None


def latest_metrics(device_id):
    """Newest collection attempt plus the time of the newest success/partial one."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT collected_at, collection_status, cpu_percent, memory_percent,
                       memory_used_mb, memory_total_mb, uptime_seconds, error,
                       (SELECT MAX(collected_at) FROM device_metrics s
                         WHERE s.device_id = m.device_id AND s.collection_status <> 'failed')
                FROM device_metrics m
                WHERE device_id = %s
                ORDER BY collected_at DESC
                LIMIT 1
                """,
                (device_id,),
            )
            row = cur.fetchone()

    if row is None:
        return None

    return {
        "device_id": device_id,
        "collected_at": _iso(row[0]),
        "status": row[1],
        "cpu_percent": _num(row[2]),
        "memory_percent": _num(row[3]),
        "memory_used_mb": _num(row[4]),
        "memory_total_mb": _num(row[5]),
        "uptime_seconds": row[6],
        "error": row[7],
        "last_success_at": _iso(row[8]),
    }


def metric_history(device_id, seconds, bucket_seconds=None):
    """
    Samples in the last `seconds`, oldest first. Failed attempts come back with NULL
    metrics (a visible gap). With bucket_seconds, samples are averaged per fixed bucket
    aligned to the Unix epoch; NULLs are ignored by AVG, so a bucket of only failed
    attempts is NULL and a bucket with no attempts is absent.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if bucket_seconds is None:
                cur.execute(
                    """
                    SELECT collected_at, cpu_percent, memory_percent, memory_used_mb, memory_total_mb, collection_status
                    FROM device_metrics
                    WHERE device_id = %s AND collected_at >= NOW() - make_interval(secs => %s)
                    ORDER BY collected_at
                    """,
                    (device_id, seconds),
                )
            else:
                cur.execute(
                    """
                    SELECT to_timestamp(floor(extract(epoch FROM collected_at) / %s) * %s) AS bucket,
                           ROUND(AVG(cpu_percent), 2), ROUND(AVG(memory_percent), 2),
                           ROUND(AVG(memory_used_mb), 1), ROUND(AVG(memory_total_mb), 1), NULL
                    FROM device_metrics
                    WHERE device_id = %s AND collected_at >= NOW() - make_interval(secs => %s)
                    GROUP BY bucket
                    ORDER BY bucket
                    """,
                    (bucket_seconds, bucket_seconds, device_id, seconds),
                )
            rows = cur.fetchall()

    return [
        {
            "collected_at": _iso(row[0]),
            "cpu_percent": _num(row[1]),
            "memory_percent": _num(row[2]),
            "memory_used_mb": _num(row[3]),
            "memory_total_mb": _num(row[4]),
            "status": row[5],
        }
        for row in rows
    ]
