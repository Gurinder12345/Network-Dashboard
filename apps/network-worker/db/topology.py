from db.client import get_connection


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
