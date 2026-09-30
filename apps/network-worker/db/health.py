from db.client import get_connection


HEALTH_COLUMNS = """
    device_id,
    status,
    last_check_at,
    last_success_at,
    response_time_ms,
    tcp_reachable,
    ssh_reachable,
    cli_reachable,
    last_error,
    consecutive_failures,
    last_status_change_at
"""


def _iso(value):
    return value.isoformat() if value else None


def _row_to_health(row, iso=True):
    fmt = _iso if iso else (lambda value: value)

    return {
        "device_id": row[0],
        "status": row[1],
        "last_check_at": fmt(row[2]),
        "last_success_at": fmt(row[3]),
        "response_time_ms": row[4],
        "tcp_reachable": row[5],
        "ssh_reachable": row[6],
        "cli_reachable": row[7],
        "last_error": row[8],
        "consecutive_failures": row[9],
        "last_status_change_at": fmt(row[10]),
    }


def list_enabled_devices():
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    hostname,
                    management_ip,
                    platform,
                    credential_path
                FROM devices
                WHERE enabled = TRUE
                ORDER BY hostname
                """
            )

            return [
                {
                    "id": row[0],
                    "hostname": row[1],
                    "management_ip": str(row[2]),
                    "platform": row[3],
                    "credential_path": row[4],
                }
                for row in cur.fetchall()
            ]

    finally:
        conn.close()


def record_health(device_id, classify):
    """
    Persist one health result atomically.

    classify(previous) receives the current row (dict with datetime values, or None),
    locked FOR UPDATE, and returns the new column values. Returns (previous, current)
    as JSON-ready dicts.
    """
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {HEALTH_COLUMNS} FROM device_health WHERE device_id = %s FOR UPDATE",
                (device_id,),
            )

            row = cur.fetchone()
            previous = _row_to_health(row) if row else None

            new = classify(_row_to_health(row, iso=False) if row else None)

            cur.execute(
                f"""
                INSERT INTO device_health (
                    device_id,
                    status,
                    last_check_at,
                    last_success_at,
                    response_time_ms,
                    tcp_reachable,
                    ssh_reachable,
                    cli_reachable,
                    last_error,
                    consecutive_failures,
                    last_status_change_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (device_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    last_check_at = EXCLUDED.last_check_at,
                    last_success_at = EXCLUDED.last_success_at,
                    response_time_ms = EXCLUDED.response_time_ms,
                    tcp_reachable = EXCLUDED.tcp_reachable,
                    ssh_reachable = EXCLUDED.ssh_reachable,
                    cli_reachable = EXCLUDED.cli_reachable,
                    last_error = EXCLUDED.last_error,
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    last_status_change_at = EXCLUDED.last_status_change_at,
                    updated_at = NOW()
                RETURNING {HEALTH_COLUMNS}
                """,
                (
                    device_id,
                    new["status"],
                    new["last_check_at"],
                    new["last_success_at"],
                    new["response_time_ms"],
                    new["tcp_reachable"],
                    new["ssh_reachable"],
                    new["cli_reachable"],
                    new["last_error"],
                    new["consecutive_failures"],
                    new["last_status_change_at"],
                ),
            )

            current = _row_to_health(cur.fetchone())

        conn.commit()

        return previous, current

    finally:
        conn.close()


def fleet_health_summary():
    """Counts over enabled devices; devices without a health row are 'unknown'."""
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(h.status, 'unknown') AS status,
                    count(*),
                    max(h.last_check_at)
                FROM devices d
                LEFT JOIN device_health h ON h.device_id = d.id
                WHERE d.enabled = TRUE
                GROUP BY 1
                """
            )

            rows = cur.fetchall()

    finally:
        conn.close()

    summary = {"total": 0, "healthy": 0, "degraded": 0, "down": 0, "unknown": 0, "last_updated": None}
    last_updated = None

    for status, count, last_check in rows:
        summary[status] = count
        summary["total"] += count
        if last_check and (last_updated is None or last_check > last_updated):
            last_updated = last_check

    summary["last_updated"] = _iso(last_updated)

    return summary
