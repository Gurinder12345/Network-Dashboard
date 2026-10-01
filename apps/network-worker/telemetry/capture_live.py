"""
Read-only CPU / memory / uptime command discovery against live switches.

Run inside a network-worker pod (Vault + device reachability):
    python -m telemetry.capture_live Kenda-HARO-IDF-A Kenda-Core-1

For each device it opens ONE SSH session through the existing health-check session code
(Vault credentials, short timeouts), runs the fixed candidate `show` commands below for
its platform, prints each command's output and duration, and saves the raw text to
/tmp/telemetry_capture/<hostname>__<command>.txt (mode 0600) for parser fixtures.

Never runs anything but `show` commands, never enters configuration mode, and never
writes to PostgreSQL or Redis. Credentials are never printed; if a password or enable
secret ever appeared in output it is replaced with ***.

A candidate that the platform does not support simply returns a CLI error, which is
reported; that is how the supported commands are confirmed rather than assumed.
"""

import argparse
import os
import re
import sys
import time

from db.devices import get_device_by_hostname
from health.checks import _open_session, _safe_error, _tcp_reachable
from vault.client import get_device_credentials


# Candidates only: the parsers are written from whatever these actually return.
CANDIDATE_COMMANDS = {
    "dell_os6": (
        "show process cpu",
        "show memory cpu",
        "show system",
        "show version",
    ),
    "dell_os10": (
        "show processes node-id 1",
        "show system",
        "show version",
        "show uptime",
    ),
}

CAPTURE_DIR = "/tmp/telemetry_capture"
READ_TIMEOUT_SECONDS = 60
CLI_ERROR = re.compile(r"^\s*%\s*(error|invalid|incomplete|ambiguous)|unrecognized command|invalid input", re.I | re.M)
SAFE_COMMAND = re.compile(r"^show [a-z0-9 \-]+$")


def _slug(command):
    return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")


def capture(device, extra_commands=()):
    commands = list(CANDIDATE_COMMANDS.get(device["platform"], ())) + list(extra_commands)
    for command in commands:
        if not SAFE_COMMAND.match(command):
            raise SystemExit(f"Refusing non-show command: {command!r}")

    print(f"\n######## {device['hostname']} ({device['platform']}, {device['management_ip']})")

    tcp_ok, tcp_error = _tcp_reachable(device["management_ip"])
    if not tcp_ok:
        print(f"UNREACHABLE: {tcp_error}")
        return False

    credentials = get_device_credentials(device["credential_path"])
    secrets = [s for s in (credentials.get("password"), credentials.get("secret")) if s]

    os.makedirs(CAPTURE_DIR, mode=0o700, exist_ok=True)
    started = time.monotonic()
    session = _open_session(device, credentials)
    print(f"session opened in {time.monotonic() - started:.1f} s")

    try:
        for command in commands:
            t0 = time.monotonic()
            try:
                output = session.send_command(command, read_timeout=READ_TIMEOUT_SECONDS)
            except Exception as exc:
                print(f"\n==== {command}  FAILED after {time.monotonic() - t0:.1f} s: {_safe_error(f'{type(exc).__name__}: {exc}', secrets)}")
                continue

            elapsed = time.monotonic() - t0
            for secret in secrets:
                output = output.replace(secret, "***")

            path = os.path.join(CAPTURE_DIR, f"{device['hostname']}__{_slug(command)}.txt")
            with open(path, "w") as handle:
                handle.write(output)
            os.chmod(path, 0o600)

            verdict = "CLI ERROR (unsupported?)" if CLI_ERROR.search(output) else "ok"
            print(f"\n==== {command}  [{verdict}]  {elapsed:.1f} s  {len(output.splitlines())} lines -> {path}")
            print(output)
    finally:
        try:
            session.disconnect()
        except Exception:
            pass

    print(f"\n{device['hostname']}: total {time.monotonic() - started:.1f} s including login")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hostnames", nargs="+")
    parser.add_argument("--extra", action="append", default=[], help='extra read-only command, e.g. --extra "show system brief"')
    args = parser.parse_args()

    ok = True
    for hostname in args.hostnames:
        device = get_device_by_hostname(hostname)  # enabled devices only
        ok = capture(device, args.extra) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
