"""
Read-only CPU / memory telemetry collection, separate from health checks.

Per device: TCP/22 -> Vault credentials -> one SSH session (health-check session code and
short timeouts) -> the platform's validated `show` commands -> parse -> one
device_metrics row (PostgreSQL) -> latest sample in Redis.

Never changes configuration, never changes device health, never creates jobs or audit
events (routine polling would flood both). One unreachable device never blocks the fleet:
collection runs in a bounded thread pool and every per-device failure is recorded as a
`failed` sample with NULL metrics.

Device coordination (coordination/device_ops.py), telemetry = priority 3: before opening
SSH it takes the device's operation slot without waiting. If the device is busy (another
operation holds it, or a higher-priority operation is waiting) or a configuration change
is in progress, the device is SKIPPED for this run: no sample row, no failure, no health
impact; the next 60 s run catches up. No backlog is ever queued. If Redis coordination is
unavailable, collection is skipped (never unlimited concurrent SSH).
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from netmiko.exceptions import NetmikoAuthenticationException

from coordination import device_ops as coord
from db.health import list_enabled_devices
from db.metrics import insert_metric, last_success_at
from health.cache import FleetLock, cache_device_metrics
from health.checks import _open_session, _safe_error, _tcp_reachable
from telemetry.parsers import FIELDS, TELEMETRY_COMMANDS, TelemetryParseError, parse_metrics
from vault.client import get_device_credentials


logger = logging.getLogger("network_worker.telemetry")

METRICS_LOCK_KEY = "metrics:fleet:lock"
# Longer than a worst-case fleet run (3 rounds of 4 devices with full timeouts), so two
# runs never overlap; Beat's `expires` drops a run that could not start in time.
METRICS_LOCK_TTL_SECONDS = int(os.getenv("METRICS_LOCK_TTL_SECONDS", "300"))
METRICS_CONCURRENCY = int(os.getenv("METRICS_CONCURRENCY", "4"))
METRICS_CLI_TIMEOUT_SECONDS = float(os.getenv("METRICS_CLI_TIMEOUT_SECONDS", "30"))


def _now():
    return datetime.now(timezone.utc)


def _empty_metrics():
    return {"cpu_percent": None, "memory_percent": None, "memory_used_mb": None,
            "memory_total_mb": None, "uptime_seconds": None}


def status_for(metrics):
    have_cpu = metrics["cpu_percent"] is not None
    have_memory = metrics["memory_percent"] is not None

    if have_cpu and have_memory:
        return "success", None
    if have_cpu:
        return "partial", "Memory unavailable"
    if have_memory:
        return "partial", "CPU unavailable"
    return "failed", "Output contained neither CPU nor memory"


def collect_device(device):
    """Collect one device. Never raises and never writes: returns the sample to record."""
    started = time.monotonic()
    commands = TELEMETRY_COMMANDS.get(device["platform"], ())
    sample = {
        "device_id": device["id"],
        "hostname": device["hostname"],
        "collected_at": _now(),
        **_empty_metrics(),
        "source": "cli:" + "; ".join(commands) if commands else "cli:none",
        "status": "failed",
        "error": None,
    }

    def finish(error=None, metrics=None):
        if metrics is not None:
            sample.update({field: metrics[field] for field in FIELDS})
            sample["status"], default_error = status_for(metrics)
            # The parser's per-source reasons are more specific than the generic one.
            problems = metrics.get("problems") or []
            sample["error"] = _safe_error("; ".join(problems)) if problems else default_error
        else:
            sample["error"] = error
        sample["duration_ms"] = int((time.monotonic() - started) * 1000)
        return sample

    if not commands:
        return finish(f"Telemetry commands not yet validated for {device['platform']}")

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
            outputs[command] = session.send_command(command, read_timeout=METRICS_CLI_TIMEOUT_SECONDS)
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
        metrics = parse_metrics(device["platform"], outputs)
    except TelemetryParseError as exc:
        return finish(_safe_error(f"Telemetry output could not be parsed: {exc}", secrets))

    return finish(metrics=metrics)


def latest_payload(sample, last_success):
    """Redis value for metrics:device:<id> (also what the API serves as latest)."""
    return {
        "device_id": sample["device_id"],
        "status": sample["status"],
        "cpu_percent": sample["cpu_percent"],
        "memory_percent": sample["memory_percent"],
        "memory_used_mb": sample["memory_used_mb"],
        "memory_total_mb": sample["memory_total_mb"],
        "uptime_seconds": sample["uptime_seconds"],
        "collected_at": sample["collected_at"].isoformat(),
        "last_success_at": last_success.isoformat() if last_success else None,
        "error": sample["error"],
    }


def skipped_sample(device, acq):
    """A device skipped because of coordination: nothing is written anywhere."""
    status = ("skipped_change" if acq.change_active else
              "skipped_unavailable" if acq.status == "unavailable" else "skipped_busy")
    coord.count("collector_skipped")
    logger.info("%s collector=telemetry device_id=%s host=%s reason=%s",
                "collector_skipped_change" if status == "skipped_change" else "collector_skipped_busy",
                device["id"], device["hostname"], acq.reason)
    return {"device_id": device["id"], "hostname": device["hostname"], "status": status, "reason": acq.reason,
            "collected_at": _now(), "duration_ms": 0}


def collect_and_record(device):
    """Collect, store in PostgreSQL, then refresh the Redis latest value. Returns the sample."""
    acq = coord.acquire_device_operation(device["id"], "telemetry", hostname=device["hostname"])
    if not acq.acquired:
        return skipped_sample(device, acq)
    with acq.lease:
        sample = collect_device(device)
    insert_metric(sample)

    last_success = sample["collected_at"] if sample["status"] != "failed" else last_success_at(device["id"])
    cache_device_metrics(latest_payload(sample, last_success))

    log = logger.info if sample["status"] == "success" else logger.warning
    log(
        "%s device_id=%s host=%s status=%s cpu=%s memory=%s uptime_s=%s duration_ms=%s error=%s",
        "metrics_collection_device" if sample["status"] != "failed" else "metrics_collection_failed",
        device["id"], device["hostname"], sample["status"], sample["cpu_percent"], sample["memory_percent"],
        sample["uptime_seconds"], sample["duration_ms"], sample["error"],
    )
    return sample


def run_fleet_metrics(trigger="schedule"):
    started = time.monotonic()

    with FleetLock(key=METRICS_LOCK_KEY, ttl_seconds=METRICS_LOCK_TTL_SECONDS) as lock:
        if lock.acquired is False:
            logger.info("metrics_collection_skipped reason=lock_held trigger=%s", trigger)
            return {"skipped": True, "reason": "A fleet telemetry collection is already running"}

        devices = list_enabled_devices()
        logger.info("metrics_collection_started trigger=%s devices_total=%s concurrency=%s",
                    trigger, len(devices), METRICS_CONCURRENCY)

        counts = {"success": 0, "partial": 0, "failed": 0, "skipped_busy": 0, "skipped_change": 0,
                  "skipped_unavailable": 0}
        not_recorded = 0
        durations = {}

        # At most METRICS_CONCURRENCY SSH sessions at once; one device never fails the fleet.
        with ThreadPoolExecutor(max_workers=max(1, METRICS_CONCURRENCY)) as pool:
            futures = {pool.submit(collect_and_record, device): device for device in devices}
            for future in as_completed(futures):
                device = futures[future]
                try:
                    sample = future.result()
                    counts[sample["status"]] += 1
                    if not sample["status"].startswith("skipped"):
                        durations.setdefault(device["platform"], []).append(sample["duration_ms"])
                except Exception as exc:
                    not_recorded += 1
                    logger.warning("metrics_not_recorded device_id=%s host=%s error=%s",
                                   device["id"], device["hostname"], _safe_error(f"{type(exc).__name__}: {exc}"))

    summary = {
        "skipped": False,
        "trigger": trigger,
        "devices_total": len(devices),
        **counts,
        "not_recorded": not_recorded,
        "avg_duration_ms": {p: int(sum(d) / len(d)) for p, d in durations.items()},
        "duration_ms": int((time.monotonic() - started) * 1000),
    }

    logger.info(
        "metrics_collection_completed trigger=%s devices_total=%s success=%s partial=%s failed=%s "
        "skipped_busy=%s skipped_change=%s skipped_unavailable=%s not_recorded=%s duration_ms=%s avg_duration_ms=%s lock=%s",
        trigger, summary["devices_total"], counts["success"], counts["partial"], counts["failed"],
        counts["skipped_busy"], counts["skipped_change"], counts["skipped_unavailable"],
        not_recorded, summary["duration_ms"], summary["avg_duration_ms"],
        {True: "held", None: "redis_unavailable"}[lock.acquired],
    )
    return summary
