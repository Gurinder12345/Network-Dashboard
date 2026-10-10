"""
Live validation of Interface Monitoring V1. READ-ONLY on the switches: only the `show`
commands in interfaces/parsers.py (and optional --extra `show ...` commands for capture);
never configuration mode, never `clear counters`. Never touches device health, jobs or
audit tables.

Run inside a network-worker pod (Vault, devices, PostgreSQL, Redis), ONE device first:

    python -m interfaces.validate_live capture Kenda-HARO-SW-01
        one SSH session, the collection commands, raw output saved to
        /tmp/interface_capture/<host>__<command>.txt (0600) for parser fixtures; prints
        per-command duration and line count. Writes nothing to PostgreSQL or Redis.
    python -m interfaces.validate_live dry-run Kenda-HARO-SW-01
        collect + parse exactly like the scheduled task; prints every normalized interface
        and the summary. Writes NOTHING.
    python -m interfaces.validate_live record Kenda-HARO-SW-01
        one coordinated collection stored like the scheduled task (PostgreSQL + Redis),
        then checks rows, the Redis snapshot/TTL, and that health/jobs/audit are unchanged.
        Requires migration 009.
"""

import argparse
import json
import os
import re
import sys
import time

from db.client import get_connection
from db.devices import get_device_by_hostname
from health.cache import _redis
from health.checks import _open_session, _safe_error, _tcp_reachable
from interfaces import cache, collector
from interfaces.parsers import INTERFACE_COMMANDS, SAFE_COMMAND
from interfaces.utilization import status_of
from vault.client import get_device_credentials

CAPTURE_DIR = "/tmp/interface_capture"


def _slug(command):
    return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")


def _query(sql, args=()):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def cmd_capture(device, extra):
    commands = list(INTERFACE_COMMANDS.get(device["platform"], ())) + list(extra)
    for command in commands:
        if not SAFE_COMMAND.match(command):
            raise SystemExit(f"Refusing non-show command: {command!r}")
    print(f"\n######## {device['hostname']} ({device['platform']}) commands: {commands}")
    tcp_ok, tcp_error = _tcp_reachable(device["management_ip"])
    if not tcp_ok:
        print(f"UNREACHABLE: {tcp_error}")
        return False
    credentials = get_device_credentials(device["credential_path"])
    secrets = [s for s in (credentials.get("password"), credentials.get("secret")) if s]
    os.makedirs(CAPTURE_DIR, mode=0o700, exist_ok=True)
    started = time.monotonic()
    session = _open_session(device, credentials)
    print(f"ssh sessions opened: 1 (login {time.monotonic() - started:.1f} s)")
    try:
        for command in commands:
            t0 = time.monotonic()
            try:
                output = session.send_command(command, read_timeout=collector.CLI_TIMEOUT_SECONDS)
            except Exception as exc:
                print(f"==== {command}: FAILED after {time.monotonic() - t0:.1f} s: {_safe_error(type(exc).__name__, secrets)}")
                continue
            for secret in secrets:
                output = output.replace(secret, "***")
            path = os.path.join(CAPTURE_DIR, f"{device['hostname']}__{_slug(command)}.txt")
            with open(path, "w") as handle:
                handle.write(output)
            os.chmod(path, 0o600)
            print(f"==== {command}: {time.monotonic() - t0:.1f} s, {len(output.splitlines())} lines, "
                  f"{len(output)} bytes -> {path}")
    finally:
        session.disconnect()
    print(f"total {time.monotonic() - started:.1f} s (one session, closed)")
    return True


def _print_interfaces(interfaces):
    print(f"{'interface':22} {'admin':5} {'oper':5} {'status':10} {'speed':>8} {'mode':8} {'vlan':>5} rx_bytes / tx_bytes / crc")
    for i in interfaces:
        speed = f"{i['speed_bps'] // 1_000_000}M" if i.get("speed_bps") else "-"
        print(f"{i['interface_name']:22} {str(i['admin_status']):5} {str(i['oper_status']):5} "
              f"{status_of(i['admin_status'], i['oper_status']):10} {speed:>8} {str(i['mode']):8} "
              f"{str(i.get('access_vlan') or i.get('native_vlan') or '-'):>5} "
              f"{i['rx_bytes']} / {i['tx_bytes']} / {i['crc_errors']}")


def cmd_dry_run(device):
    result = collector.collect_device(device)
    print(f"\n{device['hostname']}: status={result['status']} commands={result['commands_run']} "
          f"interfaces={len(result['interfaces'])} duration_ms={result['duration_ms']} error={result['error']}")
    for problem in result["problems"]:
        print(f"  problem: {problem}")
    _print_interfaces(result["interfaces"])
    return result["status"] != "failed"


def _snapshot_state():
    return {"health": dict(_query("SELECT device_id, status FROM device_health")),
            "jobs": _query("SELECT count(*) FROM jobs")[0][0],
            "audit": _query("SELECT count(*) FROM audit_events")[0][0]}


def cmd_record(device):
    if not _query("SELECT to_regclass('interface_metrics') IS NOT NULL")[0][0]:
        print("migration 009 (interface_inventory / interface_metrics) is NOT applied")
        return False
    before = _snapshot_state()
    rows_before = _query("SELECT count(*) FROM interface_metrics WHERE device_id = %s", (device["id"],))[0][0]
    outcome = collector.collect_and_record(device, trigger="validate")
    print(f"\noutcome: {json.dumps({k: v for k, v in outcome.items() if k != 'hostname'}, default=str)}")
    rows_after = _query("SELECT count(*) FROM interface_metrics WHERE device_id = %s", (device["id"],))[0][0]
    inventory = _query("SELECT count(*), max(last_seen) FROM interface_inventory WHERE device_id = %s", (device["id"],))[0]
    print(f"interface_metrics rows added: {rows_after - rows_before}  inventory rows: {inventory[0]} last_seen: {inventory[1]}")

    key = cache.SNAPSHOT_KEY.format(device_id=device["id"])
    raw = _redis().get(key)
    if raw:
        snapshot = json.loads(raw)
        print(f"redis {key}: {len(raw)} bytes, ttl={_redis().ttl(key)} s, collected_at={snapshot['collected_at']}, "
              f"summary={snapshot['summary']}")
    else:
        print(f"redis {key}: absent")
    attempt = _redis().get(cache.ATTEMPT_KEY.format(device_id=device["id"]))
    print(f"last attempt: {attempt}")

    after = _snapshot_state()
    print(f"health statuses changed: {[d for d in after['health'] if after['health'][d] != before['health'].get(d)] or 'none'} "
          "(the 60 s health Beat may legitimately change one; interface collection never writes health)")
    print(f"jobs added: {after['jobs'] - before['jobs']}  audit added: {after['audit'] - before['audit']}")
    return outcome["status"] in ("success", "partial") and after["jobs"] == before["jobs"] and after["audit"] == before["audit"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("capture", "dry-run", "record"))
    parser.add_argument("hostnames", nargs="+")
    parser.add_argument("--extra", action="append", default=[], help='capture only: extra read-only "show ..." command')
    args = parser.parse_args()
    ok = True
    for hostname in args.hostnames:
        device = get_device_by_hostname(hostname)
        if args.mode == "capture":
            ok = cmd_capture(device, args.extra) and ok
        elif args.mode == "dry-run":
            ok = cmd_dry_run(device) and ok
        else:
            ok = cmd_record(device) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
