"""
CPU / memory / uptime parsers, one per platform, written ONLY from real captured output:

    tests/fixtures/os6_real_show_process_cpu_Kenda-HARO-IDF-A.txt
    tests/fixtures/os6_real_show_memory_cpu_Kenda-HARO-IDF-A.txt
    tests/fixtures/os6_real_show_system_Kenda-HARO-IDF-A.txt
    tests/fixtures/os10_real_show_processes_node_id_1_Kenda-Core-1.txt
    tests/fixtures/os10_real_show_uptime_Kenda-Core-1.txt

Parser contract, per platform:
    parse(outputs: dict[command, raw_text]) -> {
        "cpu_percent": float | None,       # 0-100
        "memory_percent": float | None,    # 0-100
        "memory_used_mb": float | None,
        "memory_total_mb": float | None,
        "uptime_seconds": int | None,
        "problems": [str, ...],            # why a field is None (sanitized, short)
    }

Each data source is independent: a missing or malformed CPU line only makes CPU None
(and the sample `partial`); it never discards memory, and vice versa. Values are never
clamped or defaulted: anything outside 0-100 is rejected for that field. Uptime never
affects the success/partial/failed outcome.
"""

import re


class TelemetryParseError(ValueError):
    pass


FIELDS = ("cpu_percent", "memory_percent", "memory_used_mb", "memory_total_mb", "uptime_seconds")

OS6_CPU, OS6_MEMORY, OS6_SYSTEM = "show process cpu", "show memory cpu", "show system"
OS10_PROCESSES, OS10_UPTIME = "show processes node-id 1", "show uptime"

# platform -> ordered read-only commands (one SSH session per collection). `show version`
# is not collected: nothing here needs it.
TELEMETRY_COMMANDS = {
    "dell_os6": (OS6_CPU, OS6_MEMORY, OS6_SYSTEM),
    "dell_os10": (OS10_PROCESSES, OS10_UPTIME),
}

CLI_ERROR = re.compile(r"^\s*%\s*(error|invalid|incomplete|ambiguous)|invalid input|unrecognized command", re.I | re.M)


class SourceError(Exception):
    """One data source (CPU, memory or uptime) is unusable; the others still count."""


def _kb_to_mb(kb):
    return round(kb / 1024, 1)


def _percent(value, what):
    if not 0 <= value <= 100:
        raise SourceError(f"{what} out of range: {value}")
    return round(value, 2)


def _text(outputs, command):
    text = outputs.get(command)
    if text is None or not text.strip():
        raise SourceError(f"'{command}' returned no output")
    if CLI_ERROR.search(text):
        raise SourceError(f"'{command}' returned a CLI error")
    return text


def _hms_seconds(days, hours, minutes, seconds):
    if int(minutes) > 59 or int(seconds) > 59:
        raise SourceError("uptime minutes/seconds out of range")
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _collect(result, field_sets):
    """Run each (fields, fn) source; a SourceError leaves those fields None with a reason."""
    for fields, label, fn in field_sets:
        try:
            values = fn()
        except SourceError as exc:
            result["problems"].append(f"{label} unavailable: {exc}")
            continue
        for field, value in zip(fields, values):
            result[field] = value


def _empty():
    return {**{field: None for field in FIELDS}, "problems": []}


# ---- Dell OS6 (N-series, 6.8.x) ------------------------------------------------------
#   show process cpu  ->  "Total CPU Utilization            2.85%    2.98%    3.10%"
#                          (5 s, 60 s, 300 s); also "free 1758740" / "alloc 2235804" KBytes
#   show memory cpu   ->  "Total Memory........ 3994544 KBytes"
#                          "Available Memory Space........ 1758740 KBytes"
#   show system       ->  "System Up Time: 201 days, 04h:13m:32s"

_OS6_TOTAL_CPU_LINE = re.compile(r"^\s*Total CPU Utilization\b(.*)$", re.M)
_OS6_CPU_VALUES = re.compile(r"^\s*(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%\s*$")
_OS6_TOTAL_MEMORY = re.compile(r"^\s*Total Memory\.*\s*(\S+)\s+KBytes\s*$", re.M)
_OS6_AVAILABLE_MEMORY = re.compile(r"^\s*Available Memory Space\.*\s*(\S+)\s+KBytes\s*$", re.M)
_OS6_REPORT_FREE = re.compile(r"^\s*free\s+(\S+)\s*$", re.M)
_OS6_REPORT_ALLOC = re.compile(r"^\s*alloc\s+(\S+)\s*$", re.M)
_OS6_UPTIME_LINE = re.compile(r"^\s*System Up Time:(.*)$", re.M)
_OS6_UPTIME_VALUE = re.compile(r"^\s*(?:(\d+)\s+days?,\s*)?(\d+)h:(\d+)m:(\d+)s\s*$")


def _int(token, what):
    if not token.isdigit():
        raise SourceError(f"malformed {what}: {token!r}")
    return int(token)


def _os6_cpu(outputs):
    text = _text(outputs, OS6_CPU)
    line = _OS6_TOTAL_CPU_LINE.search(text)
    if not line:
        raise SourceError("'Total CPU Utilization' line not found")
    values = _OS6_CPU_VALUES.match(line.group(1))
    if not values:
        raise SourceError("malformed 'Total CPU Utilization' line")
    # The 60-second total (middle column); individual process rows are never summed.
    return (_percent(float(values.group(2)), "CPU"),)


def _memory_from(total_kb, available_kb):
    if total_kb <= 0 or available_kb > total_kb:
        raise SourceError(f"inconsistent memory values (total {total_kb} KB, available {available_kb} KB)")
    used_kb = total_kb - available_kb
    return (_percent(used_kb / total_kb * 100, "memory"), _kb_to_mb(used_kb), _kb_to_mb(total_kb))


def _os6_memory(outputs):
    try:
        text = _text(outputs, OS6_MEMORY)
        total = _OS6_TOTAL_MEMORY.search(text)
        available = _OS6_AVAILABLE_MEMORY.search(text)
        if not total:
            raise SourceError("'Total Memory' line not found")
        if not available:
            raise SourceError("'Available Memory Space' line not found")
        return _memory_from(_int(total.group(1), "Total Memory"), _int(available.group(1), "Available Memory Space"))
    except SourceError as primary:
        # Same switch, same numbers: show process cpu reports free/alloc KBytes
        # (free + alloc == Total Memory, free == Available Memory Space in the capture).
        try:
            text = _text(outputs, OS6_CPU)
            free = _OS6_REPORT_FREE.search(text)
            alloc = _OS6_REPORT_ALLOC.search(text)
            if not (free and alloc):
                raise SourceError("no memory report")
            free_kb, alloc_kb = _int(free.group(1), "free"), _int(alloc.group(1), "alloc")
            return _memory_from(free_kb + alloc_kb, free_kb)
        except SourceError:
            raise primary from None


def _os6_uptime(outputs):
    text = _text(outputs, OS6_SYSTEM)
    line = _OS6_UPTIME_LINE.search(text)
    if not line:
        raise SourceError("'System Up Time' line not found")
    value = _OS6_UPTIME_VALUE.match(line.group(1))
    if not value:
        raise SourceError("malformed 'System Up Time' line")
    return (_hms_seconds(*value.groups()),)


def parse_os6(outputs):
    result = _empty()
    _collect(result, (
        (("cpu_percent",), "CPU", lambda: _os6_cpu(outputs)),
        (("memory_percent", "memory_used_mb", "memory_total_mb"), "Memory", lambda: _os6_memory(outputs)),
        (("uptime_seconds",), "Uptime", lambda: _os6_uptime(outputs)),
    ))
    return result


# ---- Dell OS10 (10.5.x) ---------------------------------------------------------------
#   show processes node-id 1  ->  Linux `top` header:
#     "%Cpu(s):  8.6 us,  8.6 sy,  0.0 ni, 82.9 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st"
#     "KiB Mem :  4012568 total,   729204 free,  2473812 used,   809552 buff/cache"
#   show uptime               ->  "3 days 03:30:36"

_OS10_CPU_LINE = re.compile(r"^\s*%Cpu\(s\):(.*)$", re.M)
_OS10_MEM_LINE = re.compile(r"^\s*KiB Mem\s*:(.*)$", re.M)
_OS10_TOP_FIELD = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+([a-z/]+)\s*$")
_OS10_UPTIME = re.compile(r"^\s*(?:(\d+)\s+days?\s+)?(\d{1,2}):(\d{2}):(\d{2})\s*$")


def _top_fields(line, what):
    fields = {}
    for part in line.split(","):
        match = _OS10_TOP_FIELD.match(part)
        if not match:
            raise SourceError(f"malformed {what} line")
        name = match.group(2)
        if name in fields:
            raise SourceError(f"duplicate '{name}' in {what} line")
        fields[name] = float(match.group(1))
    return fields


def _os10_cpu(outputs):
    text = _text(outputs, OS10_PROCESSES)
    line = _OS10_CPU_LINE.search(text)
    if not line:
        raise SourceError("'%Cpu(s)' line not found")
    fields = _top_fields(line.group(1), "%Cpu(s)")
    if "id" not in fields:
        raise SourceError("idle ('id') not found in %Cpu(s) line")
    idle = _percent(fields["id"], "CPU idle")
    # Busy = 100 - idle; per-process %CPU rows are never summed.
    return (_percent(round(100 - idle, 2), "CPU"),)


def _os10_memory(outputs):
    text = _text(outputs, OS10_PROCESSES)
    line = _OS10_MEM_LINE.search(text)
    if not line:
        raise SourceError("'KiB Mem' line not found")
    fields = _top_fields(line.group(1), "KiB Mem")
    if "total" not in fields or "used" not in fields:
        raise SourceError("'total'/'used' not found in KiB Mem line")
    total_kb, used_kb = fields["total"], fields["used"]
    if total_kb <= 0 or used_kb > total_kb:
        raise SourceError(f"inconsistent memory values (total {total_kb:.0f} KiB, used {used_kb:.0f} KiB)")
    # V1 metric: Linux "used" / total (buff/cache is not counted as used).
    return (_percent(used_kb / total_kb * 100, "memory"), _kb_to_mb(used_kb), _kb_to_mb(total_kb))


def _os10_uptime(outputs):
    text = _text(outputs, OS10_UPTIME).strip()
    value = _OS10_UPTIME.match(text)
    if not value:
        raise SourceError("malformed 'show uptime' output")
    return (_hms_seconds(*value.groups()),)


def parse_os10(outputs):
    result = _empty()
    _collect(result, (
        (("cpu_percent",), "CPU", lambda: _os10_cpu(outputs)),
        (("memory_percent", "memory_used_mb", "memory_total_mb"), "Memory", lambda: _os10_memory(outputs)),
        (("uptime_seconds",), "Uptime", lambda: _os10_uptime(outputs)),
    ))
    return result


PARSERS = {
    "dell_os6": parse_os6,
    "dell_os10": parse_os10,
}


def parse_metrics(platform, outputs):
    parser = PARSERS.get(platform)

    if parser is None:
        raise TelemetryParseError(f"No validated telemetry parser for {platform}")

    metrics = parser(outputs)
    missing = [field for field in FIELDS if field not in metrics]
    if missing:
        raise TelemetryParseError(f"Parser for {platform} omitted fields: {missing}")

    # Backstop only; the parsers already reject out-of-range values per field.
    for field in ("cpu_percent", "memory_percent"):
        value = metrics[field]
        if value is not None and not 0 <= value <= 100:
            raise TelemetryParseError(f"{field} out of range: {value}")

    metrics.setdefault("problems", [])
    return metrics
