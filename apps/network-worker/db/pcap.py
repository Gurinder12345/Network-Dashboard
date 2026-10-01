import json

from db.client import get_connection

ACTIVE = ("queued", "validating", "extracting", "analyzing", "correlating")


def get_analysis(analysis_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, mode, client_file, server_file, created_at, expires_at, files_deleted_at "
                "FROM pcap_analyses WHERE id = %s",
                (analysis_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    keys = ("id", "status", "mode", "client_file", "server_file", "created_at", "expires_at", "files_deleted_at")
    return dict(zip(keys, row))


def claim(analysis_id):
    """queued -> validating. False if it is not (or no longer) queued (e.g. broker redelivery)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pcap_analyses SET status = 'validating', started_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND status = 'queued'",
                (analysis_id,),
            )
            return cur.rowcount == 1


def set_stage(analysis_id, stage):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pcap_analyses SET status = %s, updated_at = NOW() WHERE id = %s AND status NOT IN ('completed', 'failed')",
                (stage, analysis_id),
            )


def complete(analysis_id, result, packet_count, flow_count, duration_s, likely_issue, finding_counts):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pcap_analyses
                SET status = 'completed', completed_at = NOW(), updated_at = NOW(), result_json = %s,
                    packet_count = %s, flow_count = %s, capture_duration_seconds = %s,
                    likely_issue = %s, finding_counts = %s, error = NULL
                WHERE id = %s
                """,
                (json.dumps(result), packet_count, flow_count, duration_s, likely_issue, json.dumps(finding_counts), analysis_id),
            )


def fail(analysis_id, error):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pcap_analyses SET status = 'failed', completed_at = NOW(), updated_at = NOW(), error = %s WHERE id = %s",
                ((error or "Analysis failed")[:1024], analysis_id),
            )


def mark_files_deleted(analysis_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pcap_analyses SET files_deleted_at = NOW(), updated_at = NOW() WHERE id = %s AND files_deleted_at IS NULL",
                (analysis_id,),
            )


def fail_stale(older_than_seconds):
    """Active analyses whose worker never finished (crash/restart). Returns their ids."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pcap_analyses
                SET status = 'failed', completed_at = NOW(), updated_at = NOW(),
                    error = 'Analysis did not finish (worker restarted or time limit exceeded)'
                WHERE status = ANY(%s) AND updated_at < NOW() - make_interval(secs => %s)
                RETURNING id
                """,
                (list(ACTIVE), older_than_seconds),
            )
            return [str(r[0]) for r in cur.fetchall()]
