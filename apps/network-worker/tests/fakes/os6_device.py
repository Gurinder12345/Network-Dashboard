"""
Simulated Dell OS6 (N-series 6.x) SSH CLI for tests (see switch.py). NOT a real switch.

  exec   : `Kenda-HARO-IDF-A# `  terminal ..., enable, disable, show running-config,
           show vlan, other `show ...`, configure, exit
  config : `Kenda-HARO-IDF-A(config)# `  interface <Tw|Gi|Te>x/y/z, vlan N (context),
           no vlan N, context openers, any global command
  if     : `Kenda-HARO-IDF-A(config-if-Tw1/0/3)# `  description, no description, shutdown,
           no shutdown; anything else is stored as-is
Running-config uses the OS6 layout: `configure` header, unindented lines, blocks closed
by `exit`, quoted descriptions. Errors use the OS6 form `% Invalid input detected at '^'
marker.` (matched by the dellemc.os6 terminal plugin).
"""

import re

from fakes.switch import TEST_PASSWORD, TEST_USERNAME, FakeSwitch, apply_generic  # noqa: F401


def os6_port(description=None, admin="up", extra=()):
    return {"description": description, "admin": admin, "extra": list(extra)}


def default_os6_interfaces():
    return {
        "Tw1/0/3": os6_port(description="PRECHECK-TEST", extra=["switchport access vlan 10"]),
        "Tw1/0/4": os6_port(),
        "Tw1/0/12": os6_port(admin="down"),
        "Te1/0/1": os6_port(description="UPLINK", extra=["switchport mode trunk"]),
    }


class FakeOS6(FakeSwitch):
    configure_commands = ("configure",)
    error_text = "% Invalid input detected at '^' marker."

    def __init__(self, hostname="Kenda-HARO-IDF-A", interfaces=None, reject=(), ignore_writes=False, vlans=(1, 10, 20)):
        self.vlans = set(vlans)
        super().__init__(hostname, interfaces or default_os6_interfaces(), reject, ignore_writes)

    def running_config(self):
        lines = ["!Current Configuration:", '!System Description "Dell Networking N3224PX-ON, 6.8.1.0"',
                 "!System Software Version 6.8.1.0", "!", "configure"]
        plain = sorted(v for v in self.vlans if v != 1)
        if plain:
            lines += ["vlan " + ",".join(map(str, plain)), "exit"]
        lines.append(f'hostname "{self.hostname}"')
        lines += self.global_lines
        for parent, children in self.contexts.items():
            lines += [parent] + children + ["exit"]
        for name, intf in self.interfaces.items():
            body = []
            if intf["description"]:
                body.append(f'description "{intf["description"]}"')
            if intf["admin"] == "down":
                body.append("shutdown")
            body += intf["extra"]
            if body:  # OS6 lists only interfaces with non-default configuration
                lines += [f"interface {name}"] + body + ["exit"]
        lines.append("exit")
        return "\r\n".join(lines)

    def show(self, command):
        if command == "show running-config":
            return self.running_config()
        if command == "show vlan":
            rows = ["VLAN   Name                             Ports          Type",
                    "-----  ---------------                  -------------  --------------"]
            rows += [f"{v:<6} {'default' if v == 1 else f'VLAN{v:04d}':<32} {'':<14} {'Default' if v == 1 else 'Static'}"
                     for v in sorted(self.vlans)]
            return "\r\n".join(rows)
        if command.startswith("show "):
            return f"simulated output of {command}"
        return None

    def interface_name(self, text):
        name = "".join(text.split())
        for known in self.interfaces:
            if known.lower() == name.lower():
                return known
        return None

    def open_context(self, command):
        match = re.fullmatch(r"vlan (\d{1,4})", command.lower())
        if match and not self.ignore_writes:
            self.vlans.add(int(match.group(1)))
        return super().open_context(command)

    def _handle(self, mode, line):
        cmd = " ".join(line.strip().split())
        match = re.fullmatch(r"no vlan (\d{1,4})", cmd.lower())
        if mode == "config" and match and cmd not in self.reject:
            self.log.append(cmd)
            if not self.ignore_writes:
                self.vlans.discard(int(match.group(1)))
                self.contexts.pop(f"vlan {match.group(1)}", None)
            return mode, ""
        return super()._handle(mode, line)

    def context_prompt(self, mode):
        if mode == "config":
            return f"{self.hostname}(config)# "
        kind, name = mode
        if kind == "if":
            return f"{self.hostname}(config-if-{name})# "
        if name.lower().startswith("vlan "):
            return f"{self.hostname}(config-vlan{name.split()[1]})# "
        return f"{self.hostname}(config-{name.split()[0].lower()})# "

    def interface_command(self, intf, cmd):
        low = cmd.lower()
        store = not self.ignore_writes
        if low.startswith("description "):
            if store:
                intf["description"] = cmd[len("description "):].strip('"')
            return ""
        if low == "no description":
            if store:
                intf["description"] = None
            return ""
        if low in ("shutdown", "no shutdown"):
            if store:
                intf["admin"] = "down" if low == "shutdown" else "up"
            return ""
        if store:
            apply_generic(intf["extra"], cmd)
        return ""
