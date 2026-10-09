import logging

import psycopg

from app.db.client import get_connection


logger = logging.getLogger("network_api.health")

BASE_COLUMNS = (
    "device_id", "status", "last_check_at", "last_success_at", "response_time_ms",
    "tcp_reachable",  # TCP/22 only
    "ssh_reachable",  # SSH session established and authenticated
    "cli_reachable",  # verification command returned usable output
    "last_error", "consecutive_failures", "last_status_change_at",
)
# Migration 010 (health evidence); NULL / absent before it is applied.
EVIDENCE_COLUMNS = ("icmp_reachable", "health_reason")
TIME_COLUMNS = {"last_check_at", "last_success_at", "last_status_change_at"}
HEALTH_COLUMNS = ", ".join(BASE_COLUMNS)


def _iso(value):
    return value.isoformat() if value else None


def _row_to_health(row, columns=BASE_COLUMNS):
    health = {name: (_iso(value) if name in TIME_COLUMNS else value) for name, value in zip(columns, row)}
    for name in EVIDENCE_COLUMNS:
        health.setdefault(name, None)
    return health


def get_health_rows(device_ids=None):
    """
    Health rows keyed by device_id. Devices without a row are simply absent (= unknown).
    If the device_health migration has not been applied yet, returns {} instead of failing.
    """
    where, params = "", ()

    if device_ids is not None:
        if not device_ids:
            return {}
        where, params = " WHERE device_id = ANY(%s)", (list(device_ids),)

    for columns in (BASE_COLUMNS + EVIDENCE_COLUMNS, BASE_COLUMNS):
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {', '.join(columns)} FROM device_health{where}", params)
                    return {row[0]: _row_to_health(row, columns) for row in cur.fetchall()}
        except psycopg.errors.UndefinedColumn:
            logger.warning("device_health evidence columns missing (run migration 010); serving the original columns")
            continue
        except psycopg.errors.UndefinedTable:
            logger.warning("device_health table missing; reporting all devices as unknown (run migration 001)")
            return {}
    return {}
