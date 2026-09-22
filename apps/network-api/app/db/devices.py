from app.db.client import get_connection


def list_devices():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    hostname,
                    management_ip,
                    platform,
                    enabled
                FROM devices
                ORDER BY hostname
                """
            )

            rows = cur.fetchall()

            return [
                {
                    "id": row[0],
                    "hostname": row[1],
                    "management_ip": str(row[2]),
                    "platform": row[3],
                    "enabled": row[4],
                }
                for row in rows
            ]
