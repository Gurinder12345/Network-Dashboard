"""
Lightweight, read-only device health checks.

Per device: one ICMP echo (supplemental) -> TCP/22 -> SSH login -> one "show version".
Nothing else is run: no configs, backups, prechecks or inventory collection. Timeouts are
deliberately short so one dead switch cannot stall the fleet sweep.

Evidence (stored per check; DB/API column names kept for compatibility):
  icmp_reachable  ICMP echo reply (True/False; None = ICMP could not be attempted)
  tcp_reachable   TCP port 22 accepted a connection (TCP/22 only, not "device reachable")
  ssh_reachable   SSH session established AND authenticated
  cli_reachable   the verification command returned usable output

Classification (classify), with a stable reason code in health_reason:
  healthy   TCP/22 + SSH + CLI ok (ICMP not required: it may be blocked)      ok
            ... but slower than HEALTH_SLOW_THRESHOLD_MS -> degraded           slow_response
  degraded  ping replies, TCP/22 closed/filtered                               ssh_management_unavailable
            TCP/22 open, SSH login rejected / timed out / session error        ssh_authentication_failed |
                                                                               ssh_timeout | ssh_session_failed
            SSH ok, verification command failed                                cli_verification_failed
  down      no TCP/22 AND no ICMP reply on HEALTH_DOWN_AFTER_FAILURES
            consecutive checks (unreachable_count); the first such check is
            degraded                                                           network_unreachable
            (ICMP unavailable in the pod: TCP/22 alone, the previous rule)     tcp22_unreachable
A device that answers ping never becomes "down", and management failures (auth, CLI)
never count towards "down" (consecutive_failures keeps counting every failed check).

Device coordination (coordination/device_ops.py), health = priority 2:
  * takes the device's operation slot (waits up to COORD_HEALTH_WAIT_SECONDS, during which
    telemetry/topology yield to it);
  * slot still held by a low-priority collector -> health runs anyway (never disappears);
  * slot held by / reserved for change-workflow work (apply, precheck, backup) -> a
    TCP-only observation, no SSH, so health never competes with a change session;
  * Redis unavailable -> unlocked full check (health is the outage canary).
Change grace: while an approved change is in progress, a failed observation does not
immediately become degraded/down. The previous status is kept, the evidence is stored
(last_error, flags, consecutive_failures) for CHANGE_HEALTH_GRACE_SECONDS from the first
failure; after that the normal rules apply, so real outages always become visible.
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

from netmiko.exceptions import NetmikoTimeoutException

from coordination import device_ops as coord
from db.health import fleet_health_summary, list_enabled_devices, record_health
from health.cache import FleetLock, cache_device_health, cache_fleet_summary
from health.icmp import icmp_echo
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


def probe_device(device, tcp_only=False):
    """Run the checks and return raw observations. Never writes anything."""
    probe = {
        "icmp_reachable": None,
        "tcp_reachable": False,
        "ssh_reachable": False,
        "cli_reachable": False,
        "response_time_ms": None,
        "error": None,
        "failure": None,  # reason code of the first failed management stage
    }

    if device["platform"] not in SUPPORTED_PLATFORMS:
        probe["error"] = f"Unsupported platform for health check: {device['platform']}"
        probe["failure"] = "unsupported_platform"
        return probe

    # Supplemental, ~1 s worst case; not part of the response time below.
    probe["icmp_reachable"] = icmp_echo(device["management_ip"])

    started = time.monotonic()
    tcp_ok, tcp_error = _tcp_reachable(device["management_ip"])
    probe["tcp_reachable"] = tcp_ok

    if not tcp_ok:
        probe["error"] = tcp_error
        probe["failure"] = "tcp22_unreachable"
        probe["response_time_ms"] = int((time.monotonic() - started) * 1000)
        return probe

    if tcp_only:
        # Change-workflow work holds the device: observe reachability only, no SSH login.
        probe["tcp_only"] = True
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
            probe["failure"] = "cli_verification_failed"

    except NetmikoAuthenticationException:
        probe["error"] = "SSH authentication failed"
        probe["failure"] = "ssh_authentication_failed"
    except Exception as exc:
        stage = "CLI command" if probe["ssh_reachable"] else "SSH session"
        probe["error"] = _safe_error(f"{stage} failed: {type(exc).__name__}: {exc}", secrets)
        if probe["ssh_reachable"]:
            probe["failure"] = "cli_verification_failed"
        elif isinstance(exc, (NetmikoTimeoutException, socket.timeout, TimeoutError)):
            probe["failure"] = "ssh_timeout"
        else:
            probe["failure"] = "ssh_session_failed"
    finally:
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                pass

    probe["response_time_ms"] = int((time.monotonic() - started) * 1000)

    return probe


def _unreachable_streak(previous):
    """Previous network-unreachable streak (falls back to the old counter before migration 010)."""
    if not previous:
        return 0
    if previous.get("unreachable_count") is not None:
        return previous["unreachable_count"]
    if previous.get("tcp_reachable") is False:
        return previous.get("consecutive_failures") or 0
    return 0


def classify(probe, previous, now):
    """
    Explicit, evidence-based classification (see the module docstring for the table).
    Pure function: `probe` from probe_device, `previous` the stored row (or None).
    """
    prev_status = previous["status"] if previous else None
    prev_failures = previous["consecutive_failures"] if previous else 0
    prev_success = previous["last_success_at"] if previous else None
    prev_change = previous["last_status_change_at"] if previous else None

    icmp = probe.get("icmp_reachable")  # True / False / None (not tested)
    tcp22 = probe["tcp_reachable"]
    management_ok = tcp22 and probe["ssh_reachable"] and probe["cli_reachable"]
    error = probe["error"]
    unreachable = 0

    if management_ok:
        failures = 0
        last_success = now
        if probe["response_time_ms"] is not None and probe["response_time_ms"] > SLOW_THRESHOLD_MS:
            status, reason = "degraded", "slow_response"
            error = f"Slow response: {probe['response_time_ms']} ms (threshold {SLOW_THRESHOLD_MS} ms)"
        else:
            status, reason, error = "healthy", "ok", None
    else:
        failures = prev_failures + 1
        last_success = prev_success
        if not tcp22 and icmp is True:
            # The device answers on the network; only the SSH management plane is unavailable.
            status, reason = "degraded", "ssh_management_unavailable"
            error = f"Reachable by ICMP, SSH management unavailable ({error or 'TCP/22 unreachable'})"
        elif not tcp22:
            # No TCP/22 and no ICMP reply (or ICMP not testable): network-unreachable streak.
            unreachable = _unreachable_streak(previous) + 1
            reason = "network_unreachable" if icmp is False else "tcp22_unreachable"
            status = "down" if unreachable >= DOWN_AFTER_FAILURES else "degraded"
            evidence = "no ICMP reply and TCP/22 unreachable" if icmp is False else "TCP/22 unreachable (ICMP not available)"
            error = f"{evidence}: {error}" if status == "down" else \
                f"{evidence} ({unreachable} of {DOWN_AFTER_FAILURES} checks before down): {error}"
        else:
            # TCP/22 answered: management problem only, never "down".
            status = "degraded"
            reason = probe.get("failure") or ("cli_verification_failed" if probe["ssh_reachable"] else "ssh_session_failed")

    return {
        "status": status,
        "last_check_at": now,
        "last_success_at": last_success,
        "response_time_ms": probe["response_time_ms"],
        "icmp_reachable": icmp,
        "tcp_reachable": tcp22,
        "ssh_reachable": probe["ssh_reachable"],
        "cli_reachable": probe["cli_reachable"],
        "last_error": _safe_error(error),
        "health_reason": reason,
        "consecutive_failures": failures,
        "unreachable_count": unreachable,
        "last_status_change_at": now if status != prev_status else prev_change,
    }


def classify_during_change(probe, previous, now, change, grace_started, now_epoch):
    """
    Change-aware classification (only while an approved change is in progress):
      * TCP-only observation that succeeded: keep the previous stable status (no fake
        healthy, no fake failure); only last_check_at / tcp_reachable are updated.
      * failed observation within the grace window: keep the previous status, store the
        evidence and count the failure.
      * failed observation after the grace window: normal rules (degraded / down).
    """
    previous = previous or {}
    if probe.get("tcp_only") and probe["tcp_reachable"]:
        return {
            "status": previous.get("status") or "unknown",
            "last_check_at": now,
            "last_success_at": previous.get("last_success_at"),
            "response_time_ms": previous.get("response_time_ms"),
            "icmp_reachable": probe.get("icmp_reachable"),
            "tcp_reachable": True,
            "ssh_reachable": previous.get("ssh_reachable"),
            "cli_reachable": previous.get("cli_reachable"),
            "last_error": previous.get("last_error"),
            "health_reason": previous.get("health_reason"),
            "consecutive_failures": previous.get("consecutive_failures") or 0,
            "unreachable_count": 0,
            "last_status_change_at": previous.get("last_status_change_at"),
        }

    in_grace = grace_started is not None and now_epoch - grace_started < coord.CHANGE_HEALTH_GRACE_SECONDS
    normal = classify(probe, previous or None, now)
    if not in_grace:
        return normal
    elapsed = int(now_epoch - grace_started)
    return {
        **normal,
        "status": previous.get("status") or "unknown",
        "last_error": _safe_error(f"{probe['error'] or 'health check failed'} (change {change.get('change_id')} in "
                                  f"progress: transition held for grace, {elapsed}s of "
                                  f"{coord.CHANGE_HEALTH_GRACE_SECONDS}s)"),
        "last_status_change_at": previous.get("last_status_change_at"),
    }


def _coordinated_probe(device, lease):
    """Probe under the device slot. Returns (probe, mode)."""
    if lease is not None:  # the caller (change workflow) already owns the device
        return probe_device(device), "owner"

    acq = coord.acquire_device_operation(device["id"], "health", hostname=device["hostname"])
    if acq.acquired:
        with acq.lease:
            return probe_device(device), "locked"
    if acq.status == "unavailable":
        return probe_device(device), "unlocked_redis_unavailable"
    holder = acq.holder or {}
    if holder.get("priority", 9) <= 1:
        return probe_device(device, tcp_only=True), f"tcp_only:{holder.get('operation')}"
    # A low-priority collector still holds the slot after our wait: health never disappears.
    logger.info("health_bypass_low_priority device_id=%s host=%s held_by=%s wait_ms=%s",
                device["id"], device["hostname"], holder.get("operation"), acq.wait_ms)
    return probe_device(device), f"bypass:{holder.get('operation')}"


def check_and_record(device, lease=None):
    """
    Probe one device, persist to PostgreSQL, then refresh Redis. Returns the stored row.
    `lease`: the caller already owns the device slot (post-change health verification).
    """
    logger.info("device_health_check_started device_id=%s host=%s", device["id"], device["hostname"])

    probe, mode = _coordinated_probe(device, lease)
    now = _now()
    change = coord.change_in_progress(device["id"])
    healthy_probe = probe["tcp_reachable"] and (probe.get("tcp_only") or (probe["ssh_reachable"] and probe["cli_reachable"]))

    if change is None:
        coord.end_grace(device["id"], "no_change")
        classifier = lambda prev: classify(probe, prev, now)  # noqa: E731
    else:
        now_epoch = time.time()
        grace = None if healthy_probe else coord.grace_started_at(device["id"], now_epoch)
        if healthy_probe and not probe.get("tcp_only"):
            coord.end_grace(device["id"], "health_recovered")
        classifier = lambda prev: classify_during_change(probe, prev, now, change, grace, now_epoch)  # noqa: E731

    previous, current = record_health(device["id"], classifier)

    # PostgreSQL first, then the cache.
    cache_device_health(current)

    prev_status = previous["status"] if previous else "unknown"

    if prev_status != current["status"]:
        logger.warning(
            "device_health_transition device_id=%s host=%s from=%s to=%s reason=%s detail=%s",
            device["id"],
            device["hostname"],
            prev_status,
            current["status"],
            current.get("health_reason") or "ok",
            current["last_error"] or "ok",
        )

    logger.info(
        "device_health_check_completed device_id=%s hostname=%s status=%s reason=%s icmp_reachable=%s "
        "tcp22_reachable=%s ssh_authenticated=%s cli_verified=%s response_ms=%s consecutive_failures=%s "
        "unreachable_count=%s mode=%s change=%s",
        device["id"],
        device["hostname"],
        current["status"],
        current.get("health_reason"),
        current.get("icmp_reachable"),
        current["tcp_reachable"],
        current["ssh_reachable"],
        current["cli_reachable"],
        current["response_time_ms"],
        current["consecutive_failures"],
        current.get("unreachable_count"),
        mode,
        change.get("change_id") if change else None,
    )

    return {**current, "coordination_mode": mode}


def run_fleet_health_check(trigger="schedule"):
    started = time.monotonic()

    with FleetLock() as lock:
        if lock.acquired is False:
            logger.info("fleet_health_skipped reason=lock_held trigger=%s", trigger)
            return {"skipped": True, "reason": "A fleet health check is already running"}

        devices = list_enabled_devices()
        counts = {"healthy": 0, "degraded": 0, "down": 0, "unknown": 0}
        modes = {}
        failed_to_process = 0

        # Bounded pool: at most FLEET_CONCURRENCY SSH sessions at once. One device's
        # exception never fails the sweep.
        with ThreadPoolExecutor(max_workers=max(1, FLEET_CONCURRENCY)) as pool:
            futures = {pool.submit(check_and_record, device): device for device in devices}

            for future in as_completed(futures):
                device = futures[future]
                try:
                    result = future.result()
                    counts[result["status"]] += 1
                    mode = result.get("coordination_mode", "locked").split(":")[0]
                    modes[mode] = modes.get(mode, 0) + 1
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
        "coordination": modes,
        "elapsed_ms": elapsed_ms,
        "summary": summary,
    }
