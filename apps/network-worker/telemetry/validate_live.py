"""
Live validation of CPU/memory telemetry. Read-only on the switches (only the validated
`show` commands); never touches device health, jobs or audit tables.

Run inside a network-worker pod (Vault, devices, PostgreSQL, Redis):

    python -m telemetry.validate_live dry-run Kenda-HARO-IDF-A Kenda-Core-1
        collect + parse, print values and durations; writes NOTHING
    python -m telemetry.validate_live record Kenda-HARO-IDF-A Kenda-Core-1
        collect + store exactly like the scheduled task, then verify the
        device_metrics rows, Redis latest keys/TTL, and that health/jobs/audit are untouched
    python -m telemetry.validate_live fleet
        one manual fleet run (all enabled devices, bounded concurrency) + checks:
        peak concurrency, fleet lock held during the run and gone after, a second run
        during the first is skipped, health/jobs/audit untouched
    python -m telemetry.validate_live export /tmp/telemetry_export.json [--hours 6]
        dump real device_metrics rows + inventory + health for a local UI check
        (operational values only: no credentials, no configs)

record/fleet/export require migration 005 (device_metrics); the script checks first.
"""

import argparse
import json
import sys
import threading
import time

from db.client import get_connection
from db.devices import get_device_by_hostname
from health.cache import METRICS_DEVICE_KEY, _redis
from telemetry import collector
from telemetry.parsers import TELEMETRY_COMMANDS

LOCK_KEY = collector.METRICS_LOCK_KEY


def _query(sql, args=()):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def migration_present():
    present = _query("SELECT to_regclass('device_metrics') IS NOT NULL")[0][0]
    print(f"migration 005 (device_metrics) present: {present}")
    return present


def snapshot():
    """Health statuses + job/audit counts, to show telemetry left them alone."""
    return {
        "health": dict(_query("SELECT device_id, status FROM device_health")),
        "jobs": _query("SELECT count(*) FROM jobs")[0][0],
        "audit": _query("SELECT count(*) FROM audit_events")[0][0],
    }


def report_untouched(before, after):
    changed = {d: (before["health"].get(d), s) for d, s in after["health"].items() if before["health"].get(d) != s}
    print(f"health statuses changed during run: {changed or 'none'}  (the 60 s health Beat may legitimately change one; telemetry never writes health)")
    print(f"jobs rows added: {after['jobs'] - before['jobs']}  audit rows added: {after['audit'] - before['audit']}")
    return after["jobs"] == before["jobs"] and after["audit"] == before["audit"]


def line(sample):
    return (
        f"{sample['hostname']:20} status={sample['status']:8} cpu={sample['cpu_percent']} mem={sample['memory_percent']} "
        f"used_mb={sample['memory_used_mb']} total_mb={sample['memory_total_mb']} uptime_s={sample['uptime_seconds']} "
        f"duration_ms={sample['duration_ms']} error={sample['error']}"
    )


def devices_for(hostnames):
    return [{**get_device_by_hostname(h), "enabled": True} for h in hostnames]


def cmd_dry_run(hostnames):
    ok = True
    for device in devices_for(hostnames):
        print(f"{device['hostname']} ({device['platform']}) commands={list(TELEMETRY_COMMANDS.get(device['platform'], ()))}")
        sample = collector.collect_device(device)
        print("  " + line(sample))
        ok = ok and sample["status"] == "success"
    return 0 if ok else 1


def cmd_record(hostnames):
    if not migration_present():
        return 2
    before = snapshot()
    ok = True
    for device in devices_for(hostnames):
        sample = collector.collect_and_record(device)
        print(line(sample))
        row = _query(
            "SELECT collection_status, cpu_percent, memory_percent, memory_used_mb, memory_total_mb, uptime_seconds, source "
            "FROM device_metrics WHERE device_id = %s ORDER BY collected_at DESC LIMIT 1", (device["id"],))[0]
        key = METRICS_DEVICE_KEY.format(device_id=device["id"])
        cached = _redis().get(key)
        print(f"  db row: {row}")
        print(f"  redis {key}: ttl={_redis().ttl(key)} bytes={len(cached or '')} status={json.loads(cached)['status'] if cached else None}")
        ok = ok and sample["status"] == "success" and row[0] == "success" and cached is not None
    ok = report_untouched(before, snapshot()) and ok
    print("RESULT:", "PASS" if ok else "CHECK OUTPUT")
    return 0 if ok else 1


def cmd_fleet():
    if not migration_present():
        return 2
    before = snapshot()

    # Measure real concurrency without changing behaviour: wrap the per-device function.
    active, peak, guard = [0], [0], threading.Lock()
    original = collector.collect_device

    def counted(device):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            return original(device)
        finally:
            with guard:
                active[0] -= 1

    collector.collect_device = counted
    result = {}
    worker = threading.Thread(target=lambda: result.update(collector.run_fleet_metrics(trigger="manual")))
    worker.start()
    time.sleep(1.5)
    lock_during = bool(_redis().exists(LOCK_KEY))
    second = collector.run_fleet_metrics(trigger="manual")  # must be skipped while the first holds the lock
    worker.join()
    collector.collect_device = original
    lock_after = bool(_redis().exists(LOCK_KEY))

    print(json.dumps({k: v for k, v in result.items()}, indent=1, default=str))
    print(f"peak concurrent devices: {peak[0]} (limit {collector.METRICS_CONCURRENCY})")
    print(f"fleet lock held during run: {lock_during}; second run during first skipped: {second.get('skipped')}; lock present after: {lock_after}")

    rows = _query(
        """
        SELECT d.hostname, m.collection_status, m.cpu_percent, m.memory_percent, m.error
        FROM devices d
        JOIN LATERAL (SELECT * FROM device_metrics WHERE device_id = d.id ORDER BY collected_at DESC LIMIT 1) m ON TRUE
        WHERE d.enabled ORDER BY d.hostname
        """)
    for hostname, status, cpu, mem, error in rows:
        print(f"  {hostname:22} {status:8} cpu={cpu} mem={mem} {('error=' + error) if error else ''}")
    untouched = report_untouched(before, snapshot())

    ok = (untouched and peak[0] <= collector.METRICS_CONCURRENCY and lock_during and second.get("skipped")
          and not lock_after and result.get("not_recorded") == 0 and result.get("duration_ms", 10**9) < 60000)
    print("RESULT:", "PASS" if ok else "CHECK OUTPUT")
    return 0 if ok else 1


def cmd_export(path, hours):
    if not migration_present():
        return 2
    data = {
        "devices": [dict(zip(("id", "hostname", "management_ip", "platform", "enabled"), (r[0], r[1], str(r[2]), r[3], r[4])))
                    for r in _query("SELECT id, hostname, management_ip, platform, enabled FROM devices")],
        "device_health": [dict(zip(("device_id", "status", "last_check_at", "last_success_at", "response_time_ms",
                                    "tcp_reachable", "ssh_reachable", "cli_reachable", "last_error", "consecutive_failures"), r))
                          for r in _query("SELECT device_id, status, last_check_at, last_success_at, response_time_ms, tcp_reachable, "
                                          "ssh_reachable, cli_reachable, last_error, consecutive_failures FROM device_health")],
        "device_metrics": [dict(zip(("device_id", "collected_at", "cpu_percent", "memory_percent", "memory_used_mb", "memory_total_mb",
                                     "uptime_seconds", "source", "collection_status", "error"), r))
                           for r in _query("SELECT device_id, collected_at, cpu_percent, memory_percent, memory_used_mb, memory_total_mb, "
                                           "uptime_seconds, source, collection_status, error FROM device_metrics "
                                           "WHERE collected_at >= now() - make_interval(hours => %s) ORDER BY collected_at", (hours,))],
    }
    with open(path, "w") as handle:
        json.dump(data, handle, default=str)
    print(f"exported {len(data['device_metrics'])} samples, {len(data['devices'])} devices -> {path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("dry-run", "record"):
        sub.add_parser(mode).add_argument("hostnames", nargs="+")
    sub.add_parser("fleet")
    export = sub.add_parser("export")
    export.add_argument("path")
    export.add_argument("--hours", type=int, default=6)
    args = parser.parse_args()

    if args.mode == "dry-run":
        return cmd_dry_run(args.hostnames)
    if args.mode == "record":
        return cmd_record(args.hostnames)
    if args.mode == "fleet":
        return cmd_fleet()
    return cmd_export(args.path, args.hours)


if __name__ == "__main__":
    sys.exit(main())
