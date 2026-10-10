"""
Interface Monitoring V1 collector. READ-ONLY: only the `show` commands in
interfaces/parsers.py INTERFACE_COMMANDS are ever sent (re-checked before sending); it
never enters configuration mode and never clears counters.

Per device (collect_and_record):

    change in progress?  -> skip (collector_skipped_change), nothing written but the attempt
    take the device slot ("interface_poll", priority 4, no wait)
        busy / higher priority waiting -> skip (collector_skipped_busy); no retry, no backlog
        Redis coordination unavailable -> skip (never unbounded concurrent SSH)
    TCP/22 -> Vault credentials -> ONE SSH session (health-check session code) -> the
        platform's commands in order, inside a time budget below the slot TTL -> close
    parse -> normalize -> PostgreSQL (one transaction, bulk) -> Redis snapshot -> release

Never changes device health, never creates jobs or audit events. A failure at any stage
leaves the previous Redis snapshot and all stored history untouched.

Fleet (run_fleet_interfaces): fleet lock (no overlapping sweeps), bounded concurrency
across devices, slowest platform (OS10) first so the sweep finishes before the next
minute's health check.
"""

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from netmiko.exceptions import NetmikoAuthenticationException

from coordination import device_ops as coord
from db.health import list_enabled_devices
from db.interfaces import lldp_managed_ports, persist_collection
from health.cache import FleetLock
from health.checks import _open_session, _safe_error, _tcp_reachable
from interfaces import cache
from interfaces.names import canonical_name
from interfaces.parsers import INTERFACE_COMMANDS, SAFE_COMMAND, InterfaceParseError, parse_interfaces
from interfaces.utilization import compute, status_of
from vault.client import get_device_credentials

logger = logging.getLogger("network_worker.interfaces")


def _env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


POLL_SECONDS = _env_int("INTERFACE_POLL_SECONDS", 300, minimum=60)
# A delta across more than this is not shown as utilization (one missed poll is tolerated).
MAX_DELTA_SECONDS = _env_int("INTERFACE_MAX_DELTA_SECONDS", 3 * POLL_SECONDS)
CACHE_TTL_SECONDS = _env_int("INTERFACE_CACHE_TTL_SECONDS", 3 * POLL_SECONDS)
CONCURRENCY = _env_int("INTERFACE_CONCURRENCY", 4)
CLI_TIMEOUT_SECONDS = _env_int("INTERFACE_CLI_TIMEOUT_SECONDS", 60)
# Whole-device budget (login + all commands). Kept below the interface_poll slot TTL (150 s)
# so the slot can never expire while the session is still open.
DEVICE_BUDGET_SECONDS = min(_env_int("INTERFACE_DEVICE_BUDGET_SECONDS", 120),
                            coord.OPERATIONS["interface_poll"]["ttl"] - 20)
FLEET_LOCK_KEY = "interfaces:fleet:lock"
FLEET_LOCK_TTL_SECONDS = _env_int("INTERFACE_FLEET_LOCK_TTL_SECONDS", max(POLL_SECONDS - 10, 60))

PLATFORM_ORDER = {"dell_os10": 0, "dell_os6": 1}  # slowest login first
UPLINK_WORDS = re.compile(r"\b(uplink|upstream|up-link|core|spine|distribution|isl)\b", re.I)
LIMITS = {"interface_name": 64, "description": 255, "allowed_vlans": 1024, "port_channel": 64}


def _now():
    return datetime.now(timezone.utc)


# ---- SSH collection (no writes) ----------------------------------------------------------------
def collect_device(device):
    """One SSH session, the platform's show commands. Never raises, never writes."""
    started = time.monotonic()
    commands = INTERFACE_COMMANDS.get(device["platform"], ())
    result = {"status": "failed", "error": None, "interfaces": [], "problems": [], "commands_run": 0,
              "collected_at": _now(), "duration_ms": 0}

    def finish(error=None):
        result["error"] = error
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    if not commands:
        return finish(f"Interface commands not implemented for {device['platform']}")
    tcp_ok, _ = _tcp_reachable(device["management_ip"])
    if not tcp_ok:
        return finish("Device unreachable (TCP 22)")
    try:
        credentials = get_device_credentials(device["credential_path"])
    except Exception as exc:
        return finish(f"Credential lookup failed: {type(exc).__name__}")

    secrets = (credentials.get("password"), credentials.get("secret"))
    session = None
    outputs = {}
    try:
        session = _open_session(device, credentials)
        for command in commands:
            if not SAFE_COMMAND.match(command):  # defense in depth: show commands only
                raise RuntimeError("refused a non-show command")
            remaining = DEVICE_BUDGET_SECONDS - (time.monotonic() - started)
            if remaining < 5:
                result["problems"].append(f"'{command}': skipped (device time budget used)")
                break
            try:
                outputs[command] = session.send_command(command, read_timeout=min(CLI_TIMEOUT_SECONDS, remaining))
                result["commands_run"] += 1
            except Exception as exc:
                # A timed-out read can leave the session mid-output: stop, keep what we have.
                result["problems"].append(_safe_error(f"'{command}': {type(exc).__name__}", secrets))
                break
    except NetmikoAuthenticationException:
        return finish("SSH authentication failed")
    except Exception as exc:
        stage = "CLI command" if session is not None else "SSH session"
        return finish(_safe_error(f"{stage} failed: {type(exc).__name__}", secrets))
    finally:
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                pass

    try:
        parsed = parse_interfaces(device["platform"], outputs)
    except InterfaceParseError as exc:
        return finish(_safe_error(f"Interface output could not be parsed: {exc}", secrets))
    except Exception as exc:  # a parser bug must never escape the collector
        return finish(_safe_error(f"Interface parser error: {type(exc).__name__}", secrets))

    result["interfaces"] = parsed["interfaces"]
    result["problems"] += [_safe_error(p, secrets) for p in parsed["problems"]]
    result["status"] = "success" if not result["problems"] else "partial"
    return finish()


# ---- normalization for storage -----------------------------------------------------------------
def prepare(interfaces, platform, collected_at, managed_ports):
    """Storage-ready items: device-reported change time, evidence-based role, length limits."""
    lldp = {canonical_name(platform, name) for name in managed_ports}
    items = []
    for raw in interfaces:
        item = {k: v for k, v in raw.items() if k != "state_change_age_seconds"}
        age = raw.get("state_change_age_seconds")
        item["last_state_change"] = collected_at - timedelta(seconds=age) if age is not None else None
        for field, limit in LIMITS.items():
            if isinstance(item.get(field), str):
                item[field] = item[field][:limit]
        if item["canonical_name"] in lldp:
            item["role"] = "inter_switch"  # LLDP neighbor is another managed switch
        elif item["interface_type"] == "port_channel":
            item["role"] = "lag"
        elif item.get("description") and UPLINK_WORDS.search(item["description"]):
            item["role"] = "uplink"        # operator's own description says so
        else:
            item["role"] = None            # no evidence: neutral
        items.append(item)
    return items


def derive(previous, item, collected_at):
    return compute(previous, item, collected_at, MAX_DELTA_SECONDS)


SUMMARY_KEYS = ("total", "up", "down", "admin_down", "unknown", "erroring", "trunks", "access_ports")


def summarize(interfaces):
    summary = dict.fromkeys(SUMMARY_KEYS, 0)
    for item in interfaces:
        summary["total"] += 1
        summary[item["status"]] += 1
        summary["erroring"] += 1 if item["erroring"] else 0
        summary["trunks"] += 1 if item["mode"] == "trunk" else 0
        summary["access_ports"] += 1 if item["mode"] == "access" else 0
    return summary


def snapshot_interface(row):
    return {
        "id": row["id"], "name": row["interface_name"], "canonical_name": row["canonical_name"],
        "description": row.get("description"), "type": row["interface_type"], "role": row.get("role"),
        "admin_status": row.get("admin_status"), "oper_status": row.get("oper_status"),
        "status": status_of(row.get("admin_status"), row.get("oper_status")),
        "speed_bps": row.get("speed_bps"), "duplex": row.get("duplex"), "mode": row.get("mode"),
        "access_vlan": row.get("access_vlan"), "native_vlan": row.get("native_vlan"),
        "allowed_vlans": row.get("allowed_vlans"), "port_channel": row.get("port_channel"), "mtu": row.get("mtu"),
        "last_state_change": row["last_state_change"].isoformat() if row.get("last_state_change") else None,
        **{field: row.get(field) for field in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors",
                                                "tx_errors", "crc_errors", "input_discards", "output_discards",
                                                "rx_utilization_pct", "tx_utilization_pct", "errors_delta",
                                                "crc_delta", "discards_delta")},
        "erroring": bool(row.get("erroring")),
    }


def build_snapshot(device, result, rows):
    interfaces = sorted((snapshot_interface(r) for r in rows), key=lambda i: i["canonical_name"])
    return {
        "schema_version": cache.SCHEMA_VERSION,
        "device_id": device["id"],
        "hostname": device["hostname"],
        "platform": device["platform"],
        "collected_at": result["collected_at"].isoformat(),
        "collection_status": result["status"],
        "problems": result["problems"][:10],
        "poll_interval_seconds": POLL_SECONDS,
        "summary": summarize(interfaces),
        "interfaces": interfaces,
    }


# ---- one device, coordinated ---------------------------------------------------------------------
def _skip(device, status, reason):
    coord.count("collector_skipped")
    logger.info("%s collector=interfaces device_id=%s hostname=%s reason=%s",
                "collector_skipped_change" if status == "skipped_change" else "collector_skipped_busy",
                device["id"], device["hostname"], reason)
    cache.write_attempt(device["id"], {"at": _now().isoformat(), "status": status, "reason": reason})
    return {"device_id": device["id"], "hostname": device["hostname"], "status": status, "reason": reason,
            "interfaces_seen": 0, "samples_written": 0, "duration_ms": 0, "cache_written": False}


def collect_and_record(device, trigger="schedule"):
    """Collect one device under coordination; persist + cache on success. Never raises."""
    if coord.change_in_progress(device["id"]):
        return _skip(device, "skipped_change", "change_in_progress")
    acq = coord.acquire_device_operation(device["id"], "interface_poll", hostname=device["hostname"])
    if not acq.acquired:
        status = ("skipped_change" if acq.change_active else
                  "skipped_unavailable" if acq.status == "unavailable" else "skipped_busy")
        return _skip(device, status, acq.reason)

    with acq.lease:
        logger.info("interface_collection_started device_id=%s hostname=%s platform=%s trigger=%s",
                    device["id"], device["hostname"], device["platform"], trigger)
        result = collect_device(device)
        outcome = {"device_id": device["id"], "hostname": device["hostname"], "status": result["status"],
                   "interfaces_seen": len(result["interfaces"]), "samples_written": 0, "cache_written": False,
                   "commands_run": result["commands_run"], "duration_ms": result["duration_ms"],
                   "error": result["error"]}

        if result["status"] == "failed":
            return _failed(device, outcome, result["error"], stage="collect")

        collected_at = result["collected_at"]
        try:
            items = prepare(result["interfaces"], device["platform"], collected_at, lldp_managed_ports(device["id"]))
            rows, _ = persist_collection(device["id"], collected_at, items, MAX_DELTA_SECONDS,
                                         lambda previous, item: derive(previous, item, collected_at))
        except Exception as exc:
            return _failed(device, outcome, _safe_error(f"Database write failed: {type(exc).__name__}"), stage="database")

        outcome["samples_written"] = len(rows)
        outcome["cache_written"] = cache.write_snapshot(build_snapshot(device, result, rows), CACHE_TTL_SECONDS)
        cache.write_attempt(device["id"], {"at": collected_at.isoformat(), "status": result["status"],
                                           "problems": result["problems"][:10]})

    logger.info(
        "interface_collection_completed device_id=%s hostname=%s platform=%s status=%s interfaces_seen=%s "
        "samples_written=%s commands=%s duration_ms=%s cache_written=%s problems=%s",
        device["id"], device["hostname"], device["platform"], result["status"], outcome["interfaces_seen"],
        outcome["samples_written"], result["commands_run"], result["duration_ms"], outcome["cache_written"],
        len(result["problems"]),
    )
    return outcome


def _failed(device, outcome, error, stage):
    # The previous snapshot stays in Redis (freshness exposes the age); health is untouched.
    outcome.update(status="failed", error=error)
    logger.warning("interface_collection_failed device_id=%s hostname=%s platform=%s stage=%s duration_ms=%s error=%s",
                   device["id"], device["hostname"], device["platform"], stage, outcome["duration_ms"], error)
    cache.write_attempt(device["id"], {"at": _now().isoformat(), "status": "failed", "stage": stage, "error": error})
    return outcome


# ---- fleet sweep ---------------------------------------------------------------------------------
def run_fleet_interfaces(trigger="schedule"):
    started = time.monotonic()
    with FleetLock(key=FLEET_LOCK_KEY, ttl_seconds=FLEET_LOCK_TTL_SECONDS) as lock:
        if lock.acquired is False:
            logger.info("interfaces_collection_skipped reason=lock_held trigger=%s", trigger)
            return {"skipped": True, "reason": "An interface collection sweep is already running"}

        devices = sorted(list_enabled_devices(), key=lambda d: (PLATFORM_ORDER.get(d["platform"], 9), d["hostname"]))
        counts = dict.fromkeys(("success", "partial", "failed", "skipped_busy", "skipped_change", "skipped_unavailable"), 0)
        totals = {"interfaces_seen": 0, "samples_written": 0}
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(collect_and_record, device, trigger): device for device in devices}
            for future in as_completed(futures):
                device = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # collect_and_record never raises; belt and braces
                    outcome = {"status": "failed", "interfaces_seen": 0, "samples_written": 0}
                    logger.warning("interface_collection_failed device_id=%s hostname=%s stage=unexpected error=%s",
                                   device["id"], device["hostname"], _safe_error(type(exc).__name__))
                counts[outcome["status"]] += 1
                totals["interfaces_seen"] += outcome["interfaces_seen"]
                totals["samples_written"] += outcome["samples_written"]

    summary = {"skipped": False, "trigger": trigger, "devices_total": len(devices), **counts, **totals,
               "duration_ms": int((time.monotonic() - started) * 1000)}
    logger.info(
        "interfaces_collection_completed trigger=%s devices_total=%s success=%s partial=%s failed=%s skipped_busy=%s "
        "skipped_change=%s skipped_unavailable=%s interfaces_seen=%s samples_written=%s duration_ms=%s lock=%s",
        trigger, summary["devices_total"], counts["success"], counts["partial"], counts["failed"], counts["skipped_busy"],
        counts["skipped_change"], counts["skipped_unavailable"], totals["interfaces_seen"], totals["samples_written"],
        summary["duration_ms"], {True: "held", None: "redis_unavailable"}[lock.acquired],
    )
    return summary
