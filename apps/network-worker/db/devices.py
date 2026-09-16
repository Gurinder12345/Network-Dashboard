from db.client import get_db_connection


def get_device_by_hostname(hostname):
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    hostname,
                    management_ip,
                    platform,
                    credential_path
                FROM devices
                WHERE hostname = %s
                  AND enabled = TRUE
                """,
                (hostname,),
            )

            row = cur.fetchone()

            if row is None:
                raise ValueError(
                    f"Enabled device not found in database: {hostname}"
                )

            return {
                "id": row[0],
                "hostname": row[1],
                "management_ip": str(row[2]),
                "platform": row[3],
                "credential_path": row[4],
            }

    finally:
        conn.close()
