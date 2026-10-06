"""
Simulated switch CLI over SSH (paramiko server on 127.0.0.1) for tests. NOT a real switch.

Shared by FakeOS10 (os10_device.py) and FakeOS6 (os6_device.py). Accepts arbitrary
configuration CLI the way a device does, so multi-block changes can be exercised end to
end with the real Netmiko readers and the real Ansible playbook:

  * modes: exec (#), config, interface context, generic parent context
  * known interfaces only (unknown physical interface -> device error)
  * commands that open a context (router / vlan / line / ip access-list / route-map /
    class-map / policy-map / spanning-tree mst ...) enter it; `exit` / `end` leave
  * any other command is stored in the current context; `no <X>` removes <X>
  * `reject`: exact command lines answered with a device error (failure tests)
  * `ignore_writes`: accept configuration but store nothing (post-check mismatch tests)
Every received line is recorded in `log`. Credentials are test-only placeholders.
"""

import socket
import threading

import paramiko

TEST_USERNAME = "test-automation"
TEST_PASSWORD = "test-only-not-a-real-password"

CONTEXT_OPENERS = ("router ", "vlan ", "line ", "ip access-list ", "ipv6 access-list ", "mac access-list ",
                   "route-map ", "class-map ", "policy-map ", "spanning-tree mst configuration", "aaa group ",
                   "snmp-server view ")


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


def apply_generic(lines, command):
    """Store `command` in a context's line list (`no X` removes X / `X ...`)."""
    if command.lower().startswith("no ") and len(command) > 3:
        positive = command[3:].strip()
        lines[:] = [l for l in lines if l != positive and not l.startswith(positive + " ")]
    elif command not in lines:
        lines.append(command)


class FakeSwitch:
    configure_commands = ("configure terminal", "configure")
    error_text = "% Error: Command rejected by simulated device."

    def __init__(self, hostname, interfaces, reject=(), ignore_writes=False):
        self.hostname = hostname
        self.interfaces = interfaces
        self.global_lines = []
        self.contexts = {}  # parent line -> [child lines]
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

    # ---- flavor hooks -----------------------------------------------------------------
    def running_config(self):
        raise NotImplementedError

    def show(self, command):
        """Output of an exec-mode show command, or None if unknown."""
        return None

    def interface_name(self, text):
        """Canonical interface key for `interface <text>` or None."""
        return None

    def interface_command(self, intf, command):
        """Handle an interface-mode command; return an error string or ''."""
        return ""

    def context_prompt(self, mode):
        raise NotImplementedError

    def open_context(self, command):
        """Called when a global command opens a context; return the context key."""
        self.contexts.setdefault(command, [])
        return command

    # ---- CLI --------------------------------------------------------------------------
    def _prompt(self, mode):
        if mode == "exec":
            return f"{self.hostname}# "
        if mode == "user":
            return f"{self.hostname}> "
        return self.context_prompt(mode)

    def _store(self, target, command):
        if not self.ignore_writes:
            apply_generic(target, command)

    def _handle(self, mode, line):
        cmd = " ".join(line.strip().split())
        low = cmd.lower()
        if cmd:
            self.log.append(cmd)
        if not cmd:
            return mode, ""
        if cmd in self.reject:
            return mode, self.error_text

        if mode in ("exec", "user"):
            if low.startswith("terminal "):
                return mode, ""
            if low == "enable":
                return "exec", ""
            if low == "disable":
                return "user", ""
            if low in self.configure_commands:
                return "config", ""
            if low == "exit":
                return None, ""
            output = self.show(low)
            return mode, output if output is not None else self.error_text

        if low == "end":
            return "exec", ""

        if mode == "config":
            if low == "exit":
                return "exec", ""
            if low.startswith("interface "):
                name = self.interface_name(cmd[len("interface "):])
                if name is None:
                    return mode, self.error_text.replace("Command rejected by simulated device", "Interface not found")
                return ("if", name), ""
            if low.startswith(CONTEXT_OPENERS):
                return ("ctx", self.open_context(cmd)), ""
            self._store(self.global_lines, cmd)
            return mode, ""

        kind, name = mode
        if low == "exit":
            return "config", ""
        if kind == "if":
            return mode, self.interface_command(self.interfaces[name], cmd)
        self._store(self.contexts[name], cmd)
        return mode, ""

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

    def config_writes(self):
        """Lines received from the first configuration-mode entry onwards."""
        for index, line in enumerate(self.log):
            if line.lower() in self.configure_commands:
                return self.log[index:]
        return []
