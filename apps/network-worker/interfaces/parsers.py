"""
Read-only interface parsers for Dell OS6 and Dell OS10 -> one normalized model.

STATUS OF THESE PARSERS: written from the documented OS6 (N-series 6.x) and OS10 (10.5)
output layouts; NO real interface output from this lab has been captured yet. They read
tables by column NAME (interfaces/tables.py), not by position, and every field that is
not found stays None (never 0). Before scheduled polling is enabled, capture real output
with `python -m interfaces.validate_live capture <host>` and add it under
tests/fixtures/interfaces/ (test_interface_parsers.py then parses it).

Commands (all `show`; one SSH session per device collection, run in this order):

  dell_os6   show interfaces status            REQUIRED  name, description, link state,
                                                         speed, duplex, VLAN / mode
             show interfaces configuration     optional  admin state, MTU
             show interfaces counters          optional  octets, packets
             show interfaces counters errors   optional  FCS/CRC, receive/transmit errors,
                                                         discards
  dell_os10  show interface                    primary   admin + line-protocol state,
                                                         description, MTU, speed, octets,
                                                         packets, CRC, discards, time since
                                                         last state change, LAG members
             show interface status             optional  duplex, access/trunk mode, VLANs

A failing optional command only removes its fields (collection "partial"); a device with
no usable interface rows at all is "failed". Raw CLI text is never returned.
"""

import re

from interfaces.names import MONITORED_TYPES, canonical_name, interface_type
from interfaces.tables import first_value, key, parse_tables

OS6_STATUS = "show interfaces status"
OS6_CONFIGURATION = "show interfaces configuration"
OS6_COUNTERS = "show interfaces counters"
OS6_ERRORS = "show interfaces counters errors"
OS10_INTERFACE = "show interface"
OS10_STATUS = "show interface status"

INTERFACE_COMMANDS = {
    "dell_os6": (OS6_STATUS, OS6_CONFIGURATION, OS6_COUNTERS, OS6_ERRORS),
    "dell_os10": (OS10_INTERFACE, OS10_STATUS),
}
REQUIRED = {"dell_os6": (OS6_STATUS,), "dell_os10": ()}  # OS10: either command yields inventory

# Every collection command must be a plain `show` command (no pipes, no arguments that
# could reach configuration mode). Checked at import and again before sending.
SAFE_COMMAND = re.compile(r"^show [a-z0-9 \-]+$")
assert all(SAFE_COMMAND.match(c) for cmds in INTERFACE_COMMANDS.values() for c in cmds)

CLI_ERROR = re.compile(r"^\s*%\s*(error|invalid|incomplete|ambiguous)|invalid input|unrecognized command", re.I | re.M)

STATE_FIELDS = ("interface_name", "canonical_name", "description", "interface_type", "admin_status", "oper_status",
                "speed_bps", "duplex", "mode", "access_vlan", "native_vlan", "allowed_vlans", "port_channel", "mtu",
                "state_change_age_seconds")
COUNTER_FIELDS = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "crc_errors",
                  "input_discards", "output_discards")


BIGINT_MAX = 2**63 - 1


class InterfaceParseError(ValueError):
    pass


def _blank(name, canonical, platform):
    item = {field: None for field in STATE_FIELDS + COUNTER_FIELDS}
    item.update(interface_name=name, canonical_name=canonical, interface_type=interface_type(platform, canonical))
    return item


# ---- value helpers ----------------------------------------------------------------------------
_SPEED = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt])?(?:bps|b/s|b)?$", re.I)
_UNIT = {None: 1_000_000, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000, "t": 1_000_000_000_000}


def speed_bps(text):
    """'10000' (Mb/s), '10G', '1000M', '100 Mbps' -> bits/s; unknown / 0 / auto -> None."""
    if not text:
        return None
    match = _SPEED.match(text.strip().rstrip(",").replace(" ", ""))
    if not match:
        return None
    value = int(float(match.group(1)) * _UNIT[(match.group(2) or "").lower() or None])
    return value if value > 0 else None


def count(text):
    if text is None:
        return None
    cleaned = text.strip().replace(",", "")
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if value <= BIGINT_MAX else None  # beyond PostgreSQL bigint = misparse


def updown(text):
    value = (text or "").strip().lower()
    if value in ("up", "enable", "enabled", "connected"):
        return "up"
    if value in ("down", "disable", "disabled", "notconnect", "not connected", "administratively down",
                 "dormant", "lowerlayerdown", "notpresent", "not present", "detach", "detached"):
        return "down"
    return None


def duplex(text):
    value = (text or "").strip().lower()
    return value if value in ("full", "half") else None


def vlan_id(text):
    value = count(text)
    return value if value is not None and 1 <= value <= 4094 else None


def _usable(outputs, command):
    """Raw text of a command that ran cleanly, else (None, reason)."""
    text = outputs.get(command)
    if text is None:
        return None, "not run"
    if not text.strip():
        return None, "empty output"
    if CLI_ERROR.search(text):
        return None, "CLI error (unsupported command?)"
    return text, None


def _port_rows(text, platform, name_columns=("Port", "Interface", "Ch", "Name")):
    """{canonical: (display name, row)} for rows that look like monitored interfaces."""
    found = {}
    for table in parse_tables(text):
        for row in table["rows"]:
            name = first_value(row, *name_columns)
            canonical = canonical_name(platform, name)
            if not canonical or not re.search(r"\d", canonical):
                continue
            if interface_type(platform, canonical) not in MONITORED_TYPES:
                continue
            if canonical in found:
                found[canonical][1].update({k: v for k, v in row.items() if v})  # second table (e.g. Out*)
            else:
                found[canonical] = (name, dict(row))
    return found


def _sum(row, *names):
    values = [count(row.get(key(n))) for n in names]
    return sum(values) if values and all(v is not None for v in values) else None


# ---- Dell OS6 -----------------------------------------------------------------------------------
def _os6_mode(vlan_cell):
    value = (vlan_cell or "").strip().lower()
    if not value:
        return None, None
    if value.isdigit():
        return "access", vlan_id(value)
    if value.startswith("tr"):
        return "trunk", None
    if value.startswith("gen"):
        return "general", None
    return None, None


def parse_os6(outputs):
    problems, sources = [], {}
    text, reason = _usable(outputs, OS6_STATUS)
    sources[OS6_STATUS] = reason or "ok"
    if text is None:
        raise InterfaceParseError(f"'{OS6_STATUS}': {reason}")

    interfaces = {}
    for canonical, (name, row) in _port_rows(text, "dell_os6").items():
        item = _blank(name, canonical, "dell_os6")
        item["description"] = first_value(row, "Description", "Desc")
        item["oper_status"] = updown(first_value(row, "Link State", "Link", "State", "Link Status", "Status"))
        item["speed_bps"] = speed_bps(first_value(row, "Speed", "Oper Speed"))
        item["duplex"] = duplex(first_value(row, "Duplex", "Oper Duplex"))
        item["mode"], item["access_vlan"] = _os6_mode(first_value(row, "Vlan", "VLAN", "Mode"))
        interfaces[canonical] = item
    if not interfaces:
        raise InterfaceParseError(f"'{OS6_STATUS}' contained no interface rows")

    def merge(command, apply):
        text, reason = _usable(outputs, command)
        if text is None:
            sources[command] = reason
            problems.append(f"'{command}': {reason}")
            return
        rows = _port_rows(text, "dell_os6")
        if not rows:
            sources[command] = "no rows"
            problems.append(f"'{command}': no interface rows")
            return
        sources[command] = "ok"
        for canonical, (_, row) in rows.items():
            if canonical in interfaces:
                apply(interfaces[canonical], row)

    def configuration(item, row):
        item["admin_status"] = updown(first_value(row, "Admin State", "Admin", "Admin Status", "Admin Mode"))
        mtu = count(first_value(row, "MTU", "Max Frame"))
        item["mtu"] = mtu if mtu else None

    def counters(item, row):
        item["rx_bytes"] = count(first_value(row, "InOctets", "InTotalOctets", "Rx Octets", "RxOctets"))
        item["tx_bytes"] = count(first_value(row, "OutOctets", "OutTotalOctets", "Tx Octets", "TxOctets"))
        item["rx_packets"] = count(first_value(row, "InTotalPkts", "InPkts")) \
            if first_value(row, "InTotalPkts", "InPkts") else _sum(row, "InUcastPkts", "InMcastPkts", "InBcastPkts")
        item["tx_packets"] = count(first_value(row, "OutTotalPkts", "OutPkts")) \
            if first_value(row, "OutTotalPkts", "OutPkts") else _sum(row, "OutUcastPkts", "OutMcastPkts", "OutBcastPkts")

    def errors(item, row):
        item["crc_errors"] = count(first_value(row, "FCS-Err", "FCS Errors", "CRC-Err", "CRC Errors", "CRC"))
        item["rx_errors"] = count(first_value(row, "Rcv-Err", "Rx-Err", "In-Err", "InErrors", "Input Errors"))
        item["tx_errors"] = count(first_value(row, "Xmit-Err", "Tx-Err", "Out-Err", "OutErrors", "Output Errors"))
        item["input_discards"] = count(first_value(row, "InDiscard", "In-Discard", "InDiscards", "Rx-Discard"))
        item["output_discards"] = count(first_value(row, "OutDiscard", "Out-Discard", "OutDiscards", "Tx-Discard"))

    merge(OS6_CONFIGURATION, configuration)
    merge(OS6_COUNTERS, counters)
    merge(OS6_ERRORS, errors)
    return {"interfaces": list(interfaces.values()), "problems": problems, "sources": sources}


# ---- Dell OS10 ----------------------------------------------------------------------------------
_OS10_HEADER = re.compile(
    r"^(?P<name>(?:ethernet|port-channel|management|mgmt|vlan|loopback|null|virtual-network|tunnel)\s?[\d/:]+)"
    r"\s+is\s+(?P<admin>up|down|administratively down|not present|testing)"
    r"(?:,\s*line protocol is\s+(?P<oper>[a-z\-]+))?",
    re.I,
)
_DURATION_PART = re.compile(r"(\d+)\s+(week|day|hour|minute|second)s?", re.I)
_HMS = re.compile(r"(\d+):(\d{2}):(\d{2})")


def age_seconds(text):
    """'1 weeks 2 days 03:02:50' / '05:12:33' -> seconds; anything else -> None."""
    if not text:
        return None
    units = {"week": 604800, "day": 86400, "hour": 3600, "minute": 60, "second": 1}
    total, matched = 0, False
    for number, unit in _DURATION_PART.findall(text):
        total += int(number) * units[unit.lower()]
        matched = True
    hms = _HMS.search(text)
    if hms:
        hours, minutes, seconds = map(int, hms.groups())
        if minutes > 59 or seconds > 59:
            return None
        total += hours * 3600 + minutes * 60 + seconds
        matched = True
    return total if matched else None


def _os10_blocks(text):
    blocks, current = [], None
    for line in text.splitlines():
        match = _OS10_HEADER.match(line.strip())
        if match and not line[:1].isspace():
            current = {"match": match, "lines": []}
            blocks.append(current)
        elif current is not None:
            current["lines"].append(line)
    return blocks


def _stat(section, pattern):
    match = re.search(pattern, section, re.I | re.M)
    return int(match.group(1)) if match else None


def _os10_detail(text):
    interfaces, members = {}, {}
    for block in _os10_blocks(text):
        name = block["match"].group("name")
        canonical = canonical_name("dell_os10", name)
        if interface_type("dell_os10", canonical) not in MONITORED_TYPES:
            continue
        item = _blank(name, canonical, "dell_os10")
        body = "\n".join(block["lines"])
        item["admin_status"] = updown(block["match"].group("admin"))
        item["oper_status"] = updown(block["match"].group("oper")) if block["match"].group("oper") else None

        description = re.search(r"^[ \t]*Description:[ \t]*(.*?)[ \t]*$", body, re.M)
        item["description"] = (description.group(1) or None) if description else None
        mtu = re.search(r"\bMTU\s+(\d+)\s+bytes", body)
        item["mtu"] = int(mtu.group(1)) if mtu and int(mtu.group(1)) > 0 else None
        speed = re.search(r"\bLineSpeed\s+([^\s,]+)", body)
        item["speed_bps"] = speed_bps(speed.group(1)) if speed else None
        changed = re.search(r"Time since last interface status change:[ \t]*(\S.*?)[ \t]*$", body, re.M)
        item["state_change_age_seconds"] = age_seconds(changed.group(1)) if changed else None
        lag = re.search(r"Members in this channel:[ \t]*(\S.*?)[ \t]*$", body, re.M)
        if lag:
            for member in re.split(r"[,\s]+", lag.group(1)):
                member_name = re.sub(r"\(.*?\)$", "", member).strip()
                if member_name:
                    members[canonical_name("dell_os10", member_name)] = name

        input_part, _, output_part = body.partition("Output statistics")
        _, _, input_part = input_part.partition("Input statistics")
        if input_part:
            item["rx_packets"] = _stat(input_part, r"^\s*(\d+)\s+packets,\s+\d+\s+octets")
            item["rx_bytes"] = _stat(input_part, r"^\s*\d+\s+packets,\s+(\d+)\s+octets")
            item["crc_errors"] = _stat(input_part, r"\b(\d+)\s+CRC\b")
            item["input_discards"] = _stat(input_part, r"\b(\d+)\s+discarded\b")
            item["rx_errors"] = _stat(input_part, r"\b(\d+)\s+(?:input\s+)?errors\b")
        if output_part:
            item["tx_packets"] = _stat(output_part, r"^\s*(\d+)\s+packets,\s+\d+\s+octets")
            item["tx_bytes"] = _stat(output_part, r"^\s*\d+\s+packets,\s+(\d+)\s+octets")
            item["output_discards"] = _stat(output_part, r"\b(\d+)\s+discarded\b")
            item["tx_errors"] = _stat(output_part, r"\b(\d+)\s+(?:output\s+)?errors\b")
        interfaces[canonical] = item
    for member, channel in members.items():
        if member in interfaces:
            interfaces[member]["port_channel"] = channel
    return interfaces


def _os10_mode(row):
    mode = (first_value(row, "Mode") or "").strip().upper()
    vlan = vlan_id(first_value(row, "Vlan", "VLAN"))
    tagged = first_value(row, "Tagged-Vlans", "Tagged Vlans", "TaggedVlans")
    tagged = None if tagged in (None, "-", "") else tagged
    if mode in ("A", "ACCESS"):
        return {"mode": "access", "access_vlan": vlan}
    if mode in ("T", "TRUNK"):
        return {"mode": "trunk", "native_vlan": vlan, "allowed_vlans": tagged}
    if mode in ("L3", "ROUTED"):
        return {"mode": "routed"}
    return {}


def parse_os10(outputs):
    problems, sources = [], {}
    interfaces = {}

    detail_text, reason = _usable(outputs, OS10_INTERFACE)
    if detail_text is not None:
        interfaces = _os10_detail(detail_text)
        sources[OS10_INTERFACE] = "ok" if interfaces else "no interface blocks"
        if not interfaces:
            problems.append(f"'{OS10_INTERFACE}': no interface blocks")
    else:
        sources[OS10_INTERFACE] = reason
        problems.append(f"'{OS10_INTERFACE}': {reason}")

    status_text, reason = _usable(outputs, OS10_STATUS)
    rows = _port_rows(status_text, "dell_os10") if status_text is not None else {}
    if status_text is None or not rows:
        reason = reason or "no interface rows"
        sources[OS10_STATUS] = reason
        problems.append(f"'{OS10_STATUS}': {reason}")
    else:
        sources[OS10_STATUS] = "ok"
    for canonical, (name, row) in rows.items():
        item = interfaces.get(canonical)
        if item is None:
            if detail_text is not None and interfaces:
                continue  # detail is authoritative for which interfaces exist
            item = interfaces[canonical] = _blank(name, canonical, "dell_os10")
        item["description"] = item["description"] or first_value(row, "Description")
        item["oper_status"] = item["oper_status"] or updown(first_value(row, "Status"))
        item["speed_bps"] = item["speed_bps"] or speed_bps(first_value(row, "Speed"))
        item["duplex"] = duplex(first_value(row, "Duplex"))
        item.update(_os10_mode(row))

    if not interfaces:
        raise InterfaceParseError("; ".join(problems) or "no interface data")
    return {"interfaces": list(interfaces.values()), "problems": problems, "sources": sources}


PARSERS = {"dell_os6": parse_os6, "dell_os10": parse_os10}


def parse_interfaces(platform, outputs):
    """Normalized interfaces for one device. Raises InterfaceParseError when nothing is usable."""
    parser = PARSERS.get(platform)
    if parser is None:
        raise InterfaceParseError(f"Interface parsing not implemented for {platform}")
    result = parser(outputs)
    result["status"] = "success" if not result["problems"] else "partial"
    return result
