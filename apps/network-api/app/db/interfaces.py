"""
Interface monitoring reads (migration 009). PostgreSQL is the source of truth; these are
used when Redis has no snapshot and for history. Every read is a single query with
explicit columns (no SELECT *), bounded by indexes:

    latest_device_interfaces  inventory rows of the device's newest collection + each
                              interface's two newest samples (window function), ONE query
    get_interface             one inventory row (detail fallback / history ownership check)
    interface_history         one interface's samples in a time range (interface/time index)
    latest_collection_times   newest collection time per device for many devices, ONE query
                              (fleet summary building block; no per-device calls)
"""

from app.db.client import get_connection

INVENTORY = ("id", "interface_name", "canonical_name", "description", "interface_type", "role", "admin_status",
             "oper_status", "speed_bps", "duplex", "mode", "access_vlan", "native_vlan", "allowed_vlans",
             "port_channel", "mtu", "last_state_change", "last_seen")
COUNTERS = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "crc_errors",
            "input_discards", "output_discards")
SAMPLE = ("collected_at",) + COUNTERS + ("rx_utilization_pct", "tx_utilization_pct")

_LATEST = f"""
WITH latest AS (
    SELECT max(last_seen) AS seen FROM interface_inventory WHERE device_id = %(device)s
), inv AS (
    SELECT {", ".join(f"i.{c}" for c in INVENTORY)}
    FROM interface_inventory i, latest
    WHERE i.device_id = %(device)s AND i.last_seen = latest.seen
), samples AS (
    SELECT m.interface_id, {", ".join(f"m.{c}" for c in SAMPLE)},
           row_number() OVER (PARTITION BY m.interface_id ORDER BY m.collected_at DESC) AS rn
    FROM interface_metrics m, latest
    WHERE m.device_id = %(device)s
      AND m.collected_at > latest.seen - make_interval(secs => %(lookback)s)
      AND m.collected_at <= latest.seen
)
SELECT {", ".join(f"inv.{c}" for c in INVENTORY)},
       {", ".join(f"cur.{c}" for c in SAMPLE)},
       {", ".join(f"prev.{c}" for c in SAMPLE)}
FROM inv
LEFT JOIN samples cur ON cur.interface_id = inv.id AND cur.rn = 1
LEFT JOIN samples prev ON prev.interface_id = inv.id AND prev.rn = 2
ORDER BY inv.canonical_name
"""


def _num(value):
    return float(value) if value is not None else None


def _sample(values):
    sample = dict(zip(SAMPLE, values))
    if sample["collected_at"] is None:
        return None
    for key in ("rx_utilization_pct", "tx_utilization_pct"):
        sample[key] = _num(sample[key])
    return sample


def latest_device_interfaces(device_id, lookback_seconds):
    """[(inventory dict, current sample | None, previous sample | None)] -- one query."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(_LATEST, {"device": device_id, "lookback": lookback_seconds})
        rows = cur.fetchall()
    n, s = len(INVENTORY), len(SAMPLE)
    return [(dict(zip(INVENTORY, row[:n])), _sample(row[n:n + s]), _sample(row[n + s:n + 2 * s])) for row in rows]


def get_interface(device_id, interface_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(INVENTORY)} FROM interface_inventory WHERE id = %s AND device_id = %s",
                    (interface_id, device_id))
        row = cur.fetchone()
    return dict(zip(INVENTORY, row)) if row else None


def interface_history(device_id, interface_id, start, end, limit):
    """Samples oldest first; only the columns charts need."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT collected_at, rx_utilization_pct, tx_utilization_pct, rx_errors, tx_errors, crc_errors,
                   input_discards, output_discards
            FROM interface_metrics
            WHERE interface_id = %s AND device_id = %s AND collected_at >= %s AND collected_at <= %s
            ORDER BY collected_at
            LIMIT %s
            """,
            (interface_id, device_id, start, end, limit),
        )
        rows = cur.fetchall()
    return [{"collected_at": r[0], "rx_utilization_pct": _num(r[1]), "tx_utilization_pct": _num(r[2]),
             "rx_errors": r[3], "tx_errors": r[4], "crc_errors": r[5], "input_discards": r[6], "output_discards": r[7]}
            for r in rows]


def latest_collection_times(device_ids):
    """{device_id: newest last_seen} for many devices in one query (future fleet summary)."""
    if not device_ids:
        return {}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT device_id, max(last_seen) FROM interface_inventory WHERE device_id = ANY(%s) GROUP BY device_id",
                    (list(device_ids),))
        return dict(cur.fetchall())
