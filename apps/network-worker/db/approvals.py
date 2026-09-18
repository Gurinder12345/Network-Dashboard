from db.client import get_connection
import json
import uuid



def verify_backup_for_device(device_id, backup_job_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    j.id,
                    j.device_id,
                    j.status,
                    b.id,
                    b.storage_path,
                    b.checksum
                FROM jobs j
                JOIN backups b
                  ON b.job_id = j.id
                WHERE j.id = %s
                  AND j.device_id = %s
                LIMIT 1
                """,
                (
                    backup_job_id,
                    device_id,
                ),
            )

            row = cur.fetchone()

            if row is None:
                return {
                    "valid": False,
                    "reason": "Backup job not found for this device",
                }

            job_id = row[0]
            job_device_id = row[1]
            job_status = row[2]
            backup_id = row[3]
            storage_path = row[4]
            checksum = row[5]

            if job_status != "success":
                return {
                    "valid": False,
                    "reason": f"Backup job status is {job_status}",
                }

            return {
                "valid": True,
                "job_id": str(job_id),
                "device_id": job_device_id,
                "backup_id": backup_id,
                "storage_path": storage_path,
                "checksum": checksum,
            }

    finally:
        conn.close()




def create_change_approval(
    device_id,
    backup_job_id,
    config_lines,
    requested_by="system",
):
    approval_id = uuid.uuid4()

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO change_approvals (
                    id,
                    device_id,
                    backup_job_id,
                    requested_by,
                    status,
                    config_lines
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    approval_id,
                    device_id,
                    backup_job_id,
                    requested_by,
                    "pending",
                    json.dumps(config_lines),
                ),
            )

        conn.commit()

        return {
            "approval_id": str(approval_id),
            "device_id": device_id,
            "backup_job_id": str(backup_job_id),
            "status": "pending",
        }

    finally:
        conn.close()




def approve_change(
    approval_id,
    approved_by,
):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE change_approvals
                SET
                    status = 'approved',
                    approved_by = %s,
                    approved_at = NOW()
                WHERE id = %s
                  AND status = 'pending'
                RETURNING
                    id,
                    device_id,
                    backup_job_id,
                    requested_by,
                    approved_by,
                    status,
                    config_lines,
                    created_at,
                    approved_at
                """,
                (
                    approved_by,
                    approval_id,
                ),
            )

            row = cur.fetchone()

            if row is None:
                raise ValueError(
                    "Approval not found or is not pending"
                )

        conn.commit()

        return {
            "approval_id": str(row[0]),
            "device_id": row[1],
            "backup_job_id": str(row[2]),
            "requested_by": row[3],
            "approved_by": row[4],
            "status": row[5],
            "config_lines": row[6],
            "created_at": row[7].isoformat(),
            "approved_at": row[8].isoformat(),
        }

    finally:
        conn.close()
def get_change_approval(approval_id):
    conn = get_connection()

    try:
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
                    created_at,
                    approved_at
                FROM change_approvals
                WHERE id = %s
                """,
                (approval_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise ValueError(
                    f"Approval not found: {approval_id}"
                )

            return {
                "approval_id": str(row[0]),
                "device_id": row[1],
                "backup_job_id": str(row[2]),
                "requested_by": row[3],
                "approved_by": row[4],
                "status": row[5],
                "config_lines": row[6],
                "created_at": (
                    row[7].isoformat()
                    if row[7]
                    else None
                ),
                "approved_at": (
                    row[8].isoformat()
                    if row[8]
                    else None
                ),
            }

    finally:
        conn.close()


def mark_approval_applying(approval_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE change_approvals
                SET status = 'applying'
                WHERE id = %s
                  AND status = 'approved'
                RETURNING id
                """,
                (approval_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise ValueError(
                    "Approval is not in approved state"
                )

        conn.commit()

    finally:
        conn.close()


def mark_approval_applied(approval_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE change_approvals
                SET status = 'applied'
                WHERE id = %s
                  AND status = 'applying'
                RETURNING id
                """,
                (approval_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise ValueError(
                    "Approval is not in applying state"
                )

        conn.commit()

    finally:
        conn.close()



def mark_approval_failed(approval_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE change_approvals
                SET status = 'failed'
                WHERE id = %s
                  AND status = 'applying'
                """,
                (approval_id,),
            )

        conn.commit()

    finally:
        conn.close()
