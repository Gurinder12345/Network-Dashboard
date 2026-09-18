from db.client import get_connection


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
