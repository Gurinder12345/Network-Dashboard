from app.db.client import get_connection


def list_backups():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    b.id,
                    b.job_id,
                    b.device_id,
                    b.backup_type,
                    b.storage_path,
                    b.checksum,
                    b.created_at,
                    j.job_type,
                    d.hostname
                FROM backups b
                LEFT JOIN jobs j ON j.id = b.job_id
                LEFT JOIN devices d ON d.id = b.device_id
                ORDER BY b.created_at DESC
                """
            )

            rows = cur.fetchall()

            return [
                {
                    "id": row[0],
                    "job_id": str(row[1]) if row[1] else None,
                    "device_id": row[2],
                    "backup_type": row[3],
                    "storage_path": row[4],
                    "checksum": row[5],
                    "created_at": row[6].isoformat() if row[6] else None,
                    "job_type": row[7],
                    "hostname": row[8],
                }
                for row in rows
            ]


def latest_backup_for_device(device_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id, b.storage_path, b.created_at, j.job_type
                FROM backups b
                LEFT JOIN jobs j ON j.id = b.job_id
                WHERE b.device_id = %s
                ORDER BY b.created_at DESC
                LIMIT 1
                """,
                (device_id,),
            )
            row = cur.fetchone()

    if row is None:
        return None

    return {
        "id": row[0],
        "storage_path": row[1],
        "created_at": row[2].isoformat() if row[2] else None,
        "job_type": row[3],
    }


def get_backup_by_id(backup_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    b.id,
                    b.device_id,
                    b.storage_path,
                    d.hostname
                FROM backups b
                LEFT JOIN devices d ON d.id = b.device_id
                WHERE b.id = %s
                """,
                (backup_id,),
            )

            row = cur.fetchone()

            if row is None:
                return None

            return {
                "id": row[0],
                "device_id": row[1],
                "storage_path": row[2],
                "hostname": row[3],
            }
