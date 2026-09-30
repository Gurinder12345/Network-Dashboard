import logging

import psycopg

from app.db.client import get_connection


logger = logging.getLogger("network_api.health")

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


def _row_to_health(row):
    return {
        "device_id": row[0],
        "status": row[1],
        "last_check_at": _iso(row[2]),
        "last_success_at": _iso(row[3]),
        "response_time_ms": row[4],
        "tcp_reachable": row[5],
        "ssh_reachable": row[6],
        "cli_reachable": row[7],
        "last_error": row[8],
        "consecutive_failures": row[9],
        "last_status_change_at": _iso(row[10]),
    }


def get_health_rows(device_ids=None):
    """
    Health rows keyed by device_id. Devices without a row are simply absent (= unknown).
    If the device_health migration has not been applied yet, returns {} instead of failing.
    """
    query = f"SELECT {HEALTH_COLUMNS} FROM device_health"
    params = ()

    if device_ids is not None:
        if not device_ids:
            return {}
        query += " WHERE device_id = ANY(%s)"
        params = (list(device_ids),)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return {row[0]: _row_to_health(row) for row in cur.fetchall()}
    except psycopg.errors.UndefinedTable:
        logger.warning("device_health table missing; reporting all devices as unknown (run migration 001)")
        return {}
