from app.db.client import get_connection


APPROVAL_COLUMNS = """
    id,
    device_id,
    backup_job_id,
    requested_by,
    approved_by,
    status,
    config_lines,
    config_parents,
    created_at,
    approved_at
"""


def _row_to_approval(row):
    return {
        "id": str(row[0]),
        "device_id": row[1],
        "backup_job_id": str(row[2]) if row[2] else None,
        "requested_by": row[3],
        "approved_by": row[4],
        "status": row[5],
        "config_lines": row[6],
        "config_parents": row[7],
        "created_at": row[8].isoformat() if row[8] else None,
        "approved_at": row[9].isoformat() if row[9] else None,
    }


def list_approvals():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    device_id,
                    backup_job_id,
                    requested_by,
                    approved_by,
                    status,
                    config_lines,
                    config_parents,
                    created_at,
                    approved_at
                FROM change_approvals
                ORDER BY created_at DESC
                """
            )

            rows = cur.fetchall()

            return [_row_to_approval(row) for row in rows]


def get_approval(approval_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {APPROVAL_COLUMNS} FROM change_approvals WHERE id = %s",
                (approval_id,),
            )

            row = cur.fetchone()

            return _row_to_approval(row) if row else None


def approve_pending_approval(approval_id, approved_by):
    """Atomically move pending -> approved. Returns None if the row is no longer pending."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE change_approvals
                SET
                    status = 'approved',
                    approved_by = %s,
                    approved_at = NOW()
                WHERE id = %s
                  AND status = 'pending'
                RETURNING {APPROVAL_COLUMNS}
                """,
                (approved_by, approval_id),
            )

            row = cur.fetchone()

            return _row_to_approval(row) if row else None


def claim_approval_for_apply(approval_id):
    """Atomically move approved -> applying. Returns None if another request already claimed it."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE change_approvals
                SET status = 'applying'
                WHERE id = %s
                  AND status = 'approved'
                RETURNING {APPROVAL_COLUMNS}
                """,
                (approval_id,),
            )

            row = cur.fetchone()

            return _row_to_approval(row) if row else None


def release_apply_claim(approval_id):
    """Undo a claim (applying -> approved) when the apply task could not be enqueued."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE change_approvals
                SET status = 'approved'
                WHERE id = %s
                  AND status = 'applying'
                RETURNING id
                """,
                (approval_id,),
            )

            return cur.fetchone() is not None
