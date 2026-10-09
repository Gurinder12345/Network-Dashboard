from db.client import get_connection


import logging
import time

logger = logging.getLogger("network_worker.health")

BASE_COLUMNS = (
    "device_id",
    "status",
    "last_check_at",
    "last_success_at",
    "response_time_ms",
    "tcp_reachable",  # TCP/22 only
    "ssh_reachable",  # SSH session established and authenticated
    "cli_reachable",  # verification command returned usable output
    "last_error",
    "consecutive_failures",
    "last_status_change_at",
)
# Migration 010. Written/read only when present, so health keeps working if the image
# is deployed before the migration.
EVIDENCE_COLUMNS = ("icmp_reachable", "health_reason", "unreachable_count")
TIME_COLUMNS = {"last_check_at", "last_success_at", "last_status_change_at"}
HEALTH_COLUMNS = ", ".join(BASE_COLUMNS)

_EVIDENCE_RECHECK_SECONDS = 300
_evidence = {"available": None, "checked_at": 0.0}


def _iso(value):
    return value.isoformat() if value else None


def _row_to_health(row, iso=True, columns=BASE_COLUMNS):
    return {name: (_iso(value) if iso and name in TIME_COLUMNS else value) for name, value in zip(columns, row)}


def _columns(cur):
    """Base columns plus the migration-010 evidence columns when they exist (cached)."""
    now = time.monotonic()
    if _evidence["available"] is None or (not _evidence["available"] and now - _evidence["checked_at"] > _EVIDENCE_RECHECK_SECONDS):
        cur.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'device_health' AND column_name = ANY(%s)",
            (list(EVIDENCE_COLUMNS),),
        )
        _evidence["available"] = cur.fetchone()[0] == len(EVIDENCE_COLUMNS)
        _evidence["checked_at"] = now
        if not _evidence["available"]:
            logger.warning("device_health_evidence_columns_missing hint=apply migration 010; writing original columns only")
    return BASE_COLUMNS + (EVIDENCE_COLUMNS if _evidence["available"] else ())


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
            columns = _columns(cur)
            names = ", ".join(columns)
            cur.execute(f"SELECT {names} FROM device_health WHERE device_id = %s FOR UPDATE", (device_id,))

            row = cur.fetchone()
            previous = _row_to_health(row, columns=columns) if row else None

            new = classify(_row_to_health(row, iso=False, columns=columns) if row else None)

            written = columns[1:]
            cur.execute(
                f"""
                INSERT INTO device_health (device_id, {", ".join(written)}, updated_at)
                VALUES (%s, {", ".join(["%s"] * len(written))}, NOW())
                ON CONFLICT (device_id) DO UPDATE SET
                    {", ".join(f"{c} = EXCLUDED.{c}" for c in written)},
                    updated_at = NOW()
                RETURNING {names}
                """,
                (device_id, *[new.get(c) for c in written]),
            )

            current = _row_to_health(cur.fetchone(), columns=columns)

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
