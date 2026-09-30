import logging

import psycopg

from app.db.client import get_connection


logger = logging.getLogger("network_api.topology")


def _iso(value):
    return value.isoformat() if value else None


def list_links(include_inactive=False):
    query = """
        SELECT
            id, local_device_id, local_interface, remote_device_id,
            remote_system_name, remote_interface, remote_management_ip,
            remote_chassis_id, remote_port_id, remote_capabilities,
            protocol, first_seen_at, last_seen_at, active
        FROM topology_links
    """
    if not include_inactive:
        query += " WHERE active"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                return [
                    {
                        "id": str(row[0]),
                        "local_device_id": row[1],
                        "local_interface": row[2],
                        "remote_device_id": row[3],
                        "remote_system_name": row[4],
                        "remote_interface": row[5],
                        "remote_management_ip": str(row[6]) if row[6] else None,
                        "remote_chassis_id": row[7],
                        "remote_port_id": row[8],
                        "remote_capabilities": row[9],
                        "protocol": row[10],
                        "first_seen_at": _iso(row[11]),
                        "last_seen_at": _iso(row[12]),
                        "active": row[13],
                    }
                    for row in cur.fetchall()
                ]
    except psycopg.errors.UndefinedTable:
        logger.warning("topology tables missing; run migration 003")
        return []


def list_discovery_states():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT device_id, last_attempt_at, last_success_at, last_error, neighbors_seen FROM topology_discovery_state"
                )
                return {
                    row[0]: {
                        "last_attempt_at": _iso(row[1]),
                        "last_success_at": _iso(row[2]),
                        "last_error": row[3],
                        "neighbors_seen": row[4],
                    }
                    for row in cur.fetchall()
                }
    except psycopg.errors.UndefinedTable:
        logger.warning("topology tables missing; run migration 003")
        return {}
