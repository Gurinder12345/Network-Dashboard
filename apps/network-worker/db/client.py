import os
import psycopg


DB_HOST = os.getenv(
    "DB_HOST",
    "postgresql.network-platform.svc.cluster.local",
)

DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "networkdb")
DB_USER = os.getenv("DB_USER", "networkapp")
DB_PASSWORD = os.getenv("DB_PASSWORD")


def get_connection():
    if not DB_PASSWORD:
        raise RuntimeError("DB_PASSWORD is not configured")

    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
