"""
Live validation of the manual backup path against ONE switch. Read-only on the device: it
runs the platform's existing running-config command via run_manual_backup() -- the same
code the Celery task runs -- and nothing else. It creates a normal manual_backup job,
backups row, audit events and /backups file, exactly as "Backup Now" would.

Never prints configuration contents: only size, line count, SHA-256 and whether the
stored hostname matches.

Run inside a network-worker pod (needs Vault, the /backups PVC and DB access):
    python -m tasks.validate_backup_live Kenda-HARO-IDF-A
"""

import argparse
import hashlib
import os
import re
import sys
import uuid

from db.client import get_connection
from tasks.config_backup import run_manual_backup


def jobs_constraints(cur):
    cur.execute(
        """
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'jobs'::regclass AND contype = 'c'
        """
    )
    return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hostname")
    args = parser.parse_args()

    with get_connection() as conn, conn.cursor() as cur:
        constraints = jobs_constraints(cur)
        print(f"jobs CHECK constraints: {constraints or 'none'}")
        if any("status" in definition and "queued" not in definition for _, definition in constraints):
            print("FAIL: jobs.status has a CHECK constraint without 'queued'; the API's queued job insert would fail.")
            return 2

        cur.execute(
            "SELECT id, platform FROM devices WHERE hostname = %s AND enabled = TRUE",
            (args.hostname,),
        )
        row = cur.fetchone()
        if row is None:
            print(f"FAIL: enabled device {args.hostname!r} not found")
            return 2
        device_id, platform = row

        # Same row the API creates for POST /api/v1/devices/{id}/backup.
        job_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO jobs (id, device_id, job_type, status, requested_by, started_at)
            VALUES (%s, %s, 'manual_backup', 'queued', 'live-validation', NOW())
            """,
            (job_id, device_id),
        )

    print(f"device_id={device_id} platform={platform} job_id={job_id} (queued)")

    result = run_manual_backup(device_id, job_id)
    print(f"task result: status={result['status']} backup_id={result.get('backup_id')} error={result.get('error')}")

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, error_message, started_at, finished_at FROM jobs WHERE id = %s", (job_id,))
        status, error, started, finished = cur.fetchone()
        print(f"job: status={status} error={error} duration={(finished - started) if finished else None}")

        cur.execute("SELECT id, storage_path, checksum FROM backups WHERE job_id = %s", (job_id,))
        backup = cur.fetchone()

        cur.execute("SELECT event_type FROM audit_events WHERE job_id = %s ORDER BY id", (job_id,))
        print(f"audit events: {[r[0] for r in cur.fetchall()]}")

    if status != "success" or backup is None:
        print("FAIL: backup did not complete")
        return 1

    backup_id, path, checksum = backup
    with open(path, encoding="utf-8") as f:
        text = f.read()

    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    hostname_line = re.search(r'^hostname\s+"?([^"\s]+)"?\s*$', text, re.MULTILINE)
    checks = {
        "file non-empty": len(text) > 0,
        "sha256 matches DB checksum": sha == checksum,
        "contains a hostname line": bool(hostname_line),
        "file mode 0600": oct(os.stat(path).st_mode & 0o777) == "0o600",
        "not JSON-wrapped": not text.lstrip().startswith("{"),
    }

    print(f"backup_id={backup_id} file={os.path.basename(path)} size={len(text.encode())} bytes lines={text.count(chr(10))} sha256={sha}")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    # Informational: a switch's configured hostname can differ from its inventory name.
    print(f"  INFO  configured hostname: {hostname_line.group(1) if hostname_line else None} (inventory: {args.hostname})")

    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
