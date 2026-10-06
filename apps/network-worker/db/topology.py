import logging
import os

import psycopg

from db.client import get_connection
from topology.identity import normalize_chassis_id, normalize_system_name


logger = logging.getLogger("network_worker.topology")

_IDENTITY_COLUMNS = "id, device_id, lldp_system_name, chassis_id, created_at, updated_at"


def _cut(value, length):
    return value[:length] if isinstance(value, str) else value


def list_inventory():
    """All inventory devices (enabled or not) for neighbor correlation."""
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, hostname, management_ip, platform FROM devices")
            return [
                {"id": row[0], "hostname": row[1], "management_ip": str(row[2]) if row[2] else None, "platform": row[3]}
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def get_discovery_state(device_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_attempt_at, last_success_at, last_error FROM topology_discovery_state WHERE device_id = %s",
                (device_id,),
            )
            row = cur.fetchone()
            return {"last_attempt_at": row[0], "last_success_at": row[1], "last_error": row[2]} if row else None
    finally:
        conn.close()


def record_discovery_success(device_id, neighbors, discovery_source, seen_at):
    """
    One transaction per device: upsert every observed neighbor, then deactivate this
    device's links that were NOT seen in this run, then record success.
    Only ever called after collection and parsing succeeded.
    Returns {"upserted", "new", "deactivated"}.
    """
    conn = get_connection()
    new_links = 0

    try:
        with conn.cursor() as cur:
            for n in neighbors:
                cur.execute(
                    """
                    INSERT INTO topology_links (
                        local_device_id, local_interface, remote_device_id,
                        remote_system_name, remote_interface, remote_management_ip,
                        remote_chassis_id, remote_port_id, remote_capabilities,
                        protocol, discovery_source, first_seen_at, last_seen_at, active
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
                    ON CONFLICT ON CONSTRAINT topology_links_observation_uq DO UPDATE SET
                        remote_device_id = EXCLUDED.remote_device_id,
                        remote_system_name = EXCLUDED.remote_system_name,
                        remote_interface = EXCLUDED.remote_interface,
                        remote_management_ip = EXCLUDED.remote_management_ip,
                        remote_capabilities = EXCLUDED.remote_capabilities,
                        discovery_source = EXCLUDED.discovery_source,
                        last_seen_at = EXCLUDED.last_seen_at,
                        active = true
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (
                        device_id,
                        _cut(n["local_interface"], 128),
                        n.get("remote_device_id"),
                        _cut(n.get("remote_system_name"), 255),
                        _cut(n.get("remote_interface"), 128),
                        n.get("remote_management_ip"),
                        _cut(n.get("remote_chassis_id"), 255),
                        _cut(n.get("remote_port_id"), 255),
                        n.get("capabilities"),
                        n.get("protocol", "lldp"),
                        _cut(discovery_source, 64),
                        seen_at,
                        seen_at,
                    ),
                )
                if cur.fetchone()[0]:
                    new_links += 1

            cur.execute(
                """
                UPDATE topology_links
                SET active = false
                WHERE local_device_id = %s
                  AND active
                  AND last_seen_at < %s
                """,
                (device_id, seen_at),
            )
            deactivated = cur.rowcount

            cur.execute(
                """
                INSERT INTO topology_discovery_state
                    (device_id, last_attempt_at, last_success_at, last_error, neighbors_seen, updated_at)
                VALUES (%s, %s, %s, NULL, %s, NOW())
                ON CONFLICT (device_id) DO UPDATE SET
                    last_attempt_at = EXCLUDED.last_attempt_at,
                    last_success_at = EXCLUDED.last_success_at,
                    last_error = NULL,
                    neighbors_seen = EXCLUDED.neighbors_seen,
                    updated_at = NOW()
                """,
                (device_id, seen_at, seen_at, len(neighbors)),
            )

        conn.commit()

        return {"upserted": len(neighbors), "new": new_links, "deactivated": deactivated}

    finally:
        conn.close()


def record_discovery_failure(device_id, error, attempted_at):
    """Records the failed attempt only. Existing topology_links are left untouched."""
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO topology_discovery_state (device_id, last_attempt_at, last_error, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (device_id) DO UPDATE SET
                    last_attempt_at = EXCLUDED.last_attempt_at,
                    last_error = EXCLUDED.last_error,
                    updated_at = NOW()
                """,
                (device_id, attempted_at, _cut(error, 2048)),
            )

        conn.commit()

    finally:
        conn.close()


# ---- LLDP identities (read-only; rows are written by an operator) -------------------

def _identity_row(row):
    return {
        "id": str(row[0]),
        "device_id": row[1],
        "lldp_system_name": row[2],
        "chassis_id": row[3],
        "created_at": row[4].isoformat() if row[4] else None,
        "updated_at": row[5].isoformat() if row[5] else None,
    }


def _query_identities(where="", params=()):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_IDENTITY_COLUMNS} FROM device_lldp_identity {where} ORDER BY device_id", params)
            return [_identity_row(row) for row in cur.fetchall()]
    except psycopg.errors.UndefinedTable:
        # Migration 004 not applied yet: behave as "no explicit identities".
        logger.warning("device_lldp_identity table missing; explicit LLDP identities disabled (run migration 004)")
        return []
    finally:
        conn.close()


def list_lldp_identities():
    return _query_identities()


def get_lldp_identities_for_device(device_id):
    return _query_identities("WHERE device_id = %s", (device_id,))


def get_lldp_identity_by_system_name(name):
    normalized = normalize_system_name(name)
    if not normalized:
        return None
    rows = _query_identities("WHERE lower(lldp_system_name) = %s", (normalized,))
    return rows[0] if rows else None


def get_lldp_identity_by_chassis_id(chassis_id):
    normalized = normalize_chassis_id(chassis_id)
    if not normalized:
        return None
    rows = _query_identities("WHERE chassis_id = %s", (normalized,))
    return rows[0] if rows else None


# Inactive links seen this recently still count as "something is connected here" for the
# OS10 change safety check (a link that is down right now may come back).
STORED_LINK_RECENT_HOURS = int(os.getenv("OS10_STORED_LINK_RECENT_HOURS", "24"))


def list_links_for_interface(device_id, interface, recent_hours=None):
    """
    Stored LLDP links touching one interface of one device, from either end: links this
    device discovered on the interface, and links other devices discovered TO it. Active
    links, plus inactive ones seen within recent_hours. Returns neighbor dicts
    (remote_system_name, remote_interface, remote_device_id, remote_hostname, source).
    """
    hours = STORED_LINK_RECENT_HOURS if recent_hours is None else recent_hours
    interface = "".join(interface.lower().split())
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.local_device_id, ld.hostname, l.local_interface,
                       l.remote_device_id, rd.hostname, l.remote_system_name, l.remote_interface,
                       l.remote_port_id, l.remote_chassis_id, l.active,
                       l.local_device_id = %s AND lower(replace(l.local_interface, ' ', '')) = %s AS ours
                FROM topology_links l
                JOIN devices ld ON ld.id = l.local_device_id
                LEFT JOIN devices rd ON rd.id = l.remote_device_id
                WHERE (l.active OR l.last_seen_at > now() - make_interval(hours => %s))
                  AND ((l.local_device_id = %s AND lower(replace(l.local_interface, ' ', '')) = %s)
                    OR (l.remote_device_id = %s AND lower(replace(coalesce(l.remote_interface, ''), ' ', '')) = %s))
                ORDER BY l.active DESC, l.last_seen_at DESC
                """,
                (device_id, interface, hours, device_id, interface, device_id, interface),
            )
            links = []
            for row in cur.fetchall():
                (local_id, local_host, local_intf, remote_id, remote_host, remote_name, remote_intf,
                 remote_port, remote_chassis, active, ours) = row
                state = "active" if active else "recently seen"
                if ours:
                    links.append({"remote_system_name": remote_name, "remote_interface": remote_intf,
                                  "remote_port_id": remote_port, "remote_chassis_id": remote_chassis,
                                  "remote_device_id": remote_id, "remote_hostname": remote_host,
                                  "source": f"stored ({state})"})
                else:
                    links.append({"remote_system_name": local_host, "remote_interface": local_intf,
                                  "remote_device_id": local_id, "remote_hostname": local_host,
                                  "source": f"stored, reported by {local_host} ({state})"})
            return links
    finally:
        conn.close()
