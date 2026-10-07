"""
Shared guarded-change workflow for every supported platform (Dell OS6, Dell OS10), using
the ordered configuration-block model (changes/blocks.py). There is no configuration
command policy: any CLI may be submitted; the device is the syntax authority. The
operational protections stay: precheck, pre-change backup, human approval, atomic apply
claim, one execution per approval, post-check, jobs and audit.

Precheck (read-only, all-or-nothing):
    device enabled -> structural validation -> read device state ONCE (running config;
    OS6 `show vlan` when VLAN commands are present) -> per block: config capture,
    detectable problems, semantic pre-validation where supported
    -> [any block FAILED: precheck_failed, nothing stored] [all already in effect: done]
    -> pre-change backup from that same snapshot (no backup => no approval => no write)
    -> one pending approval for the whole multi-block change

Apply (the API has already claimed approved -> applying atomically):
    status check -> backup belongs to this device and succeeded -> structural re-check
    -> EXCLUSIVE device slot (coordination; waits for the current holder, collectors
    yield; busy/unavailable -> approval back to approved, nothing sent)
    -> change-in-progress state (health grace, collectors skip)
    -> execution claim (execution_result recorded once; a duplicate/redelivered task stops)
    -> one config_apply job -> fresh pre-apply running-config backup under the same slot
    (no backup => nothing sent) -> blocks sent strictly in order in one session, stopping
    at the first rejected command -> per-block status persisted (applied / failed /
    not_attempted) -> post-check: running config re-read, semantic verification where
    supported, operator verification commands -> immediate post-change health check
    -> applied, or failed (partial apply is reported block by block; nothing is rolled
    back automatically) -> finally: change state cleared, slot released (owner-checked;
    both also expire by TTL if the worker dies).

Coordination TTLs are refreshed by the owner at each phase boundary with that phase's
own budget (no background thread), so a crashed worker frees the device within one
phase budget.
"""

import logging
import re
from datetime import datetime, timezone

from changes import execution as exe
from changes.blocks import (
    block_label,
    blocks_from_legacy,
    command_count,
    flatten,
    normalize_blocks,
    normalize_verification_commands,
    render_cli,
    stored_blocks,
)
from changes.os10 import VerificationError
from changes.platforms import platform_for
from changes.verify import NOT_AVAILABLE, NOT_PRESENT, VERIFIED, precheck_block_issues, semantic_status, verify_blocks
from coordination import device_ops as coord
from db.approvals import (
    begin_execution,
    release_apply_claim,
    create_change_approval,
    get_change_approval,
    mark_approval_applied,
    mark_approval_applying,
    mark_approval_failed,
    set_execution_result,
    verify_backup_for_device,
)
from db.devices import get_device_by_hostname, get_device_by_id
from db.jobs import create_audit_event, create_job, mark_job_failed, mark_job_success
from tasks.config_backup import classify_backup_error, perform_config_backup

logger = logging.getLogger("network_worker.changes")

MAX_ERROR = 600

# Per-phase coordination budgets (slot + change-in-progress TTL), from existing timeouts:
#   pre-apply backup  running-config read: OS10 read_timeout 120 s; OS6 up to 3 attempts
#   apply             ansible/blocks_runner.py timeout: 180 s + 3 s per step (max 3600 s)
#   postcheck         running-config read + 120 s per verification command
#   health            one lightweight check (worst ~57 s)
PHASE_BACKUP_SECONDS = 600
PHASE_POSTCHECK_SECONDS = 600
PHASE_HEALTH_SECONDS = 120
PHASE_MARGIN_SECONDS = 60


def _apply_phase_seconds(blocks):
    from ansible.blocks_runner import BASE_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, PER_STEP_SECONDS

    steps = sum(len(b["commands"]) + (3 if b["parent"] else 2) for b in blocks)
    return min(MAX_TIMEOUT_SECONDS, int(BASE_TIMEOUT_SECONDS + PER_STEP_SECONDS * steps)) + PHASE_MARGIN_SECONDS


def _busy_text(acq, hostname):
    if acq.status == "unavailable":
        return "device coordination is unavailable (Redis)"
    holder = acq.holder or {}
    if acq.change_active:
        return f"a configuration change is in progress on {hostname}"
    return f"{hostname} is busy ({holder.get('operation', 'another operation')})"


class ChangeError(RuntimeError):
    """A sanitized, user-facing failure."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _audit(job_id, device_id, event_type, message):
    try:
        create_audit_event(job_id=job_id, device_id=device_id, event_type=event_type,
                           message=exe.redact(message)[:2000])
    except Exception:
        logger.warning("audit event %s not recorded", event_type)


def ansible_failure_summary(result):
    """Short reason from ansible-playbook output: the failing task's msg / device error line."""
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    device_error = re.search(r"%\s?Error:?[^\\\r\n'\"]*", text)
    if device_error:
        reason = device_error.group(0).strip()
    else:
        msg = re.search(r'"msg":\s*"([^"]+)"', text[text.find("failed"):] if "failed" in text else text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        flagged = [line for line in lines if line.startswith(("fatal:", "ERROR!", "[ERROR]"))]
        reason = msg.group(1) if msg else (flagged[0] if flagged else (lines[-1] if lines else "no output"))
    return f"Ansible apply failed (rc={result.get('returncode')}): {reason}"[:MAX_ERROR]


def _sanitized(stage, exc):
    if isinstance(exc, ChangeError):
        return str(exc)
    if isinstance(exc, VerificationError):  # our own message: no device output or secrets
        return f"{stage}: {exc}"[:MAX_ERROR]
    return f"{stage}: {classify_backup_error(exc)}"


# ---- precheck ------------------------------------------------------------------------------
def _precheck_report(adapter, blocks, state, verification_commands):
    """Structured precheck: per-block PASS/FAILED + four separate check categories."""
    verified = verify_blocks(adapter.name, blocks, state["running_config"], state.get("vlan_ids"))
    report_blocks, command_results, already, proposed = [], [], [], []
    for block, entry in zip(blocks, verified):
        issues = precheck_block_issues(adapter.name, block, entry["found"])
        for result in entry["commands"]:
            command_results.append({**result, "block": entry["index"] + 1,
                                    "desired_state_present": result["status"] == VERIFIED,
                                    "verification_method": result["method"], "type": result["status"]})
            (already if result["status"] == VERIFIED else proposed).append(result["command"])
        report_blocks.append({
            "index": entry["index"], "parent": block["parent"], "global": block["parent"] is None,
            "command_count": len(block["commands"]), "status": "FAILED" if issues else "PASS", "issues": issues,
            "current_config_found": entry["found"], "current_config": entry["current_config"],
            "commands": entry["commands"],
        })

    statuses = {r["status"] for r in command_results}
    semantic = ("SUPPORTED" if statuses <= {VERIFIED, NOT_PRESENT} else
                "NOT AVAILABLE" if statuses == {NOT_AVAILABLE} else "PARTIAL")
    failed = any(b["status"] == "FAILED" for b in report_blocks)
    return {
        "overall": "FAILED" if failed else "PASS",
        "checks": {"structural_validation": "PASS", "device_connectivity": "PASS",
                   "current_config_capture": "PASS", "semantic_verification": semantic},
        "blocks": report_blocks,
        "block_count": len(blocks),
        "command_count": command_count(blocks),
        "cli_preview": render_cli(blocks),
        "verification_commands": verification_commands,
        # Shape shared with the UI's per-command table.
        "already_present": already,
        "proposed_changes": proposed,
        "would_change": bool(proposed),
        "verification_method": "running-configuration",
        "command_results": command_results,
    }


def _summary_for_approval(report):
    """Compact precheck result stored on the approval (no captured configuration)."""
    return {
        "overall": report["overall"], "checks": report["checks"], "block_count": report["block_count"],
        "command_count": report["command_count"],
        "blocks": [{"index": b["index"], "parent": b["parent"], "status": b["status"], "issues": b["issues"],
                    "commands": [{"status": c["status"], "detail": c["detail"]} for c in b["commands"]]}
                   for b in report["blocks"]],
    }


def run_precheck(target_host, config_lines=None, config_parents=None, *, backup, expected_platform=None,
                 config_blocks=None, verification_commands=None):
    device = get_device_by_hostname(target_host)  # enabled devices only
    try:
        adapter = platform_for(device["platform"])
        if expected_platform and device["platform"] != expected_platform:
            raise ValueError(f"{target_host} is {device['platform']}, not {expected_platform}")

        blocks = normalize_blocks(config_blocks if config_blocks is not None
                                  else blocks_from_legacy(config_lines, config_parents))
        verify_cmds = normalize_verification_commands(verification_commands)

        # SSH-heavy read: take the device slot (priority 1; waits for a collector to finish,
        # new telemetry/topology yield). Fail closed if busy or coordination is unavailable.
        acq = coord.acquire_device_operation(device["id"], "precheck", hostname=target_host)
        if not acq.acquired:
            raise ChangeError(f"Precheck not started: {_busy_text(acq, target_host)}; try again shortly")
        try:
            state = adapter.read_device_state(target_host, blocks)
        except Exception as exc:
            raise ChangeError(_sanitized("Could not read the running configuration", exc)) from None
        finally:
            acq.lease.release()
        snapshot = state["running_config"]

        report = _precheck_report(adapter, blocks, state, verify_cmds)
        base = {"target_host": target_host, "platform": adapter.name, "dry_run": report,
                "block_count": len(blocks), "command_count": command_count(blocks)}

        if report["overall"] == "FAILED":
            failed = [f"Block {b['index'] + 1}: {'; '.join(b['issues'])}" for b in report["blocks"] if b["issues"]]
            _audit(None, device["id"], "precheck_failed",
                   f"Precheck for {target_host} ({adapter.label}) failed: {' | '.join(failed)}")
            return {**base, "status": "precheck_failed", "rejection_reasons": failed,
                    "backup_required": False, "ready_for_approval": False}

        if not report["would_change"]:
            _audit(None, device["id"], "precheck_completed",
                   f"Precheck for {target_host} ({adapter.label}): every command already in effect; no change required")
            return {**base, "status": "no_change_required", "backup_required": False, "ready_for_approval": False}

        backup_result = backup(target_host, config_snapshot=snapshot)
        if backup_result.get("status") != "success":
            raise RuntimeError(f"Backup failed for {target_host}")

        approval = create_change_approval(
            device_id=device["id"],
            backup_job_id=backup_result["job_id"],
            config_lines=flatten(blocks),
            config_parents=None,
            requested_by="system",
            config_blocks=blocks,
            verification_commands=verify_cmds,
            precheck_summary=_summary_for_approval(report),
        )
    except Exception as exc:
        _audit(None, device["id"], "precheck_failed", f"Precheck for {target_host} failed: {str(exc)[:MAX_ERROR]}")
        raise

    _audit(backup_result["job_id"], device["id"], "precheck_completed",
           f"Precheck for {target_host} ({adapter.label}): {len(blocks)} block(s), {command_count(blocks)} command(s), "
           f"semantic pre-validation {report['checks']['semantic_verification'].lower()}; pre-change backup "
           f"{backup_result['job_id']}; approval {approval['approval_id']} pending")
    _audit(backup_result["job_id"], device["id"], "change_created",
           f"Change {approval['approval_id']} created for {target_host}: "
           + ", ".join(block_label(b, i) + f" {len(b['commands'])} command(s)" for i, b in enumerate(blocks)))

    return {**base, "status": "pending_approval", "backup_required": True, "backup": backup_result,
            "approval": approval, "config_blocks": blocks, "ready_for_approval": True}


# ---- apply ---------------------------------------------------------------------------------
def _fail_before_device(approval_id, approval, claimed_by_api, exc):
    if claimed_by_api:
        mark_approval_failed(approval_id)
        _audit(approval["backup_job_id"], approval["device_id"], "apply_failed",
               f"Approved configuration apply rejected before device changes (approval_id={approval_id}): {exc}")


def _post_check(adapter, hostname, blocks, execution, verify_cmds):
    """Semantic verification of the applied blocks + operator verification commands."""
    applied = {b["index"] for b in execution["blocks"] if b["status"] == "applied"}
    post = {"read": None, "semantic": NOT_AVAILABLE, "blocks": [], "error": None}
    try:
        state = adapter.read_device_state(hostname, blocks)
        post["read"] = "ok"
        post["blocks"] = [{"index": e["index"], "found": e["found"], "commands": e["commands"]}
                          for e in verify_blocks(adapter.name, blocks, state["running_config"], state.get("vlan_ids"),
                                                 only=applied) if e["index"] in applied]
        post["semantic"] = semantic_status([c for b in post["blocks"] for c in b["commands"]])
    except Exception as exc:
        post["read"] = "failed"
        post["error"] = _sanitized("Post-check could not read the device", exc)

    outputs = []
    if verify_cmds:
        try:
            for item in adapter.run_show_commands(hostname, verify_cmds):
                outputs.append({"command": item["command"], "ok": not item["failed"],
                                "output": exe.truncate_output(item["output"])})
        except Exception as exc:
            outputs = [{"command": c, "ok": False, "output": _sanitized("Verification command not run", exc)}
                       for c in verify_cmds]
    return post, outputs


def run_apply(approval_id, claimed_by_api=False, expected_platform=None):
    approval = get_change_approval(approval_id)

    # The API claims approved -> applying atomically before enqueueing. A cancelled,
    # applied, failed or re-delivered change never reaches the device.
    expected_status = "applying" if claimed_by_api else "approved"
    if approval["status"] != expected_status:
        raise ValueError(f"Approval {approval_id} is {approval['status']}, expected {expected_status}")

    try:
        device = get_device_by_id(approval["device_id"])
        adapter = platform_for(device["platform"])
        if expected_platform and device["platform"] != expected_platform:
            raise ValueError(f"{device['hostname']} is not a {expected_platform} device")

        backup_check = verify_backup_for_device(device["id"], approval["backup_job_id"])
        if not backup_check["valid"]:
            raise ValueError(f"Backup verification failed: {backup_check['reason']}")

        # Structural re-check of the stored change (integrity, not command policy).
        blocks = normalize_blocks(stored_blocks(approval))
        verify_cmds = normalize_verification_commands(approval.get("verification_commands") or [])
    except Exception as exc:
        _fail_before_device(approval_id, approval, claimed_by_api, exc)
        raise

    hostname = device["hostname"]
    acq = coord.acquire_device_operation(device["id"], "apply", owner=f"change:{approval_id}",
                                         ttl_seconds=PHASE_BACKUP_SECONDS + PHASE_MARGIN_SECONDS, hostname=hostname)
    if not acq.acquired:
        reason = _busy_text(acq, hostname)
        returned = claimed_by_api and release_apply_claim(approval_id)
        _audit(approval["backup_job_id"], device["id"], "apply_deferred",
               f"Apply for approval {approval_id} not started: {reason}; nothing was sent"
               + ("; approval returned to approved" if returned else ""))
        raise ChangeError(f"Apply not started: {reason}. Nothing was sent"
                          + ("; the approval is back to approved - apply again shortly." if returned else "."))

    lease = acq.lease
    try:
        coord.set_change_in_progress(lease, approval_id, None, PHASE_BACKUP_SECONDS + PHASE_MARGIN_SECONDS)
        return _apply_locked(approval_id, approval, claimed_by_api, device, adapter, blocks, verify_cmds, lease)
    finally:
        coord.clear_change_in_progress(lease)
        lease.release()


def _pre_apply_backup(device, approval_id):
    """Fresh running-config backup taken under the change's own slot (no second lock)."""
    job_id = create_job(device_id=device["id"], job_type="config_backup", requested_by="pre-apply")
    try:
        result = perform_config_backup(device["hostname"], device["id"], device["platform"], job_id,
                                       source=f"pre_apply approval={approval_id}")
    except Exception as exc:
        reason = classify_backup_error(exc)
        mark_job_failed(job_id, reason)
        _audit(job_id, device["id"], "backup_failed",
               f"Pre-apply backup failed for {device['hostname']}: {reason} (approval_id={approval_id})")
        raise ChangeError(f"Pre-apply backup failed ({reason}); nothing was sent") from None
    return {"status": "success", "job_id": job_id, "backup_id": result["backup_id"], "checksum": result["checksum"]}


def _post_change_health(device, lease):
    """One lightweight health check while still owning the device (before releasing it)."""
    from health.checks import check_and_record

    try:
        row = check_and_record(device, lease=lease)
    except Exception as exc:
        return {"ok": False, "status": None, "error": _sanitized("Post-change health check failed", exc)}
    ok = bool(row.get("tcp_reachable") and row.get("ssh_reachable") and row.get("cli_reachable"))
    return {"ok": ok, "status": row.get("status"), "response_time_ms": row.get("response_time_ms"),
            "error": None if ok else (row.get("last_error") or "post-change health check failed")}


def _apply_locked(approval_id, approval, claimed_by_api, device, adapter, blocks, verify_cmds, lease):
    if not claimed_by_api:
        mark_approval_applying(approval_id)  # approved -> applying

    hostname = device["hostname"]
    started = _now()
    record = {"outcome": "applying", "execution": "applying", "semantic": None, "started_at": started,
              "finished_at": None, "job_id": None, "blocks": exe.initial_blocks(blocks, "applying"),
              "post_check": None, "verification": [], "error": None}

    # One execution per approval, even if the task is delivered twice.
    if not begin_execution(approval_id, record):
        raise ValueError(f"Approval {approval_id} already has an apply attempt; it will not be executed again")

    job_id = create_job(device_id=device["id"], job_type="config_apply", requested_by=approval.get("approved_by") or "system")
    record["job_id"] = job_id
    set_execution_result(approval_id, record)
    coord.set_change_in_progress(lease, approval_id, job_id, PHASE_BACKUP_SECONDS + PHASE_MARGIN_SECONDS)
    _audit(approval["backup_job_id"], device["id"], "apply_started",
           f"Approved configuration apply started for {hostname} (approval_id={approval_id}, job {job_id}): "
           f"{len(blocks)} block(s), {command_count(blocks)} command(s)")

    def phase(seconds):
        if not coord.refresh_change(lease, seconds):
            record["coordination_warning"] = "device slot expired during the change; other operations may have run"

    try:
        try:
            record["pre_apply_backup"] = _pre_apply_backup(device, approval_id)
        except ChangeError as exc:
            record["blocks"] = exe.initial_blocks(blocks, "not_attempted")
            record.update(execution="failed", outcome="failed", finished_at=_now(), error=str(exc))
            set_execution_result(approval_id, record)
            raise
        set_execution_result(approval_id, record)

        phase(_apply_phase_seconds(blocks))
        try:
            steps, run = adapter.apply_blocks(hostname, blocks)
            execution = exe.interpret(blocks, steps, run)
            if execution["outcome"] == "unknown" and run.get("progress") is None and run.get("returncode") not in (0, -1):
                execution["error"] = f"{ansible_failure_summary(run)}; the device may be partially configured - verify manually"
        except Exception as exc:  # runner failed before Ansible could send anything (inventory, Vault, ...)
            execution = {"blocks": exe.initial_blocks(blocks, "not_attempted"), "outcome": "failed", "returncode": None,
                         "error": _sanitized("Apply could not start; nothing was sent", exc)}

        record.update(blocks=execution["blocks"], execution=execution["outcome"], error=execution["error"])
        set_execution_result(approval_id, record)
        for entry in execution["blocks"]:
            block = blocks[entry["index"]]
            label = block_label(block, entry["index"])
            if entry["status"] == "applied":
                _audit(approval["backup_job_id"], device["id"], "apply_block_completed",
                       f"{label} applied on {hostname}: {len(block['commands'])} command(s) (approval_id={approval_id})")
            elif entry["status"] == "failed":
                _audit(approval["backup_job_id"], device["id"], "apply_block_failed",
                       f"{label} failed on {hostname} at "
                       f"{'command ' + str(entry['failed_command']) if entry['failed_command'] else entry.get('failed_step')}: "
                       f"{entry['error']} (approval_id={approval_id})")
            else:
                _audit(approval["backup_job_id"], device["id"], f"apply_block_{entry['status']}",
                       f"{label} {entry['status'].replace('_', ' ')} on {hostname} (approval_id={approval_id})")

        post, outputs = (None, [])
        if execution["outcome"] != "failed" or any(b["status"] == "applied" for b in execution["blocks"]):
            phase(PHASE_POSTCHECK_SECONDS + 120 * len(verify_cmds) + PHASE_MARGIN_SECONDS)
            post, outputs = _post_check(adapter, hostname, blocks, execution, verify_cmds)
        record.update(post_check=post, verification=outputs, semantic=post["semantic"] if post else None)

        # Immediate lightweight health verification before the device is released. A failure
        # is surfaced separately and never changes the change result; nothing is undone.
        phase(PHASE_HEALTH_SECONDS)
        record["post_change_health"] = _post_change_health(device, lease)
        if not record["post_change_health"]["ok"]:
            _audit(approval["backup_job_id"], device["id"], "post_change_health_warning",
                   f"Post-change health check for {hostname} failed (approval_id={approval_id}): "
                   f"{record['post_change_health']['error']}")

        outcome, message = _final_outcome(hostname, execution, post)
        record.update(outcome=outcome, finished_at=_now(), error=message if outcome != "applied" else None)
        set_execution_result(approval_id, record)

        if post is not None:
            _audit(approval["backup_job_id"], device["id"],
                   "postcheck_failed" if post["read"] != "ok" or post["semantic"] == "failed" else "postcheck_completed",
                   f"Post-check for {hostname} (approval_id={approval_id}): read {post['read']}, "
                   f"semantic verification {post['semantic'].replace('_', ' ')}"
                   + (f", {len(outputs)} verification command(s) run" if outputs else ""))

        if outcome == "applied":
            mark_approval_applied(approval_id)  # applying -> applied
            mark_job_success(job_id)
            _audit(approval["backup_job_id"], device["id"], "apply_completed",
                   f"Approved configuration apply completed for {hostname} (approval_id={approval_id}): "
                   f"{len(blocks)} block(s) applied; semantic verification {post['semantic'].replace('_', ' ')}")
            return {"approval_id": approval_id, "status": "applied", "platform": adapter.name, "target_host": hostname,
                    "approved_by": approval["approved_by"], "backup_job_id": approval["backup_job_id"], "job_id": job_id,
                    "config_blocks": blocks, "execution": record, "post_check": post, "write_skipped": False}

        raise ChangeError(message)

    except Exception as exc:
        message = str(exc) if isinstance(exc, ChangeError) else f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
        if record.get("outcome") in ("applying", None):
            record.update(outcome="failed", finished_at=_now(), error=message)
            set_execution_result(approval_id, record)
        mark_approval_failed(approval_id)  # applying -> failed
        mark_job_failed(job_id, exe.redact(message))
        _audit(approval["backup_job_id"], device["id"], "apply_failed",
               f"Approved configuration apply failed for {hostname} (approval_id={approval_id}): {message}")
        if isinstance(exc, ChangeError):
            raise
        raise ChangeError(exe.redact(message)) from None


def _final_outcome(hostname, execution, post):
    """(outcome, message): applied | partial_apply | failed | unknown."""
    if execution["outcome"] == "success":
        if post is None or post["read"] != "ok":
            return "failed", (f"Post-check could not read the device; the change may have been applied - verify manually"
                              f"{': ' + post['error'].split(': ', 1)[-1] if post and post['error'] else ''}")[:MAX_ERROR]
        if post["semantic"] == "failed":
            missing = [c["command"] for b in post["blocks"] for c in b["commands"] if c["status"] == NOT_PRESENT]
            return "failed", (f"Post-check failed for {hostname}: configuration is not present after apply "
                              f"(missing: {', '.join(missing)})")[:MAX_ERROR]
        return "applied", None
    if execution["outcome"] == "partial":
        return "partial_apply", (f"PARTIAL APPLY on {hostname}: {exe.summary(execution)}. {execution['error']}. "
                                 "Applied blocks were not rolled back.")[:MAX_ERROR]
    if execution["outcome"] == "failed":
        return "failed", (f"Apply failed on {hostname}; no block was applied. {execution['error']}")[:MAX_ERROR]
    return "unknown", (f"Apply result unknown on {hostname}: {execution['error']}")[:MAX_ERROR]
