"""
Canonical ordered configuration-block model for guarded changes (Dell OS6 and Dell OS10).

A change is an ordered list of blocks; each block has an optional parent (configuration
context) and ordered child commands:

    [{"order": 0, "parent": "interface ethernet1/1/18", "commands": ["description APP", "shutdown"]},
     {"order": 1, "parent": None, "commands": ["ip routing"]}]          # global block

Order is the list order (the server assigns "order"); commands are never reordered or
de-duplicated. Only whitespace at either end of a line is trimmed.

There is NO configuration command policy here: any CLI the device accepts may be sent.
What is validated is input integrity only:
  * shape and types (list of blocks, string commands, optional string/None parent)
  * resource limits (blocks, commands per block, total commands, line lengths)
  * single-line printable ASCII (no NUL / control characters that could inject a second
    command or corrupt the terminal session)
  * configuration-mode commands (configure / end / exit / do) are rejected because the
    execution adapter owns mode transitions (`do` would run an exec-mode command in the
    middle of a configuration session); this is about session structure, not command type.

Future work (not supported in this release): nested parents inside a block
(parent -> child-parent -> command). A command that itself opens a context (e.g.
`router ospf 1` in a global block) is sent as raw CLI; following commands in the same
block then run in whatever context the device entered. Each block ends with `end`, so
the next block always starts from a clean configuration session.
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


class BlockError(ValueError):
    """Structural problem(s) with a change request. `problems` lists every one found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("Invalid change request: " + "; ".join(self.problems))


def _check_line(value, where, max_length, problems):
    if not isinstance(value, str):
        problems.append(f"{where} must be a string")
        return None
    text = value.strip()
    if not text:
        problems.append(f"{where} is empty")
        return None
    if len(text) > max_length:
        problems.append(f"{where} is longer than {max_length} characters")
        return None
    if not PRINTABLE.match(text):
        problems.append(f"{where} must be single-line printable text (no control characters)")
        return None
    if MODE_COMMAND.match(text):
        problems.append(f"{where} ({text!r}): configure / end / exit / do are handled by the platform")
        return None
    return text


def normalize_blocks(blocks):
    """Validate and canonicalize a block list. Raises BlockError listing every problem."""
    problems = []
    if not isinstance(blocks, list):
        raise BlockError(["blocks must be a list"])
    if not blocks:
        raise BlockError(["at least one configuration block is required"])
    if len(blocks) > MAX_BLOCKS:
        raise BlockError([f"at most {MAX_BLOCKS} blocks are allowed"])

    canonical, total = [], 0
    for index, block in enumerate(blocks):
        label = f"block {index + 1}"
        if not isinstance(block, dict):
            problems.append(f"{label} must be an object")
            continue
        unknown = set(block) - {"parent", "commands", "order"}
        if unknown:
            problems.append(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")

        parent = block.get("parent")
        if parent is not None:
            if isinstance(parent, str) and not parent.strip():
                parent = None
            else:
                parent = _check_line(parent, f"{label} parent", MAX_PARENT_LENGTH, problems)
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

        clean = [_check_line(c, f"{label} command {n + 1}", MAX_COMMAND_LENGTH, problems)
                 for n, c in enumerate(commands)]
        if None not in clean:
            canonical.append({"order": len(canonical), "parent": parent, "commands": clean})

    if total > MAX_TOTAL_COMMANDS:
        problems.append(f"at most {MAX_TOTAL_COMMANDS} commands are allowed in one change")
    if problems:
        raise BlockError(problems)
    return canonical


def normalize_verification_commands(commands):
    """Optional operator-supplied READ-ONLY commands run after apply (`show ...` only)."""
    if commands is None:
        return []
    if not isinstance(commands, list):
        raise BlockError(["verification_commands must be a list"])
    if len(commands) > MAX_VERIFICATION_COMMANDS:
        raise BlockError([f"at most {MAX_VERIFICATION_COMMANDS} verification commands are allowed"])
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
        raise BlockError(problems)
    return clean


def blocks_from_legacy(config_lines, config_parents=None):
    """
    Single-parent request/record -> one block. Additional (nested) legacy parents become the
    block's leading commands: the device enters them in the same order as before.
    """
    parents = [p for p in (config_parents or []) if isinstance(p, str) and p.strip()]
    lines = list(config_lines or [])
    if not parents:
        return [{"order": 0, "parent": None, "commands": lines}]
    return [{"order": 0, "parent": parents[0], "commands": parents[1:] + lines}]


def stored_blocks(record):
    """Blocks of an approval record: config_blocks, or the legacy single-parent adapter."""
    if record.get("config_blocks"):
        return record["config_blocks"]
    return blocks_from_legacy(record.get("config_lines"), record.get("config_parents"))


def command_count(blocks):
    return sum(len(b["commands"]) for b in blocks)


def flatten(blocks):
    """Flat CLI line list (parent, then children) kept in the legacy config_lines column."""
    lines = []
    for block in blocks:
        if block["parent"]:
            lines.append(block["parent"])
        lines.extend(block["commands"])
    return lines


def block_label(block, index=None):
    number = (block.get("order", 0) if index is None else index) + 1
    return f"Block {number}" + (f" ({block['parent']})" if block["parent"] else " (global)")


def render_cli(blocks):
    """Read-only CLI preview in exact execution order."""
    parts = []
    for index, block in enumerate(blocks):
        header = f"! Block {index + 1}" + ("" if block["parent"] else " - Global")
        body = [block["parent"]] + [f" {c}" for c in block["commands"]] if block["parent"] else list(block["commands"])
        parts.append("\n".join([header] + body))
    return "\n\n".join(parts)


def apply_steps(blocks, configure_command):
    """
    Ordered device steps: per block `configure` -> parent -> children -> `end`.
    Each step carries its block index so execution progress maps back to blocks exactly.
    """
    steps = []
    for index, block in enumerate(blocks):
        sequence = [("configure", configure_command)]
        if block["parent"]:
            sequence.append(("parent", block["parent"]))
        sequence += [("command", c) for c in block["commands"]]
        sequence.append(("end", "end"))
        for kind, command in sequence:
            steps.append({"i": len(steps), "block": index, "kind": kind, "command": command})
    return steps
