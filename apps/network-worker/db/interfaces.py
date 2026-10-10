"""
Interface inventory + metrics persistence (migration 009). Bulk, one transaction per
device collection, a fixed number of statements regardless of interface count:

    1. topology roles      SELECT local interfaces with an active LLDP link to a managed device
    2. inventory upsert    INSERT ... SELECT FROM unnest(arrays) ON CONFLICT DO UPDATE RETURNING
    3. previous counters   SELECT DISTINCT ON (interface_id) ... (device/time index)
    4. metrics insert      INSERT ... SELECT FROM unnest(arrays)
    COMMIT

so a 48-port switch costs 4 round trips, not 48 x N.
"""

from db.client import get_connection

INVENTORY_COLUMNS = ("interface_name", "canonical_name", "description", "interface_type", "admin_status", "oper_status",
                     "speed_bps", "duplex", "mode", "access_vlan", "native_vlan", "allowed_vlans", "port_channel", "mtu",
                     "role", "last_state_change")
_INVENTORY_TYPES = ("varchar", "varchar", "varchar", "varchar", "varchar", "varchar", "bigint", "varchar", "varchar",
                    "int", "int", "varchar", "varchar", "int", "varchar", "timestamptz")
COUNTER_COLUMNS = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "crc_errors",
                   "input_discards", "output_discards")
METRIC_COLUMNS = COUNTER_COLUMNS + ("rx_utilization_pct", "tx_utilization_pct")

_UPSERT = f"""
INSERT INTO interface_inventory ({", ".join(INVENTORY_COLUMNS)}, device_id, first_seen, last_seen)
SELECT u.*, %s::bigint, %s::timestamptz, %s::timestamptz
FROM unnest({", ".join(f"%s::{t}[]" for t in _INVENTORY_TYPES)}) AS u({", ".join(INVENTORY_COLUMNS)})
ON CONFLICT (device_id, canonical_name) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in INVENTORY_COLUMNS if c not in ("canonical_name", "last_state_change"))},
    -- Device-reported change time wins; otherwise an oper-status flip observed between two
    -- polls is recorded at this poll (5-minute resolution); otherwise keep the old value.
    last_state_change = CASE
        WHEN EXCLUDED.last_state_change IS NOT NULL THEN EXCLUDED.last_state_change
        WHEN interface_inventory.oper_status IS NOT NULL AND EXCLUDED.oper_status IS NOT NULL
             AND interface_inventory.oper_status <> EXCLUDED.oper_status THEN EXCLUDED.last_seen
        ELSE interface_inventory.last_state_change
    END,
    last_seen = EXCLUDED.last_seen,
    updated_at = now()
RETURNING id, canonical_name, first_seen, last_state_change
"""

_PREVIOUS = f"""
SELECT DISTINCT ON (interface_id) interface_id, collected_at, {", ".join(COUNTER_COLUMNS)}
FROM interface_metrics
WHERE device_id = %s AND collected_at > %s::timestamptz - make_interval(secs => %s) AND collected_at < %s
ORDER BY interface_id, collected_at DESC
"""

_INSERT_METRICS = f"""
INSERT INTO interface_metrics (device_id, collected_at, interface_id, {", ".join(METRIC_COLUMNS)})
SELECT %s, %s, u.*
FROM unnest(%s::bigint[], {", ".join("%s::bigint[]" for _ in COUNTER_COLUMNS)}, %s::numeric[], %s::numeric[])
     AS u(interface_id, {", ".join(METRIC_COLUMNS)})
"""

_LLDP_PORTS = """
SELECT DISTINCT local_interface
FROM topology_links
WHERE local_device_id = %s AND active AND remote_device_id IS NOT NULL AND remote_device_id <> local_device_id
"""


def lldp_managed_ports(device_id):
    """Local interface names (as stored by topology discovery) with an active LLDP link to
    another managed device. Best effort: [] if the topology tables are unavailable."""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(_LLDP_PORTS, (device_id,))
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def persist_collection(device_id, collected_at, items, lookback_seconds, derive):
    """
    Upsert inventory, read each interface's previous counters, derive utilization via
    `derive(previous, item)`, insert one metrics row per interface; one transaction.
    Returns (rows, statements): rows = items enriched with `id`, `first_seen`,
    `last_state_change` and the derived fields.
    """
    statements = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            columns = [[item.get(c) for item in items] for c in INVENTORY_COLUMNS]
            cur.execute(_UPSERT, (device_id, collected_at, collected_at, *columns))
            statements += 1
            stored = {row[1]: row for row in cur.fetchall()}

            cur.execute(_PREVIOUS, (device_id, collected_at, lookback_seconds, collected_at))
            statements += 1
            previous = {
                row[0]: {"collected_at": row[1], **dict(zip(COUNTER_COLUMNS, row[2:]))}
                for row in cur.fetchall()
            }

            rows = []
            for item in items:
                interface_id, _, first_seen, last_state_change = stored[item["canonical_name"]]
                derived = derive(previous.get(interface_id), item)
                rows.append({**item, **derived, "id": interface_id, "first_seen": first_seen,
                             "last_state_change": last_state_change})

            cur.execute(_INSERT_METRICS, (
                device_id, collected_at, [r["id"] for r in rows],
                *[[r.get(c) for r in rows] for c in COUNTER_COLUMNS],
                [r.get("rx_utilization_pct") for r in rows], [r.get("tx_utilization_pct") for r in rows],
            ))
            statements += 1
        conn.commit()
    return rows, statements
