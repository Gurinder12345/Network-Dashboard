"""
Simulated Dell OS10 SSH CLI for tests (paramiko server on 127.0.0.1). NOT a real switch.

Emulates just enough of OS10 10.5 for the change workflow:
  exec   : `Kenda-Core-1# `  terminal length/width, show running-configuration [interface X],
           configure terminal, exit
  config : `Kenda-Core-1(config)# `  interface ethernetX/Y/Z, end, exit
  if     : `Kenda-Core-1(conf-if-eth1/1/5)# `  description <text>, no description, end, exit
Anything else returns `% Error: Unrecognized command.` like the device does.
Interface names follow the real captures (tests/fixtures/os10_real_*.txt).

Knobs: reject (commands answered with an error), ignore_writes (accept description but
do not store it -> post-check mismatch). Every received line is recorded in `log`.
Credentials are test-only placeholders.
"""

import socket
import threading

import paramiko

TEST_USERNAME = "test-automation"
TEST_PASSWORD = "test-only-not-a-real-password"

PROMPTS = {"exec": "#", "config": "(config)#"}


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
    def __init__(self, hostname="Kenda-Core-1", interfaces=None, reject=(), ignore_writes=False):
        self.hostname = hostname
        self.interfaces = interfaces or {
            "ethernet1/1/5": {"description": None, "body": ["no shutdown", "switchport access vlan 1", "flowcontrol receive on"]},
            "ethernet1/1/30:2": {"description": "UPLINK-DO-NOT-TOUCH", "body": ["no shutdown", "channel-group 10 mode active"]},
            "ethernet1/1/31": {"description": None, "body": ["shutdown", "switchport access vlan 1"]},
        }
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
        lines += [f" {line}" for line in intf["body"]]
        lines.append("!")
        return lines

    def running_config(self):
        lines = ["! Version 10.5.4.0", "! Last configuration change at Oct  06 12:00:00 2026", "!",
                 f"hostname {self.hostname}", "!", "interface mgmt1/1/1", " no shutdown", " ip address dhcp", "!"]
        for name in self.interfaces:
            lines += self.interface_text(name)
        lines += ["!", "end"]
        return "\r\n".join(lines)

    # ---- CLI -------------------------------------------------------------------------
    def _prompt(self, mode):
        if mode.startswith("if:"):
            return f"{self.hostname}(conf-if-eth{mode[3:][len('ethernet'):]})# "
        return f"{self.hostname}{PROMPTS[mode]} "

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
        name = mode[3:]
        if low == "exit":
            return "config", ""
        if low.startswith("description "):
            if not self.ignore_writes:
                self.interfaces[name]["description"] = cmd[len("description "):]
            return mode, ""
        if low == "no description":
            if not self.ignore_writes:
                self.interfaces[name]["description"] = None
            return mode, ""
        return mode, "% Error: Unrecognized command."

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
