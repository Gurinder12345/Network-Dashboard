"""
LLDP command mapping and output parsers. Pure functions: no network, DB or Vault access,
so they can be unit-tested against fixtures.

Parsing strategy (no guessed column widths):
  * Column boundaries come from the output itself: the dashed underline row when the
    platform prints one per column (OS6), otherwise the start positions of the header
    labels (OS10).
  * Each data row is sliced on those boundaries. If a value runs across a boundary
    (overflowing a column), the row falls back to splitting on runs of 2+ spaces, and is
    skipped with a warning if that is ambiguous too. Nothing is guessed.
  * Missing values become None. The remote interface is taken from the advertised Port ID
    only when that Port ID is an interface name (not a MAC or bare number). Neither
    summary command prints a management IP or capabilities, so those stay None.
"""

import re

PROTOCOL = "lldp"

# The only place commands are chosen. Keyed by platform, never by hostname.
LLDP_COMMANDS = {
    "dell_os10": "show lldp neighbors",
    "dell_os6": "show lldp remote-device all",
}

_ERROR_MARKERS = ("% invalid", "% error", "invalid input", "% incomplete", "lldp is not enabled", "lldp is disabled")
_DASH_ROW = re.compile(r"^\s*-{2,}(\s+-{2,})*\s*$")
_EMPTY_VALUES = {"", "-", "--", "n/a", "not advertised"}
_MAC = re.compile(r"^([0-9a-f]{2}[:\-.]?){5}[0-9a-f]{2}$|^([0-9a-f]{4}\.){2}[0-9a-f]{4}$", re.IGNORECASE)
_INTERFACE = re.compile(r"^[A-Za-z][A-Za-z\-]*\s?\d+(/\d+)*(:\d+)?(\.\d+)?$")

NEIGHBOR_FIELDS = (
    "local_interface",
    "remote_system_name",
    "remote_interface",
    "remote_management_ip",
    "remote_chassis_id",
    "remote_port_id",
    "capabilities",
    "protocol",
)


class LldpParseError(ValueError):
    """Output could not be understood; the caller must treat discovery as failed."""


def lldp_command_for(platform):
    try:
        return LLDP_COMMANDS[platform]
    except KeyError:
        raise LldpParseError(f"No LLDP command mapped for platform {platform!r}") from None


def _clean(value):
    if value is None:
        return None
    value = value.strip()
    return None if value.lower() in _EMPTY_VALUES else value


def _remote_interface_from_port_id(port_id):
    """Only an interface-name Port ID is an interface; MAC / numeric Port IDs are not."""
    if not port_id or _MAC.match(port_id) or port_id.isdigit():
        return None
    return port_id if _INTERFACE.match(port_id) else None


def _classify_column(label):
    text = " ".join(label.lower().split())
    if "chassis" in text:
        return "remote_chassis_id"
    if "port" in text and ("rem" in text or "id" in text) and "loc" not in text:
        return "remote_port_id"
    if "name" in text:
        return "remote_system_name"
    if "remid" in text or text == "rem id" or text == "index":
        return "remote_index"
    if "interface" in text or "loc" in text or text.startswith("local"):
        return "local_interface"
    return None


def _check_for_cli_error(output):
    if output is None or not output.strip():
        raise LldpParseError("Empty LLDP output")
    lowered = output.lower()
    for marker in _ERROR_MARKERS:
        if marker in lowered:
            raise LldpParseError(f"Device returned an error for the LLDP command ({marker.strip('% ')})")


def _crosses_boundary(line, start):
    return 0 < start < len(line) and line[start - 1] != " " and line[start] != " "


def _misaligned(line, start, end):
    """In a left-aligned column, content that does not begin at the column start means a
    neighbouring value overflowed into it (e.g. a long name pushing into the Port ID)."""
    if start >= len(line):
        return False
    segment = line[start:end] if end else line[start:]
    return bool(segment.strip()) and line[start] == " "


def _parse_rows(lines, columns, warnings, left_aligned=False):
    """
    columns: list of (field_name, start, end|None). Returns list of dicts.
    left_aligned=True (OS10): every value must start exactly at its column start;
    otherwise the row is treated like a boundary crossing (fallback, then warning).
    """
    rows = []
    names = [name for name, _, _ in columns]

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            break  # end of table
        if _DASH_ROW.match(line) or line.strip().endswith(("#", ">")):
            continue

        values = None
        suspect = any(_crosses_boundary(line, start) for _, start, _ in columns[1:])
        if left_aligned:
            suspect = suspect or any(_misaligned(line, start, end) for _, start, end in columns[1:])

        if not suspect:
            values = [line[start:end].strip() if end else line[start:].strip() for _, start, end in columns]
        else:
            tokens = re.split(r"\s{2,}", line.strip())
            if len(tokens) == len(columns):
                values = tokens

        if values is None:
            warnings.append(f"Unparseable row skipped: {line.strip()[:120]}")
            continue

        row = dict(zip(names, values))

        # Wrapped continuation line (no local interface): append to the previous row.
        if not row.get("local_interface"):
            if rows:
                for key, value in row.items():
                    if value and key != "local_interface":
                        rows[-1][key] = (rows[-1].get(key) or "") + value
            else:
                warnings.append(f"Row without local interface skipped: {line.strip()[:120]}")
            continue

        rows.append(row)

    return rows


def _to_neighbors(rows):
    neighbors = []
    for row in rows:
        port_id = _clean(row.get("remote_port_id"))
        neighbors.append(
            {
                "local_interface": _clean(row.get("local_interface")),
                "remote_system_name": _clean(row.get("remote_system_name")),
                "remote_interface": _remote_interface_from_port_id(port_id),
                "remote_management_ip": None,  # not printed by these summary commands
                "remote_chassis_id": _clean(row.get("remote_chassis_id")),
                "remote_port_id": port_id,
                "capabilities": None,  # not printed by these summary commands
                "protocol": PROTOCOL,
            }
        )
    return neighbors


def _require_columns(columns, source):
    fields = {name for name, _, _ in columns}
    if "local_interface" not in fields:
        raise LldpParseError(f"{source}: no local interface column in LLDP header")
    if not fields & {"remote_chassis_id", "remote_port_id", "remote_system_name"}:
        raise LldpParseError(f"{source}: no remote identity columns in LLDP header")


def parse_os6_lldp_remote_device_all(output):
    """
    Dell OS6 `show lldp remote-device all`. Column spans come from the dashed underline
    row; header labels (possibly split over two lines, e.g. "Local" / "Interface") are
    read from the lines directly above it within each span.
    Returns (neighbors, warnings).
    """
    _check_for_cli_error(output)
    lines = output.splitlines()
    warnings = []

    dash_index = next((i for i, line in enumerate(lines) if _DASH_ROW.match(line) and line.count("-") > 10), None)
    if dash_index is None:
        raise LldpParseError("OS6: LLDP table underline not found (unexpected or truncated output)")

    dash_line = lines[dash_index]
    spans = [(m.start(), m.end()) for m in re.finditer(r"-+", dash_line)]
    if len(spans) < 2:
        raise LldpParseError("OS6: LLDP table has too few columns")

    header_lines = [l for l in lines[max(0, dash_index - 2):dash_index] if l.strip()]
    columns = []
    for index, (start, end) in enumerate(spans):
        label = " ".join(h[start:(spans[index + 1][0] if index + 1 < len(spans) else None)].strip() for h in header_lines)
        field = _classify_column(label) or f"column_{index}"
        columns.append((field, start, spans[index + 1][0] if index + 1 < len(spans) else None))

    _require_columns(columns, "OS6")
    rows = _parse_rows(lines[dash_index + 1:], columns, warnings)
    return _to_neighbors(rows), warnings


def parse_os10_lldp_neighbors(output):
    """
    Dell OS10 `show lldp neighbors`. Column starts come from the header label positions
    (labels are separated by 2+ spaces); a full-width dashed line follows the header.
    Returns (neighbors, warnings).
    """
    _check_for_cli_error(output)
    lines = output.splitlines()
    warnings = []

    header_index = next(
        (i for i, line in enumerate(lines) if "loc" in line.lower() and "rem" in line.lower() and "port" in line.lower()),
        None,
    )
    if header_index is None:
        raise LldpParseError("OS10: LLDP neighbors header not found (unexpected or truncated output)")

    header = lines[header_index]
    labels = [(m.group(0), m.start()) for m in re.finditer(r"\S+(?: \S+)*", header)]
    if len(labels) < 2:
        raise LldpParseError("OS10: LLDP header has too few columns")

    columns = []
    for index, (label, start) in enumerate(labels):
        end = labels[index + 1][1] if index + 1 < len(labels) else None
        columns.append((_classify_column(label) or f"column_{index}", start, end))

    _require_columns(columns, "OS10")

    body_start = header_index + 1
    while body_start < len(lines) and _DASH_ROW.match(lines[body_start]):
        body_start += 1

    columns = _snap_to_data_gutters(columns, _table_body(lines[body_start:]))
    rows = _parse_rows(lines[body_start:], columns, warnings, left_aligned=True)
    return _to_neighbors(rows), warnings


OS10_MAX_BOUNDARY_SHIFT = 3


def _table_body(lines):
    body = []
    for line in lines:
        if not line.strip():
            break
        if _DASH_ROW.match(line) or line.strip().endswith(("#", ">")):
            continue
        body.append(line.rstrip("\n"))
    return body


def _snap_to_data_gutters(columns, body):
    """
    OS10 left-aligns some columns up to a few characters before their header label
    (real Kenda-Core-1 output: "Rem Chassis Id" label at column 72, values at 71).
    Move each column start left -- at most OS10_MAX_BOUNDARY_SHIFT characters -- to the
    right-most position where EVERY data row has whitespace immediately before it, so the
    boundary sits in a gutter shared by all rows. If no such position exists within the
    window, the header position is kept and the row-level checks decide (warnings).
    """
    if not body:
        return columns

    def is_gutter(position):
        return all(position <= len(row) and (position == 0 or row[position - 1] == " ") for row in body)

    snapped = [columns[0]]
    for index, (name, start, end) in enumerate(columns[1:], start=1):
        previous_start = snapped[index - 1][1]
        new_start = start
        for candidate in range(start, max(previous_start + 1, start - OS10_MAX_BOUNDARY_SHIFT) - 1, -1):
            if is_gutter(candidate):
                new_start = candidate
                break
        snapped[index - 1] = (snapped[index - 1][0], snapped[index - 1][1], new_start)
        snapped.append((name, new_start, end))

    return snapped


def count_table_rows(output):
    """
    Independent cross-check: number of non-empty data lines after the first dashed
    underline row, up to the first blank line. Used to detect wrapped/missed rows.
    Returns None when no underline row exists.
    """
    lines = (output or "").splitlines()
    start = next((i for i, line in enumerate(lines) if _DASH_ROW.match(line) and line.count("-") > 10), None)
    if start is None:
        return None
    count = 0
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if _DASH_ROW.match(line) or line.strip().endswith(("#", ">")):
            continue
        count += 1
    return count


PARSERS = {
    "dell_os10": parse_os10_lldp_neighbors,
    "dell_os6": parse_os6_lldp_remote_device_all,
}


def parse_lldp_output(platform, output):
    try:
        parser = PARSERS[platform]
    except KeyError:
        raise LldpParseError(f"No LLDP parser for platform {platform!r}") from None
    return parser(output)
