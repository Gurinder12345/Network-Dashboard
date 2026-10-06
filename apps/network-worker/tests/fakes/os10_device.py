"""
Simulated Dell OS10 SSH CLI for tests (paramiko server on 127.0.0.1). NOT a real switch.

Emulates just enough of OS10 10.5 for the change workflow:
  exec   : `Kenda-Core-1# `  terminal length/width, show running-configuration [interface X],
           show lldp neighbors, configure terminal, exit
  config : `Kenda-Core-1(config)# `  interface ethernetX/Y/Z, end, exit
  if     : `Kenda-Core-1(conf-if-eth1/1/5)# `  description <text>, no description,
           shutdown, no shutdown, switchport mode access|trunk, switchport access vlan <id>,
           switchport trunk allowed vlan <list> (ADDS, as documented for OS10),
           no switchport trunk allowed vlan <list>, end, exit
Anything else returns `% Error: Unrecognized command.` like the device does. Assigning a
VLAN that does not exist returns an error (the platform's precheck refuses it first).
Running-configuration and `show lldp neighbors` layouts follow the documented OS10 format
and the real LLDP capture (tests/fixtures/os10_real_show_lldp_neighbors.txt); the
switchport rendering is NOT from a lab capture.

Knobs: reject (commands answered with an error), ignore_writes (accept writes but do not
store them -> post-check mismatch). Every received line is recorded in `log`.
Credentials are test-only placeholders.
"""

import socket
import threading

import paramiko

from changes.vlans import VlanListError, format_vlan_list, parse_vlan_list

TEST_USERNAME = "test-automation"
TEST_PASSWORD = "test-only-not-a-real-password"
MANAGEMENT_IP = "192.0.2.10"

PROMPTS = {"exec": "#", "config": "(config)#"}


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


class _Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        ok = username == TEST_USERNAME and password == TEST_PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        return True


class FakeOS10:
    def __init__(self, hostname="Kenda-Core-1", interfaces=None, reject=(), ignore_writes=False, vlans=(1, 10, 20, 30, 40),
                 lldp=None):
        self.hostname = hostname
        self.interfaces = interfaces or default_interfaces()
        self.vlans = set(vlans)
        self.lldp = dict(DEFAULT_LLDP if lldp is None else lldp)
        self.reject = set(reject)
        self.ignore_writes = ignore_writes
        self.log = []
        self.connections = 0
        self._key = paramiko.RSAKey.generate(2048)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    # ---- rendering ---------------------------------------------------------------
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
                 f"hostname {self.hostname}", "!"]
        for vlan in sorted(self.vlans):
            lines += [f"interface vlan{vlan}", " no shutdown", "!"]
        lines += ["interface mgmt1/1/1", " no shutdown", " no ip address dhcp", f" ip address {MANAGEMENT_IP}/24",
                  " ipv6 address autoconfig", "!"]
        for name in self.interfaces:
            lines += self.interface_text(name)
        lines += ["!", "end"]
        return "\r\n".join(lines)

    def lldp_neighbors(self):
        lines = ["Loc PortID          Rem Host Name        Rem Port Id                    Rem Chassis Id",
                 "-" * 86]
        for local, (host, remote_port, chassis) in self.lldp.items():
            lines.append(f"{local:<20}{host:<21}{remote_port:<30}{chassis:<24}")
        return "\r\n".join(lines)

    # ---- CLI -------------------------------------------------------------------------
    def _prompt(self, mode):
        if mode.startswith("if:"):
            return f"{self.hostname}(conf-if-eth{mode[3:][len('ethernet'):]})# "
        return f"{self.hostname}{PROMPTS[mode]} "

    def _interface_command(self, intf, cmd, low):
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
        return "% Error: Unrecognized command."

    def _handle(self, mode, line):
        cmd = " ".join(line.strip().split())
        low = cmd.lower()
        if cmd:
            self.log.append(cmd)
        if not cmd:
            return mode, ""
        if cmd in self.reject:
            return mode, "% Error: Command rejected by simulated device."
        if mode == "exec":
            if low.startswith("terminal "):
                return mode, ""
            if low == "show running-configuration":
                return mode, self.running_config()
            if low == "show lldp neighbors":
                return mode, self.lldp_neighbors()
            if low.startswith("show running-configuration interface "):
                name = low.split()[-1].replace("ethernet", "ethernet")
                return mode, "\r\n".join(self.interface_text(name)) if name in self.interfaces else "% Error: Interface not found."
            if low in ("configure terminal", "configure"):
                return "config", ""
            if low == "exit":
                return None, ""
            return mode, "% Error: Unrecognized command."
        if low == "end":
            return "exec", ""
        if mode == "config":
            if low.startswith("interface "):
                name = low[len("interface "):].replace(" ", "")
                if name in self.interfaces:
                    return f"if:{name}", ""
                return mode, "% Error: Interface not found."
            if low == "exit":
                return "exec", ""
            return mode, "% Error: Unrecognized command."
        # interface mode
        if low == "exit":
            return "config", ""
        return mode, self._interface_command(self.interfaces[mode[3:]], cmd, low)

    def _session(self, client):
        transport = paramiko.Transport(client)
        transport.add_server_key(self._key)
        try:
            transport.start_server(server=_Server())
            channel = transport.accept(20)
            if channel is None:
                return
            self.connections += 1
            mode = "exec"
            channel.send(f"\r\n{self._prompt(mode)}")
            buffer = ""
            while True:
                data = channel.recv(4096)
                if not data:
                    break
                for ch in data.decode("utf-8", "replace"):
                    if ch in "\r\n":
                        channel.send("\r\n")
                        mode_after, output = self._handle(mode, buffer)
                        buffer = ""
                        if mode_after is None:
                            channel.close()
                            return
                        mode = mode_after
                        if output:
                            channel.send(output + "\r\n")
                        channel.send(self._prompt(mode))
                    else:
                        buffer += ch
                        channel.send(ch)  # echo, like a terminal
        except Exception:
            pass
        finally:
            transport.close()

    def _accept(self):
        while not self._stop:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def close(self):
        self._stop = True
        # shutdown() wakes the thread blocked in accept(); close() alone leaves it listening.
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
