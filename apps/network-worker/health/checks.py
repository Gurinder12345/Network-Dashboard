"""
Lightweight, read-only device health checks.

Per device: TCP/22 -> SSH login -> one "show version". Nothing else is run: no configs,
backups, prechecks or inventory collection. Timeouts are deliberately short so one dead
switch cannot stall the fleet sweep.
"""

import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from netmiko import ConnectHandler
from netmiko.dell.dell_dnos6 import DellDNOS6SSH
from netmiko.exceptions import NetmikoAuthenticationException

from db.health import fleet_health_summary, list_enabled_devices, record_health
from health.cache import FleetLock, cache_device_health, cache_fleet_summary
from vault.client import get_device_credentials


logger = logging.getLogger("network_worker.health")

# Paramiko logs every SSH connect/auth at INFO; a 60 s sweep would flood the worker log.
logging.getLogger("paramiko").setLevel(logging.WARNING)

TCP_TIMEOUT_SECONDS = float(os.getenv("HEALTH_TCP_TIMEOUT_SECONDS", "3"))
TCP_ATTEMPTS = 2  # one retry for a transient connect failure
SSH_CONN_TIMEOUT_SECONDS = float(os.getenv("HEALTH_SSH_CONN_TIMEOUT_SECONDS", "5"))
SSH_AUTH_TIMEOUT_SECONDS = float(os.getenv("HEALTH_SSH_AUTH_TIMEOUT_SECONDS", "10"))
PROMPT_TIMEOUT_SECONDS = float(os.getenv("HEALTH_PROMPT_TIMEOUT_SECONDS", "20"))
CLI_TIMEOUT_SECONDS = float(os.getenv("HEALTH_CLI_TIMEOUT_SECONDS", "15"))
SLOW_THRESHOLD_MS = int(os.getenv("HEALTH_SLOW_THRESHOLD_MS", "15000"))
DOWN_AFTER_FAILURES = int(os.getenv("HEALTH_DOWN_AFTER_FAILURES", "2"))
FLEET_CONCURRENCY = int(os.getenv("HEALTH_FLEET_CONCURRENCY", "4"))
MAX_ERROR_LENGTH = 1024

HEALTH_COMMAND = "show version"
SUPPORTED_PLATFORMS = {"dell_os6", "dell_os10"}


class CredentialLookupError(Exception):
    """Vault could not provide credentials. A platform problem, not a device problem."""


class HealthDellOS6SSH(DellDNOS6SSH):
    """Dell OS6 session setup with a health-check prompt timeout (config paths wait 60 s)."""

    def session_preparation(self):
        self.ansi_escape_codes = True
        self.read_until_pattern(pattern=r"[>#]", read_timeout=PROMPT_TIMEOUT_SECONDS)
        self.set_base_prompt()
        self.enable()
        self.set_terminal_width()
        self.disable_paging(command="terminal length 0")


def _now():
    return datetime.now(timezone.utc)


def _safe_error(message, secrets=()):
    if not message:
        return None

    text = str(message)

    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")

    return text[:MAX_ERROR_LENGTH]


def _tcp_reachable(host, port=22):
    last_error = None

    for attempt in range(TCP_ATTEMPTS):
        try:
            with socket.create_connection((host, port), timeout=TCP_TIMEOUT_SECONDS):
                return True, None
        except OSError as exc:
            last_error = f"TCP {port} unreachable: {type(exc).__name__}: {exc}"
            if attempt + 1 < TCP_ATTEMPTS:
                time.sleep(1)

    return False, last_error


def _open_session(device, credentials):
    params = {
        "host": device["management_ip"],
        "username": credentials["username"],
        "password": credentials["password"],
        "secret": credentials.get("secret", ""),
        "port": 22,
        "conn_timeout": SSH_CONN_TIMEOUT_SECONDS,
        "auth_timeout": SSH_AUTH_TIMEOUT_SECONDS,
        "banner_timeout": SSH_AUTH_TIMEOUT_SECONDS,
        "fast_cli": False,
    }

    if device["platform"] == "dell_os6":
        return HealthDellOS6SSH(**params)

    return ConnectHandler(device_type="dell_os10", **params)


def probe_device(device):
    """Run the checks and return raw observations. Never writes anything."""
    started = time.monotonic()
    probe = {
        "tcp_reachable": False,
        "ssh_reachable": False,
        "cli_reachable": False,
        "response_time_ms": None,
        "error": None,
    }

    if device["platform"] not in SUPPORTED_PLATFORMS:
        probe["error"] = f"Unsupported platform for health check: {device['platform']}"
        return probe

    tcp_ok, tcp_error = _tcp_reachable(device["management_ip"])
    probe["tcp_reachable"] = tcp_ok

    if not tcp_ok:
        probe["error"] = tcp_error
        probe["response_time_ms"] = int((time.monotonic() - started) * 1000)
        return probe

    try:
        credentials = get_device_credentials(device["credential_path"])
    except Exception as exc:
        raise CredentialLookupError(f"Credential lookup failed: {type(exc).__name__}") from None

    secrets = (credentials.get("password"), credentials.get("secret"))
    session = None

    try:
        session = _open_session(device, credentials)
        probe["ssh_reachable"] = True

        output = session.send_command(HEALTH_COMMAND, read_timeout=CLI_TIMEOUT_SECONDS)

        if output and len(output.strip()) > 20 and "% Invalid" not in output:
            probe["cli_reachable"] = True
        else:
            probe["error"] = "CLI health command returned no usable output"

    except NetmikoAuthenticationException:
        probe["error"] = "SSH authentication failed"
    except Exception as exc:
        stage = "CLI command" if probe["ssh_reachable"] else "SSH session"
        probe["error"] = _safe_error(f"{stage} failed: {type(exc).__name__}: {exc}", secrets)
    finally:
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                pass

    probe["response_time_ms"] = int((time.monotonic() - started) * 1000)

    return probe


def classify(probe, previous, now):
    """
    healthy  : TCP + SSH + CLI succeed within SLOW_THRESHOLD_MS
    degraded : reachable but SSH/CLI failed or slow; or the first TCP failure
    down     : TCP/22 unreachable on DOWN_AFTER_FAILURES consecutive checks
    """
    prev_status = previous["status"] if previous else None
    prev_failures = previous["consecutive_failures"] if previous else 0
    prev_success = previous["last_success_at"] if previous else None
    prev_change = previous["last_status_change_at"] if previous else None

    cli_ok = probe["tcp_reachable"] and probe["ssh_reachable"] and probe["cli_reachable"]
    error = probe["error"]

    if cli_ok:
        failures = 0
        last_success = now
        if probe["response_time_ms"] is not None and probe["response_time_ms"] > SLOW_THRESHOLD_MS:
            status = "degraded"
            error = f"Slow response: {probe['response_time_ms']} ms (threshold {SLOW_THRESHOLD_MS} ms)"
        else:
            status = "healthy"
            error = None
    else:
        failures = prev_failures + 1
        last_success = prev_success
        if not probe["tcp_reachable"] and failures >= DOWN_AFTER_FAILURES:
            status = "down"
        else:
            status = "degraded"

    return {
        "status": status,
        "last_check_at": now,
        "last_success_at": last_success,
        "response_time_ms": probe["response_time_ms"],
        "tcp_reachable": probe["tcp_reachable"],
        "ssh_reachable": probe["ssh_reachable"],
        "cli_reachable": probe["cli_reachable"],
        "last_error": _safe_error(error),
        "consecutive_failures": failures,
        "last_status_change_at": now if status != prev_status else prev_change,
    }


def check_and_record(device):
    """Probe one device, persist to PostgreSQL, then refresh Redis. Returns the stored row."""
    logger.info("device_health_check_started device_id=%s host=%s", device["id"], device["hostname"])

    probe = probe_device(device)
    now = _now()

    previous, current = record_health(device["id"], lambda prev: classify(probe, prev, now))

    # PostgreSQL first, then the cache.
    cache_device_health(current)

    prev_status = previous["status"] if previous else "unknown"

    if prev_status != current["status"]:
        logger.warning(
            "device_health_transition device_id=%s host=%s from=%s to=%s reason=%s",
            device["id"],
            device["hostname"],
            prev_status,
            current["status"],
            current["last_error"] or "ok",
        )

    logger.info(
        "device_health_check_completed device_id=%s host=%s status=%s response_ms=%s tcp=%s ssh=%s cli=%s",
        device["id"],
        device["hostname"],
        current["status"],
        current["response_time_ms"],
        current["tcp_reachable"],
        current["ssh_reachable"],
        current["cli_reachable"],
    )

    return current


def run_fleet_health_check(trigger="schedule"):
    started = time.monotonic()

    with FleetLock() as lock:
        if lock.acquired is False:
            logger.info("fleet_health_skipped reason=lock_held trigger=%s", trigger)
            return {"skipped": True, "reason": "A fleet health check is already running"}

        devices = list_enabled_devices()
        counts = {"healthy": 0, "degraded": 0, "down": 0, "unknown": 0}
        failed_to_process = 0

        # Bounded pool: at most FLEET_CONCURRENCY SSH sessions at once. One device's
        # exception never fails the sweep.
        with ThreadPoolExecutor(max_workers=max(1, FLEET_CONCURRENCY)) as pool:
            futures = {pool.submit(check_and_record, device): device for device in devices}

            for future in as_completed(futures):
                device = futures[future]
                try:
                    counts[future.result()["status"]] += 1
                except Exception as exc:
                    failed_to_process += 1
                    logger.warning(
                        "device_health_not_recorded device_id=%s host=%s error=%s",
                        device["id"],
                        device["hostname"],
                        _safe_error(f"{type(exc).__name__}: {exc}"),
                    )

        # Summary comes from PostgreSQL (authoritative), then is cached.
        summary = fleet_health_summary()
        cache_fleet_summary(summary)

    elapsed_ms = int((time.monotonic() - started) * 1000)

    logger.info(
        "fleet_health_completed trigger=%s checked=%s healthy=%s degraded=%s down=%s "
        "failed_to_process=%s elapsed_ms=%s lock=%s",
        trigger,
        len(devices),
        counts["healthy"],
        counts["degraded"],
        counts["down"],
        failed_to_process,
        elapsed_ms,
        {True: "held", None: "redis_unavailable"}[lock.acquired],
    )

    return {
        "skipped": False,
        "trigger": trigger,
        "checked": len(devices),
        **counts,
        "failed_to_process": failed_to_process,
        "elapsed_ms": elapsed_ms,
        "summary": summary,
    }
