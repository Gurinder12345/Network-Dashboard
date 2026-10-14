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
    python -m interfaces.validate_live bytes Kenda-Access-user-1 Kenda-HQ-IDF-B
        OS6 byte-counter diagnosis: the production port selection (cap) and its
        `show statistics` outputs, saved and explained per port. Writes nothing to PostgreSQL/Redis.
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

from coordination import device_ops as coord
from db.client import get_connection
from db.devices import get_device_by_hostname
from health.cache import _redis
from health.checks import _open_session, _safe_error, _tcp_reachable
from interfaces import cache, collector
from interfaces.parsers import (
    INTERFACE_COMMANDS,
    OS6_STATISTICS_COMMAND,
    OS6_STATUS,
    SAFE_COMMAND,
    diagnose_os6_statistics,
    parse_interfaces,
)
from interfaces.utilization import status_of
from vault.client import get_device_credentials

CAPTURE_DIR = "/tmp/interface_capture"
# Capture-only extras may name one interface (e.g. "show interfaces counters gigabitethernet 1/0/2");
# still `show` only, no pipes or other punctuation. Collection commands use parsers.SAFE_COMMAND.
CAPTURE_EXTRA = re.compile(r"^show [a-z0-9 \-/:]+$")


def _slug(command):
    return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")


def _query(sql, args=()):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _holder(device_id):
    """Who holds the device slot now (operation, owner host:pid:thread, started/expires), for diagnostics."""
    try:
        holder = coord.current_operation(device_id)
    except Exception as exc:
        return f"unavailable ({type(exc).__name__})"
    if not holder:
        return "none"
    return {k: holder.get(k) for k in ("operation", "owner", "started_at", "expires_at")}


def _device_slot(device):
    """Take the same coordination slot as a scheduled poll (priority 4, no wait); None = skip."""
    if coord.change_in_progress(device["id"]):
        print(f"SKIPPED: a configuration change is in progress on {device['hostname']}; no SSH opened")
        return None
    acq = coord.acquire_device_operation(device["id"], "interface_poll", hostname=device["hostname"])
    if not acq.acquired:
        print(f"SKIPPED ({acq.reason}): device slot held by {_holder(device['id'])}; no SSH opened. Retry later.")
        return None
    return acq.lease


def cmd_capture(device, extra, only_extra=False):
    commands = ([] if only_extra else list(INTERFACE_COMMANDS.get(device["platform"], ()))) + list(extra)
    for command in commands:
        if not (SAFE_COMMAND.match(command) or (command in extra and CAPTURE_EXTRA.match(command))):
            raise SystemExit(f"Refusing non-show command: {command!r}")
    print(f"\n######## {device['hostname']} ({device['platform']}) commands: {commands}")
    tcp_ok, tcp_error = _tcp_reachable(device["management_ip"])
    if not tcp_ok:
        print(f"UNREACHABLE: {tcp_error}")
        return False
    credentials = get_device_credentials(device["credential_path"])
    secrets = [s for s in (credentials.get("password"), credentials.get("secret")) if s]
    os.makedirs(CAPTURE_DIR, mode=0o700, exist_ok=True)
    lease = _device_slot(device)
    if lease is None:
        return False
    with lease:
        return _capture_session(device, commands, credentials, secrets)


def _capture_session(device, commands, credentials, secrets):
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
    lease = _device_slot(device)
    if lease is None:
        return False
    with lease:
        result = collector.collect_device(device)
    print(f"\n{device['hostname']}: status={result['status']} commands={result['commands_run']} "
          f"interfaces={len(result['interfaces'])} duration_ms={result['duration_ms']} error={result['error']}")
    for problem in result["problems"]:
        print(f"  problem: {problem}")
    _print_interfaces(result["interfaces"])
    b = result.get("os6_bytes")
    if b:
        print(f"os6_bytes: eligible={b['eligible']} queried={b['queried']} with_bytes={b['with_bytes']} "
              f"skipped_cap={b['skipped_cap']} skipped_budget={b['skipped_budget']} duration_ms={b['duration_ms']} "
              f"error={b['error']} failures={b['failures'] or 'none'} failed_ports={b['failed_ports'] or 'none'}")
    return result["status"] != "failed"


VERSION_LINE = re.compile(r"(system description|machine description|machine type|model|software version|"
                          r"version|serial|system type)", re.I)


def cmd_bytes(device):
    """
    OS6 byte-counter diagnosis (read-only, one session, under the device lock):
    `show version` (model/firmware lines only) + `show interfaces status`, the SAME port
    selection and cap as production, then each port's `show statistics ...`. Saves each
    output (credentials masked) and prints per port: command, duration, RX/TX octets found,
    reason code and the octet label lines.
    """
    if device["platform"] != "dell_os6":
        print(f"{device['hostname']}: not dell_os6, nothing to do")
        return True
    tcp_ok, tcp_error = _tcp_reachable(device["management_ip"])
    if not tcp_ok:
        print(f"{device['hostname']}: UNREACHABLE: {tcp_error}")
        return False
    credentials = get_device_credentials(device["credential_path"])
    secrets = [s for s in (credentials.get("password"), credentials.get("secret")) if s]
    lease = _device_slot(device)
    if lease is None:
        return False
    os.makedirs(CAPTURE_DIR, mode=0o700, exist_ok=True)

    def save(command, output):
        for secret in secrets:
            output = output.replace(secret, "***")
        path = os.path.join(CAPTURE_DIR, f"{device['hostname']}__{_slug(command)}.txt")
        with open(path, "w") as handle:
            handle.write(output)
        os.chmod(path, 0o600)
        return output, path

    started = time.monotonic()
    with lease:
        session = _open_session(device, credentials)
        try:
            print(f"\n######## {device['hostname']} ({device['management_ip']}) ssh sessions opened: 1 "
                  f"(login {time.monotonic() - started:.1f} s)")
            version, _ = save("show version", session.send_command("show version", read_timeout=30))
            for line in [l.strip() for l in version.splitlines() if VERSION_LINE.search(l)][:6]:
                print(f"  {line[:110]}")
            status, _ = save(OS6_STATUS, session.send_command(OS6_STATUS, read_timeout=collector.CLI_TIMEOUT_SECONDS))
            parsed = parse_interfaces("dell_os6", {OS6_STATUS: status})
            targets = collector.os6_byte_targets(parsed["interfaces"])
            selected = targets[:collector.OS6_UTIL_MAX_PORTS_PER_POLL]
            print(f"  eligible oper-up Gi/Te ports: {len(targets)}; selected (cap {collector.OS6_UTIL_MAX_PORTS_PER_POLL}): "
                  f"{[item['interface_name'] for item, _ in selected]}")
            ok = 0
            for item, command in selected:
                if not OS6_STATISTICS_COMMAND.match(command):
                    continue
                t0 = time.monotonic()
                try:
                    output = session.send_command(command, read_timeout=collector.OS6_UTIL_CLI_TIMEOUT_SECONDS)
                except Exception as exc:
                    print(f"  {command!r}: FAILED after {time.monotonic() - t0:.1f} s: {_safe_error(type(exc).__name__, secrets)}")
                    if not collector._resync(session):
                        print("  session not back at the prompt: stopping")
                        break
                    continue
                elapsed = time.monotonic() - t0
                output, path = save(command, output)
                d = diagnose_os6_statistics(output)
                ok += d["reason"] == "ok"
                print(f"  {command!r}: {elapsed:.2f} s, {len(output.splitlines())} lines -> reason={d['reason']} "
                      f"rx_octets={d['rx']} tx_octets={d['tx']}  [{path}]")
                for line in d["octet_lines"]:
                    print(f"      octet label: {line}")
        finally:
            session.disconnect()
    print(f"  {device['hostname']}: {ok}/{len(selected)} ports with both octet counters; "
          f"total {time.monotonic() - started:.1f} s (one session, closed)")
    return True


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
    if outcome["status"].startswith("skipped"):
        print(f"device slot held by: {_holder(device['id'])} -- nothing was collected; retry later")
        return False
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
    parser.add_argument("mode", choices=("capture", "dry-run", "record", "bytes"))
    parser.add_argument("hostnames", nargs="+")
    parser.add_argument("--extra", action="append", default=[], help='capture only: extra read-only "show ..." command')
    parser.add_argument("--only-extra", action="store_true", help="capture only the --extra commands (one session)")
    args = parser.parse_args()
    ok = True
    for hostname in args.hostnames:
        device = get_device_by_hostname(hostname)
        if args.mode == "capture":
            ok = cmd_capture(device, args.extra, only_extra=args.only_extra) and ok
        elif args.mode == "bytes":
            ok = cmd_bytes(device) and ok
        elif args.mode == "dry-run":
            ok = cmd_dry_run(device) and ok
        else:
            ok = cmd_record(device) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
