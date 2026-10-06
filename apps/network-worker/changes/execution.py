"""
Turn an apply run (ansible/blocks_runner.py) into exact per-block execution status, and
sanitize device output before it is stored or shown.

Block status:  pending | applying | applied | failed | not_attempted | unknown
Outcome:       success  every step of every block was accepted
               partial  some blocks were applied, then a command was rejected
               failed   a command was rejected before any block completed
               unknown  the run ended without a progress report (e.g. timeout): what
                        reached the device cannot be determined -> verify manually

Network CLI is not transactional: nothing here claims a rollback. Blocks applied before a
failure stay applied and are reported as such.
"""

import re

MAX_ERROR = 400
MAX_OUTPUT = 20000

# Values after these keywords are replaced in stored/displayed output and audit text.
_SENSITIVE = re.compile(
    r"(?i)\b(password|secret|key|community|passphrase|auth-key|md5|psk|token)(\s+(?:\d{1,2}\s+)?)(\S+)")
_DEVICE_ERROR = re.compile(r"%\s?(?:Error|Invalid|Incomplete|Ambiguous)[^\\\r\n'\"]*", re.IGNORECASE)


def redact(text):
    return _SENSITIVE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text or "")


def device_error(text):
    """Short, sanitized device/Ansible error (never the echoed command line)."""
    text = text or ""
    match = _DEVICE_ERROR.search(text)
    if match:
        return redact(match.group(0).strip())[:MAX_ERROR]
    for marker in ("timed out", "timeout", "unable to connect", "Authentication", "not reachable"):
        if marker.lower() in text.lower():
            return f"connection problem ({marker.lower()})"
    return "command rejected by the device"


def truncate_output(text, limit=MAX_OUTPUT):
    text = redact(text or "")
    return text if len(text) <= limit else text[:limit] + f"\n... (truncated, {len(text) - limit} more characters)"


def initial_blocks(blocks, status="pending"):
    return [{"index": i, "parent": b["parent"], "command_count": len(b["commands"]), "status": status,
             "failed_command": None, "error": None} for i, b in enumerate(blocks)]


def interpret(blocks, steps, run):
    """Per-block status from the step-level progress report."""
    result = {"blocks": initial_blocks(blocks, "unknown"), "outcome": "unknown", "error": None,
              "returncode": run.get("returncode")}
    progress = run.get("progress")

    if progress is None:
        result["error"] = ("Apply ended without a progress report "
                           f"({device_error(run.get('stderr') or run.get('stdout'))}); "
                           "the device may be partially configured - verify manually")
        return result

    applied, failed = set(progress["applied"]), progress["failed"]
    by_block = {}
    for step in steps:
        by_block.setdefault(step["block"], []).append(step)

    for index, entry in enumerate(result["blocks"]):
        block_steps = by_block.get(index, [])
        if all(s["i"] in applied for s in block_steps):
            entry["status"] = "applied"
        elif failed is not None and any(s["i"] == failed for s in block_steps):
            entry["status"] = "failed"
            step = next(s for s in block_steps if s["i"] == failed)
            commands = [s for s in block_steps if s["kind"] == "command"]
            entry["failed_command"] = (commands.index(step) + 1) if step in commands else None
            entry["failed_step"] = step["kind"]
            entry["error"] = device_error(progress["error"])
        else:
            entry["status"] = "not_attempted"

    statuses = [b["status"] for b in result["blocks"]]
    if failed is None and run.get("returncode") == 0 and all(s == "applied" for s in statuses):
        result["outcome"] = "success"
    elif failed is not None:
        result["outcome"] = "partial" if "applied" in statuses else "failed"
        bad = next(b for b in result["blocks"] if b["status"] == "failed")
        where = (f"command {bad['failed_command']}" if bad["failed_command"]
                 else f"{bad.get('failed_step', 'step')}")
        result["error"] = f"Block {bad['index'] + 1} {where} rejected: {bad['error']}"
    else:
        result["outcome"] = "unknown"
        result["blocks"] = [{**b, "status": "unknown"} if b["status"] != "applied" else b for b in result["blocks"]]
        result["error"] = "Apply finished without a clear result; verify the device manually"
    return result


def summary(execution):
    parts = [f"block {b['index'] + 1} {b['status'].replace('_', ' ')}" for b in execution["blocks"]]
    return ", ".join(parts)
