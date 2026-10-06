"""
Ordered configuration-block model for change requests (Dell OS6 + Dell OS10).

Mirrors the worker's changes/blocks.py (same limits, same environment variables): the API
rejects malformed requests early with 422; the worker re-validates before precheck and
again before apply. This is input-integrity validation only. There is NO configuration
command policy: any CLI may be submitted and the device is the syntax authority.

Rejected: non-list / empty block lists, blocks without commands, non-string values,
control characters / NUL / non-ASCII (one entry must be exactly one CLI line),
configure / end / exit / do (the platform owns mode transitions), unknown fields, and
anything above the resource limits. Verification commands must be read-only `show ...`.
"""

import os
import re


def _limit(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


MAX_BLOCKS = _limit("CHANGE_MAX_BLOCKS", 50)
MAX_COMMANDS_PER_BLOCK = _limit("CHANGE_MAX_COMMANDS_PER_BLOCK", 100)
MAX_TOTAL_COMMANDS = _limit("CHANGE_MAX_TOTAL_COMMANDS", 500)
MAX_COMMAND_LENGTH = _limit("CHANGE_MAX_COMMAND_LENGTH", 1000)
MAX_PARENT_LENGTH = _limit("CHANGE_MAX_PARENT_LENGTH", 1000)
MAX_VERIFICATION_COMMANDS = _limit("CHANGE_MAX_VERIFICATION_COMMANDS", 20)

PRINTABLE = re.compile(r"^[\x20-\x7e]+$")
MODE_COMMAND = re.compile(r"^(configure|config|conf\s+t|end|exit|do)(\s|$)", re.IGNORECASE)
READ_ONLY_COMMAND = re.compile(r"^show\s+\S", re.IGNORECASE)


def _line(value, where, max_length, problems):
    if not isinstance(value, str):
        problems.append(f"{where} must be a string")
        return None
    text = value.strip()
    if not text:
        problems.append(f"{where} is empty")
    elif len(text) > max_length:
        problems.append(f"{where} is longer than {max_length} characters")
    elif not PRINTABLE.match(text):
        problems.append(f"{where} must be single-line printable text (no control characters)")
    elif MODE_COMMAND.match(text):
        problems.append(f"{where} ({text!r}): configure / end / exit / do are handled by the platform")
    else:
        return text
    return None


def validate_blocks(blocks):
    """Canonical [{order, parent, commands}] or ValueError listing every problem."""
    if not isinstance(blocks, list):
        raise ValueError("blocks must be a list")
    if not blocks:
        raise ValueError("at least one configuration block is required")
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"at most {MAX_BLOCKS} blocks are allowed")

    problems, canonical, total = [], [], 0
    for index, block in enumerate(blocks):
        label = f"block {index + 1}"
        if not isinstance(block, dict):
            problems.append(f"{label} must be an object")
            continue
        unknown = set(block) - {"parent", "commands", "order"}
        if unknown:
            problems.append(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")
        parent = block.get("parent")
        if isinstance(parent, str) and not parent.strip():
            parent = None
        elif parent is not None:
            parent = _line(parent, f"{label} parent", MAX_PARENT_LENGTH, problems)
            if parent is None:
                continue
        commands = block.get("commands")
        if not isinstance(commands, list):
            problems.append(f"{label} commands must be a list")
            continue
        if not commands:
            problems.append(f"{label} must contain at least one command")
            continue
        if len(commands) > MAX_COMMANDS_PER_BLOCK:
            problems.append(f"{label} has more than {MAX_COMMANDS_PER_BLOCK} commands")
            continue
        total += len(commands)
        clean = [_line(c, f"{label} command {n + 1}", MAX_COMMAND_LENGTH, problems) for n, c in enumerate(commands)]
        if None not in clean:
            canonical.append({"order": len(canonical), "parent": parent, "commands": clean})

    if total > MAX_TOTAL_COMMANDS:
        problems.append(f"at most {MAX_TOTAL_COMMANDS} commands are allowed in one change")
    if problems:
        raise ValueError("; ".join(problems))
    return canonical


def validate_verification_commands(commands):
    if commands is None:
        return []
    if not isinstance(commands, list):
        raise ValueError("verification_commands must be a list")
    if len(commands) > MAX_VERIFICATION_COMMANDS:
        raise ValueError(f"at most {MAX_VERIFICATION_COMMANDS} verification commands are allowed")
    problems, clean = [], []
    for n, command in enumerate(commands):
        where = f"verification command {n + 1}"
        if not isinstance(command, str) or not command.strip():
            problems.append(f"{where} must be a non-empty string")
            continue
        text = command.strip()
        if len(text) > MAX_COMMAND_LENGTH or not PRINTABLE.match(text):
            problems.append(f"{where} must be single-line printable text of at most {MAX_COMMAND_LENGTH} characters")
        elif not READ_ONLY_COMMAND.match(text):
            problems.append(f"{where} ({text!r}) must be a read-only `show ...` command")
        else:
            clean.append(text)
    if problems:
        raise ValueError("; ".join(problems))
    return clean


def blocks_from_legacy(config_lines, config_parents=None):
    """Historical single-parent change -> one block (extra nested parents lead the commands)."""
    parents = [p for p in (config_parents or []) if isinstance(p, str) and p.strip()]
    lines = list(config_lines or [])
    if not parents:
        return [{"order": 0, "parent": None, "commands": lines}]
    return [{"order": 0, "parent": parents[0], "commands": parents[1:] + lines}]


def render_cli(blocks):
    parts = []
    for index, block in enumerate(blocks):
        header = f"! Block {index + 1}" + ("" if block.get("parent") else " - Global")
        body = ([block["parent"]] + [f" {c}" for c in block["commands"]]) if block.get("parent") else list(block["commands"])
        parts.append("\n".join([header] + body))
    return "\n\n".join(parts)
