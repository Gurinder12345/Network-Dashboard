"""
Simulated Dell OS10 SSH CLI for tests (see switch.py). NOT a real switch.

  exec   : `Kenda-Core-1# `  terminal ..., show running-configuration [interface X],
           show lldp neighbors, show vlan, other `show ...`, configure terminal, exit
  config : `Kenda-Core-1(config)# `  interface ethernetX/Y/Z | interface vlanN, context
           openers (router ..., ip access-list ..., ...), any global command
  if     : `Kenda-Core-1(conf-if-eth1/1/5)# `  description, no description, shutdown,
           no shutdown, switchport mode access|trunk, switchport access vlan <id>,
           switchport trunk allowed vlan <list> (ADDS, as documented for OS10),
           no switchport trunk allowed vlan <list>; anything else is stored as-is
Running-configuration and `show lldp neighbors` layouts follow the documented OS10 format
and the real LLDP capture (tests/fixtures/os10_real_show_lldp_neighbors.txt).
"""

import re

from fakes.switch import TEST_PASSWORD, TEST_USERNAME, FakeSwitch, apply_generic  # noqa: F401
from changes.vlans import VlanListError, format_vlan_list, parse_vlan_list

MANAGEMENT_IP = "192.0.2.10"


def port(description=None, admin="up", mode="access", access_vlan=1, allowed=(), extra=()):
    """One interface. mode None = no switchport lines (e.g. a port-channel member)."""
    return {"description": description, "admin": admin, "mode": mode, "access_vlan": access_vlan,
            "allowed": set(allowed), "extra": list(extra)}


def default_interfaces():
    return {
        "ethernet1/1/5": port(extra=["flowcontrol receive on"]),
        "ethernet1/1/18": port(access_vlan=20),
        "ethernet1/1/19": port(mode="trunk", access_vlan=1, allowed=(10, 20)),
        "ethernet1/1/25": port(mode="trunk", access_vlan=1, allowed=(10, 20, 30)),
        "ethernet1/1/30:2": port(description="UPLINK-DO-NOT-TOUCH", mode=None, extra=["channel-group 10 mode active"]),
        "ethernet1/1/31": port(admin="down"),
    }


DEFAULT_LLDP = {"ethernet1/1/25": ("kenda-core-02", "ethernet1/1/25", "e8:b5:d0:7a:5c:a3"),
                "ethernet1/1/30:2": ("Kenda-HQ-Array01-B", "00:e0:ed:96:d9:67", "00:e0:ed:96:d9:67")}


class FakeOS10(FakeSwitch):
    configure_commands = ("configure terminal", "configure")
    error_text = "% Error: Command rejected by simulated device."

    def __init__(self, hostname="Kenda-Core-1", interfaces=None, reject=(), ignore_writes=False,
                 vlans=(1, 10, 20, 30, 40), lldp=None):
        self.vlans = set(vlans)
        self.lldp = dict(DEFAULT_LLDP if lldp is None else lldp)
        super().__init__(hostname, interfaces or default_interfaces(), reject, ignore_writes)

    # ---- rendering ------------------------------------------------------------------
    def interface_text(self, name):
        intf = self.interfaces[name]
        lines = [f"interface {name}"]
        if intf["description"]:
            lines.append(f" description {intf['description']}")
        lines.append(" shutdown" if intf["admin"] == "down" else " no shutdown")
        if intf["mode"] == "trunk":
            lines.append(" switchport mode trunk")
        if intf["mode"] in ("access", "trunk"):
            lines.append(f" switchport access vlan {intf['access_vlan']}")
        if intf["mode"] == "trunk" and intf["allowed"]:
            lines.append(f" switchport trunk allowed vlan {format_vlan_list(intf['allowed'])}")
        lines += [f" {line}" for line in intf["extra"]]
        lines.append("!")
        return lines

    def running_config(self):
        lines = ["! Version 10.5.4.0", "! Last configuration change at Oct  06 12:00:00 2026", "!",
                 f"hostname {self.hostname}"]
        lines += self.global_lines
        lines.append("!")
        for vlan in sorted(self.vlans):
            lines += [f"interface vlan{vlan}", " no shutdown"]
            lines += [f" {l}" for l in self.contexts.get(f"interface vlan{vlan}", [])]
            lines.append("!")
        lines += ["interface mgmt1/1/1", " no shutdown", " no ip address dhcp", f" ip address {MANAGEMENT_IP}/24",
                  " ipv6 address autoconfig", "!"]
        for name in self.interfaces:
            lines += self.interface_text(name)
        for parent, children in self.contexts.items():
            if parent.startswith("interface vlan"):
                continue
            lines += [parent] + [f" {c}" for c in children] + ["!"]
        lines += ["!", "end"]
        return "\r\n".join(lines)

    def lldp_neighbors(self):
        lines = ["Loc PortID          Rem Host Name        Rem Port Id                    Rem Chassis Id", "-" * 86]
        for local, (host, remote_port, chassis) in self.lldp.items():
            lines.append(f"{local:<20}{host:<21}{remote_port:<30}{chassis:<24}")
        return "\r\n".join(lines)

    def show(self, command):
        if command == "show running-configuration":
            return self.running_config()
        if command.startswith("show running-configuration interface "):
            name = command.split()[-1]
            return "\r\n".join(self.interface_text(name)) if name in self.interfaces else "% Error: Interface not found."
        if command == "show lldp neighbors":
            return self.lldp_neighbors()
        if command == "show vlan":
            return "\r\n".join(["Codes: * - Default VLAN", "    NUM    Status    Description"]
                               + [f"    {v:<6} Active" for v in sorted(self.vlans)])
        if command.startswith("show "):
            return f"simulated output of {command}"
        return None

    # ---- CLI hooks -------------------------------------------------------------------
    def interface_name(self, text):
        name = "".join(text.lower().split())
        if name in self.interfaces:
            return name
        match = re.fullmatch(r"vlan(\d{1,4})", name)
        if match:
            if not self.ignore_writes:
                self.vlans.add(int(match.group(1)))
            self.contexts.setdefault(f"interface vlan{match.group(1)}", [])
            return f"vlan:{match.group(1)}"
        return None

    def _handle(self, mode, line):
        # interface vlanN behaves as a generic context
        if isinstance(mode, tuple) and mode[0] == "if" and mode[1].startswith("vlan:"):
            cmd = " ".join(line.strip().split())
            if cmd in ("exit", "end") or cmd in self.reject:
                return super()._handle(mode, line)
            self.log.append(cmd)
            self._store(self.contexts[f"interface vlan{mode[1][5:]}"], cmd)
            return mode, ""
        return super()._handle(mode, line)

    def context_prompt(self, mode):
        if mode == "config":
            return f"{self.hostname}(config)# "
        kind, name = mode
        if kind == "if":
            short = name[len("ethernet"):] if name.startswith("ethernet") else name.replace(":", "")
            return f"{self.hostname}(conf-if-{'eth' if name.startswith('ethernet') else 'vl-'}{short})# "
        slug = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")[:30]
        return f"{self.hostname}(conf-{slug})# "

    def interface_command(self, intf, cmd):
        low = cmd.lower()
        tokens = low.split()
        store = not self.ignore_writes
        if low.startswith("description "):
            if store:
                intf["description"] = cmd[len("description "):]
            return ""
        if low == "no description":
            if store:
                intf["description"] = None
            return ""
        if low in ("shutdown", "no shutdown"):
            if store:
                intf["admin"] = "down" if low == "shutdown" else "up"
            return ""
        if tokens[:2] == ["switchport", "mode"] and len(tokens) == 3 and tokens[2] in ("access", "trunk"):
            if store:
                intf["mode"] = tokens[2]
                if tokens[2] == "access":
                    intf["allowed"] = set()
            return ""
        try:
            if tokens[:3] == ["switchport", "access", "vlan"] and len(tokens) == 4:
                vlan = int(tokens[3])
                if vlan not in self.vlans:
                    return f"% Error: VLAN {vlan} does not exist."
                if store:
                    intf["access_vlan"] = vlan
                return ""
            if tokens[:4] == ["switchport", "trunk", "allowed", "vlan"] and len(tokens) == 5:
                vlans = set(parse_vlan_list(tokens[4], device_limits=True))
                if intf["mode"] != "trunk":
                    return "% Error: Interface is not in trunk mode."
                if vlans - self.vlans:
                    return "% Error: VLAN does not exist."
                if store:
                    intf["allowed"] |= vlans
                return ""
            if tokens[:5] == ["no", "switchport", "trunk", "allowed", "vlan"] and len(tokens) == 6:
                vlans = set(parse_vlan_list(tokens[5], device_limits=True))
                if store:
                    intf["allowed"] -= vlans
                return ""
        except (ValueError, VlanListError):
            return "% Error: Invalid VLAN."
        if store:
            apply_generic(intf["extra"], cmd)
        return ""
