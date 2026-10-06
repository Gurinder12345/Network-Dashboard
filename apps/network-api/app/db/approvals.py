from app.config_blocks import blocks_from_legacy, render_cli
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
    approved_at,
    cancelled_by,
    cancelled_at,
    cancellation_reason,
    config_blocks,
    verification_commands,
    precheck_summary,
    execution_result
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
        "cancelled_by": row[10],
        "cancelled_at": row[11].isoformat() if row[11] else None,
        "cancellation_reason": row[12],
        **_blocks_view(row[13], row[6], row[7]),
        "verification_commands": row[14] or [],
        "precheck_summary": row[15],
        "execution_result": row[16],
    }


def _blocks_view(config_blocks, config_lines, config_parents):
    """
    Multi-block view of any change. Rows created before migration 008 have no
    config_blocks: their single parent + lines are shown as one block (legacy=True).
    """
    legacy = not config_blocks
    blocks = blocks_from_legacy(config_lines, config_parents) if legacy else config_blocks
    return {
        "config_blocks": blocks,
        "legacy_single_block": legacy,
        "block_count": len(blocks),
        "command_count": sum(len(b.get("commands") or []) for b in blocks),
        "cli_preview": render_cli(blocks),
    }


def list_approvals():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {APPROVAL_COLUMNS}
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


def cancel_open_approval(approval_id, cancelled_by, reason):
    """
    Atomically move pending/approved -> cancelled. Returns None if the row is no longer in
    one of those states (another cancel won, or Apply already claimed it as applying).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE change_approvals
                SET
                    status = 'cancelled',
                    cancelled_by = %s,
                    cancelled_at = NOW(),
                    cancellation_reason = %s
                WHERE id = %s
                  AND status IN ('pending', 'approved')
                RETURNING {APPROVAL_COLUMNS}
                """,
                (cancelled_by, reason, approval_id),
            )

            row = cur.fetchone()

            return _row_to_approval(row) if row else None
