import json

from app.db.client import get_connection

ACTIVE = ("queued", "validating", "extracting", "analyzing", "correlating")

LIST_COLUMNS = (
    "id", "status", "mode", "created_at", "started_at", "completed_at", "client_filename", "server_filename",
    "client_size_bytes", "server_size_bytes", "capture_duration_seconds", "packet_count", "flow_count",
    "likely_issue", "finding_counts", "error", "expires_at", "files_deleted_at", "created_by",
)


def _row(row, columns=LIST_COLUMNS):
    out = dict(zip(columns, row))
    out["id"] = str(out["id"])
    for key in ("created_at", "started_at", "completed_at", "expires_at", "files_deleted_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


def count_active():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pcap_analyses WHERE status = ANY(%s)", (list(ACTIVE),))
            return cur.fetchone()[0]


def insert_analysis(analysis_id, mode, files, created_by, retention_hours):
    client, server = files["client"], files.get("server")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pcap_analyses (id, status, mode, client_filename, server_filename, client_file, server_file,
                                           client_size_bytes, server_size_bytes, expires_at, created_by)
                VALUES (%s, 'queued', %s, %s, %s, %s, %s, %s, %s, NOW() + make_interval(hours => %s), %s)
                """,
                (analysis_id, mode, client["display_name"], server["display_name"] if server else None,
                 client["stored_name"], server["stored_name"] if server else None,
                 client["size"], server["size"] if server else None, retention_hours, created_by),
            )


def mark_failed(analysis_id, error):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pcap_analyses SET status = 'failed', completed_at = NOW(), updated_at = NOW(), error = %s WHERE id = %s",
                        (error, analysis_id))


def list_recent(limit=50):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(LIST_COLUMNS)} FROM pcap_analyses ORDER BY created_at DESC LIMIT %s", (limit,))
            return [_row(r) for r in cur.fetchall()]


def get_analysis(analysis_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(LIST_COLUMNS)}, result_json FROM pcap_analyses WHERE id = %s", (analysis_id,))
            row = cur.fetchone()
    if row is None:
        return None
    out = _row(row[:-1])
    result = row[-1]
    out["result"] = json.loads(result) if isinstance(result, str) else result
    return out


def delete_analysis(analysis_id):
    """Delete unless active. Returns 'deleted', 'active' or None (not found)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM pcap_analyses WHERE id = %s FOR UPDATE", (analysis_id,))
            row = cur.fetchone()
            if row is None:
                return None
            if row[0] in ACTIVE:
                return "active"
            cur.execute("DELETE FROM pcap_analyses WHERE id = %s", (analysis_id,))
            return "deleted"
