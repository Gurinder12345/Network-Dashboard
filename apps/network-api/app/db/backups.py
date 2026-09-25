from app.db.client import get_connection


def list_backups():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    job_id,
                    device_id,
                    backup_type,
                    storage_path,
                    checksum,
                    created_at
                FROM backups
                ORDER BY created_at DESC
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
                }
                for row in rows
            ]


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
