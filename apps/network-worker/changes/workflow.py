"""
Shared guarded-change workflow for every supported platform (Dell OS6, Dell OS10).

Precheck (read-only):
    device enabled -> platform adapter -> policy/normalize -> read device state ONCE
    (running config; OS10 L2 changes also LLDP in the same session)
    -> plan: OS6 verifies the lines; OS10 classifies the interface, checks VLANs and
       current mode, and derives the exact device commands
    -> [rejected by safety: done, nothing stored] [no change: done]
    -> pre-change backup from that same snapshot (no backup => no approval => no write)
    -> pending approval (OS10: the planned device commands are what is approved)

Apply (the API has already claimed approved -> applying atomically):
    status check -> backup belongs to this device and succeeded -> stored commands
    re-validated -> [OS10: re-read; safety and preconditions re-checked against the
    device as it is now; commands already in effect are skipped; nothing left => applied
    without writing] -> Ansible write -> post-check reads the device again -> applied
    Any failure after the claim -> failed (+ audit apply_failed). Ansible's return code
    alone never marks a change applied: only a passing post-check does.

The original OS6 behaviour is preserved; OS6-specific task names in worker.py call this
module with expected_platform="dell_os6".
"""

import logging
import re

from changes.os10 import VerificationError
from changes.os10_safety import SafetyRejection
from changes.platforms import platform_for
from db.approvals import (
    create_change_approval,
    get_change_approval,
    mark_approval_applied,
    mark_approval_applying,
    mark_approval_failed,
    verify_backup_for_device,
)
from db.devices import get_device_by_hostname, get_device_by_id
from db.jobs import create_audit_event
from tasks.config_backup import classify_backup_error

logger = logging.getLogger("network_worker.changes")

MAX_ERROR = 600


class ChangeError(RuntimeError):
    """A sanitized, user-facing failure."""


def _audit(job_id, device_id, event_type, message):
    try:
        create_audit_event(job_id=job_id, device_id=device_id, event_type=event_type, message=message[:2000])
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
    item = re.search(r"(?:failed|fatal): \[[^\]]*\] \(item=([^)]*)\)", text)
    suffix = f" (command: {item.group(1)})" if item else ""
    return f"Ansible apply failed (rc={result.get('returncode')}): {reason}{suffix}"[:MAX_ERROR]


def _sanitized(stage, exc):
    if isinstance(exc, ChangeError):
        return str(exc)
    if isinstance(exc, (VerificationError, SafetyRejection)):  # our own messages: no device output or secrets
        return f"{stage}: {exc}"[:MAX_ERROR]
    return f"{stage}: {classify_backup_error(exc)}"


def run_precheck(target_host, config_lines, config_parents=None, *, backup, expected_platform=None):
    device = get_device_by_hostname(target_host)  # enabled devices only
    try:
        adapter = platform_for(device["platform"])
        if expected_platform and device["platform"] != expected_platform:
            raise ValueError(f"{target_host} is {device['platform']}, not {expected_platform}")

        lines, parents = adapter.prepare(config_lines, config_parents)

        try:
            state = adapter.read_device_state(target_host, lines)
        except Exception as exc:
            raise ChangeError(_sanitized("Could not read the running configuration", exc)) from None
        snapshot = state["running_config"]

        dry_run = adapter.plan(target_host, device, lines, parents, state)

        if dry_run.get("rejected"):
            reasons = dry_run["rejection_reasons"]
            _audit(None, device["id"], "precheck_failed",
                   f"Precheck for {target_host} ({adapter.label}) rejected by safety policy: {' '.join(reasons)}")
            return {
                "target_host": target_host,
                "platform": adapter.name,
                "status": "rejected",
                "rejection_reasons": reasons,
                "dry_run": dry_run,
                "backup_required": False,
                "ready_for_approval": False,
            }

        if not dry_run["would_change"]:
            _audit(None, device["id"], "precheck_completed",
                   f"Precheck for {target_host} ({adapter.label}): requested state already present; no change required")
            return {
                "target_host": target_host,
                "platform": adapter.name,
                "status": "no_change_required",
                "dry_run": dry_run,
                "backup_required": False,
                "ready_for_approval": False,
            }

        backup_result = backup(target_host, config_snapshot=snapshot)
        if backup_result.get("status") != "success":
            raise RuntimeError(f"Backup failed for {target_host}")

        # OS10: the planned device commands are approved (and later re-validated) verbatim.
        approved_lines = dry_run.get("device_commands") or lines
        approval = create_change_approval(
            device_id=device["id"],
            backup_job_id=backup_result["job_id"],
            config_lines=approved_lines,
            config_parents=parents,
            requested_by="system",
        )
    except Exception as exc:
        _audit(None, device["id"], "precheck_failed", f"Precheck for {target_host} failed: {str(exc)[:MAX_ERROR]}")
        raise

    _audit(backup_result["job_id"], device["id"], "precheck_completed",
           f"Precheck for {target_host} ({adapter.label}): {len(dry_run['proposed_changes'])} change(s) proposed; "
           f"pre-change backup {backup_result['job_id']}; approval {approval['approval_id']} pending")

    return {
        "target_host": target_host,
        "platform": adapter.name,
        "status": "pending_approval",
        "dry_run": dry_run,
        "backup_required": True,
        "backup": backup_result,
        "approval": approval,
        "config_lines": approved_lines,
        "config_parents": parents,
        "ready_for_approval": True,
    }


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

        if not approval["config_lines"]:
            raise ValueError("Approved configuration is empty")

        # Re-validate the stored change (defence in depth for OS10's allow-list).
        config_lines, config_parents = adapter.validate_approved(approval["config_lines"], approval.get("config_parents"))

    except Exception as exc:
        if claimed_by_api:
            mark_approval_failed(approval_id)
            _audit(approval["backup_job_id"], approval["device_id"], "apply_failed",
                   f"Approved configuration apply rejected before device changes (approval_id={approval_id}): {exc}")
        raise

    if not claimed_by_api:
        mark_approval_applying(approval_id)  # approved -> applying

    hostname = device["hostname"]
    _audit(approval["backup_job_id"], device["id"], "apply_started",
           f"Approved configuration apply started for {hostname} (approval_id={approval_id})")

    try:
        to_send = config_lines
        if adapter.pre_apply_verify:
            try:
                before = adapter.pre_apply(device, config_lines, config_parents)
            except SafetyRejection as exc:
                raise ChangeError(f"Pre-apply safety check failed; nothing was sent: {exc}"[:MAX_ERROR]) from None
            except Exception as exc:
                raise ChangeError(_sanitized("Pre-apply read failed; nothing was sent", exc)) from None
            if not before["would_change"]:
                mark_approval_applied(approval_id)
                _audit(approval["backup_job_id"], device["id"], "apply_completed",
                       f"Approved configuration already present on {hostname}; no commands sent "
                       f"(approval_id={approval_id}, verified by {before['verification_method']})")
                return _apply_result(approval_id, approval, hostname, adapter, config_lines, config_parents,
                                     None, before, write_skipped=True)
            # Only what is still needed, in the approved order.
            to_send = before["proposed_changes"]

        result = adapter.apply(hostname, to_send, config_parents)
        if result["returncode"] != 0:
            raise ChangeError(ansible_failure_summary(result))

        try:
            post_check = adapter.verify(hostname, config_lines, config_parents)
        except Exception as exc:
            raise ChangeError(_sanitized("Post-check could not read the device; the change may have been applied "
                                         "- verify manually", exc)) from None

        if post_check["would_change"]:
            raise ChangeError(f"Post-check failed for {hostname}: configuration is not present after apply "
                              f"(missing: {', '.join(post_check['proposed_changes'])})"[:MAX_ERROR])

        mark_approval_applied(approval_id)  # applying -> applied
        _audit(approval["backup_job_id"], device["id"], "apply_completed",
               f"Approved configuration apply completed for {hostname} (approval_id={approval_id}); "
               f"post-check confirmed {len(post_check['already_present'])} line(s) via {post_check['verification_method']}")
        return _apply_result(approval_id, approval, hostname, adapter, config_lines, config_parents, result, post_check,
                             sent_lines=to_send)

    except Exception as exc:
        mark_approval_failed(approval_id)  # applying -> failed
        message = str(exc) if isinstance(exc, ChangeError) else f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
        _audit(approval["backup_job_id"], device["id"], "apply_failed",
               f"Approved configuration apply failed for {hostname} (approval_id={approval_id}): {message}")
        raise


def _apply_result(approval_id, approval, hostname, adapter, config_lines, config_parents, ansible_result, post_check,
                  write_skipped=False, sent_lines=None):
    return {
        "approval_id": approval_id,
        "status": "applied",
        "platform": adapter.name,
        "target_host": hostname,
        "approved_by": approval["approved_by"],
        "backup_job_id": approval["backup_job_id"],
        "config_lines": config_lines,
        "config_parents": config_parents,
        "ansible_result": ansible_result,
        "post_check": post_check,
        "write_skipped": write_skipped,
        "sent_lines": [] if write_skipped else (sent_lines if sent_lines is not None else config_lines),
    }
