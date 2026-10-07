"""
Read-only LLDP topology discovery.

Per device: Vault credentials -> SSH -> exactly one LLDP show command (by platform) ->
parse -> correlate with inventory -> persist. Nothing else runs on the switch.

Failure safety: if collection or parsing fails for a device, its existing links are left
exactly as they were (not deactivated) and only the failed attempt is recorded. Device
health is never touched by topology discovery.

Device coordination (coordination/device_ops.py), topology = priority 5 (lowest): the
device's operation slot is taken without waiting; a busy device (or one with a change in
progress, or with coordination unavailable) is skipped for this sweep -- no failure is
recorded, existing links stay, and the next 5-minute run catches up.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from coordination import device_ops as coord
from db.health import list_enabled_devices
from db.jobs import create_audit_event
from db.topology import (
    get_discovery_state,
    list_inventory,
    list_lldp_identities,
    record_discovery_failure,
    record_discovery_success,
)
from health.cache import FleetLock, cache_topology_last_discovery, invalidate_topology_graph
from health.checks import _open_session, _safe_error
from topology.correlate import correlate
from topology.lldp import lldp_command_for, parse_lldp_output
from vault.client import get_device_credentials


logger = logging.getLogger("network_worker.topology")

TOPOLOGY_LOCK_KEY = "topology:fleet:lock"
TOPOLOGY_LOCK_TTL_SECONDS = int(os.getenv("TOPOLOGY_LOCK_TTL_SECONDS", "600"))
TOPOLOGY_CONCURRENCY = int(os.getenv("TOPOLOGY_CONCURRENCY", "4"))
LLDP_CLI_TIMEOUT_SECONDS = float(os.getenv("TOPOLOGY_CLI_TIMEOUT_SECONDS", "60"))


def _now():
    return datetime.now(timezone.utc)


def collect_lldp_output(device):
    """Runs the platform's single LLDP show command. Returns raw text (never logged/stored)."""
    command = lldp_command_for(device["platform"])
    credentials = get_device_credentials(device["credential_path"])
    secrets = (credentials.get("password"), credentials.get("secret"))

    session = None
    try:
        session = _open_session(device, credentials)
        return command, session.send_command(command, read_timeout=LLDP_CLI_TIMEOUT_SECONDS)
    except Exception as exc:
        raise RuntimeError(_safe_error(f"LLDP collection failed: {type(exc).__name__}: {exc}", secrets)) from None
    finally:
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                pass


def _audit(device_id, event_type, message):
    try:
        create_audit_event(job_id=None, device_id=device_id, event_type=event_type, message=message)
    except Exception:
        logger.warning("topology audit event %s not recorded", event_type)


def discover_device(device, inventory, identities=None):
    """Discover one device. Never raises: returns a result dict with success/failure/skipped."""
    started = time.monotonic()
    attempted_at = _now()
    result = {"device_id": device["id"], "hostname": device["hostname"], "success": False}

    acq = coord.acquire_device_operation(device["id"], "topology", hostname=device["hostname"])
    if not acq.acquired:
        skipped = ("skipped_change" if acq.change_active else
                   "skipped_unavailable" if acq.status == "unavailable" else "skipped_busy")
        coord.count("collector_skipped")
        logger.info("%s collector=topology device_id=%s host=%s reason=%s",
                    "collector_skipped_change" if skipped == "skipped_change" else "collector_skipped_busy",
                    device["id"], device["hostname"], acq.reason)
        return {**result, "skipped": skipped, "reason": acq.reason}
    with acq.lease:
        return _discover_locked(device, inventory, identities, started, attempted_at, result)


def _discover_locked(device, inventory, identities, started, attempted_at, result):

    try:
        command, output = collect_lldp_output(device)
        neighbors, warnings = parse_lldp_output(device["platform"], output)
        correlated = correlate(neighbors, inventory, device["id"], identities)
        counts = record_discovery_success(device["id"], correlated, command, attempted_at)
    except Exception as exc:
        error = _safe_error(f"{type(exc).__name__}: {exc}")
        previous = None
        try:
            previous = get_discovery_state(device["id"])
            record_discovery_failure(device["id"], error, attempted_at)
        except Exception:
            logger.warning("topology failure state not recorded device_id=%s", device["id"])

        # Audit only the transition into failure, not every 5-minute retry.
        if previous is None or previous.get("last_error") is None:
            _audit(device["id"], "topology_discovery_failed",
                   f"LLDP discovery failed for {device['hostname']}; existing links kept: {error}")

        logger.warning("topology_discovery_failed device_id=%s host=%s error=%s", device["id"], device["hostname"], error)
        return {**result, "error": error, "elapsed_ms": int((time.monotonic() - started) * 1000)}

    managed = sum(1 for n in correlated if n.get("remote_device_id"))
    match_types = {}
    for n in correlated:
        match_types[n["match_type"]] = match_types.get(n["match_type"], 0) + 1

    # Identity conflicts never pick a device; surface them instead of hiding them.
    conflicts = [
        f"{n['local_interface']}: {n['correlation_note']}" for n in correlated if n.get("match_type") == "conflict"
    ]
    for conflict in conflicts:
        logger.warning("topology_identity_conflict device_id=%s host=%s %s", device["id"], device["hostname"], conflict)

    logger.info(
        "topology_discovery_device device_id=%s host=%s neighbors=%s managed=%s new=%s deactivated=%s warnings=%s match_types=%s",
        device["id"], device["hostname"], len(correlated), managed, counts["new"], counts["deactivated"], len(warnings),
        match_types,
    )

    return {
        **result,
        "success": True,
        "neighbors_seen": len(correlated),
        "managed_links": managed,
        "unmanaged_neighbors": len(correlated) - managed,
        "match_types": match_types,
        "identity_conflicts": conflicts,
        "new_links": counts["new"],
        "deactivated_links": counts["deactivated"],
        "parser_warnings": warnings,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def run_fleet_topology_discovery(trigger="schedule"):
    started = time.monotonic()

    with FleetLock(key=TOPOLOGY_LOCK_KEY, ttl_seconds=TOPOLOGY_LOCK_TTL_SECONDS) as lock:
        if lock.acquired is False:
            logger.info("topology_discovery_skipped reason=lock_held trigger=%s", trigger)
            return {"skipped": True, "reason": "A topology discovery is already running"}

        if trigger == "manual":
            _audit(None, "topology_discovery_started", "Manual LLDP topology discovery started")

        devices = list_enabled_devices()
        inventory = list_inventory()
        identities = list_lldp_identities()
        results = []

        # At most TOPOLOGY_CONCURRENCY SSH sessions at once; one device never fails the fleet.
        with ThreadPoolExecutor(max_workers=max(1, TOPOLOGY_CONCURRENCY)) as pool:
            futures = {pool.submit(discover_device, device, inventory, identities): device for device in devices}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    device = futures[future]
                    results.append({"device_id": device["id"], "hostname": device["hostname"], "success": False,
                                    "error": _safe_error(f"{type(exc).__name__}: {exc}")})

    ok = [r for r in results if r["success"]]
    skipped = [r for r in results if r.get("skipped")]
    summary = {
        "skipped": False,
        "trigger": trigger,
        "checked": len(results),
        "successful": len(ok),
        "failed": len(results) - len(ok) - len(skipped),
        "skipped_busy": sum(1 for r in skipped if r["skipped"] == "skipped_busy"),
        "skipped_change": sum(1 for r in skipped if r["skipped"] == "skipped_change"),
        "skipped_unavailable": sum(1 for r in skipped if r["skipped"] == "skipped_unavailable"),
        "neighbors_seen": sum(r["neighbors_seen"] for r in ok),
        "managed_links": sum(r["managed_links"] for r in ok),
        "unmanaged_neighbors": sum(r["unmanaged_neighbors"] for r in ok),
        "identity_conflicts": [f"{r['hostname']} {c}" for r in ok for c in r.get("identity_conflicts", [])],
        "new_links": sum(r["new_links"] for r in ok),
        "deactivated_links": sum(r["deactivated_links"] for r in ok),
        "failed_devices": [{"device_id": r["device_id"], "hostname": r["hostname"], "error": r.get("error")}
                           for r in results if not r["success"] and not r.get("skipped")],
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "finished_at": _now().isoformat(),
    }

    # PostgreSQL is already written; now refresh the cache.
    if ok:
        invalidate_topology_graph()
    cache_topology_last_discovery(summary)

    changed = summary["new_links"] or summary["deactivated_links"]
    if trigger == "manual" or changed:
        _audit(None, "topology_discovery_completed",
               f"LLDP discovery ({trigger}): {summary['successful']}/{summary['checked']} devices, "
               f"{summary['neighbors_seen']} neighbors, {summary['new_links']} new, "
               f"{summary['deactivated_links']} no longer seen, {summary['failed']} failed")

    logger.info(
        "topology_discovery_completed trigger=%s checked=%s successful=%s failed=%s skipped_busy=%s "
        "skipped_change=%s neighbors=%s managed=%s unmanaged=%s new=%s deactivated=%s elapsed_ms=%s",
        trigger, summary["checked"], summary["successful"], summary["failed"], summary["skipped_busy"],
        summary["skipped_change"], summary["neighbors_seen"],
        summary["managed_links"], summary["unmanaged_neighbors"], summary["new_links"],
        summary["deactivated_links"], summary["elapsed_ms"],
    )

    return summary
