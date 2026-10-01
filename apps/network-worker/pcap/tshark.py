"""
Safe TShark / capinfos execution for uploaded captures (read-only file analysis).

- Argument arrays only: never a shell, never a filename inside a command string.
- `-n`: no name resolution, so analysing a capture never triggers DNS lookups.
- Minimal environment, a private HOME (no user preferences or plugins), and per-run
  limits: wall-clock timeout, address-space and CPU-time rlimits.
- Output is streamed line by line; the capture is never loaded into Python memory.
- Error messages never include the on-disk path (replaced with "<capture>").
No live capture: dumpcap is not used (and is removed from the worker image).
"""

import os
import resource
import shutil
import signal
import subprocess
import tempfile
import threading

TSHARK = "/usr/bin/tshark"
CAPINFOS = "/usr/bin/capinfos"

TIMEOUT_SECONDS = int(os.getenv("PCAP_TSHARK_TIMEOUT_SECONDS", "600"))
MEMORY_LIMIT_BYTES = int(os.getenv("PCAP_TSHARK_MEMORY_LIMIT_MB", "2048")) * 1024 * 1024
MAX_STDERR_BYTES = 8192


class TsharkError(RuntimeError):
    """A capture could not be read; the message is safe to show to users."""


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _env(home):
    return {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


NOISE = ("Running as user", "will continue anyway")


def _safe_message(stderr, path):
    """First meaningful tool message, without the file path or the tool-name prefix."""
    text = (stderr or "").replace(path, "<capture>").strip()
    lines = []
    for line in text.splitlines():
        line = line.strip()
        for prefix in ("capinfos: ", "tshark: "):
            if line.startswith(prefix):
                line = line[len(prefix):]
        if line and not any(noise in line for noise in NOISE):
            lines.append(line)
    return " ".join(lines)[:300] or "capture could not be read"


def capinfos(path):
    """capinfos table output as {column name: value}."""
    args = [CAPINFOS, "-M", "-T", "-t", "-E", "-c", "-s", "-u", "-a", "-e", "-y", "-x", "-z", "--", path]
    with tempfile.TemporaryDirectory(prefix="pcap-home-") as home:
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=120, env=_env(home),
                                  preexec_fn=_limits, check=False)
        except subprocess.TimeoutExpired:
            raise TsharkError("Reading capture metadata timed out") from None

    if proc.returncode != 0:
        raise TsharkError(f"Not a readable packet capture: {_safe_message(proc.stderr, path)}")

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise TsharkError("Not a readable packet capture: no metadata returned")
    header, values = lines[0].split("\t"), lines[1].split("\t")
    return dict(zip(header, values))


def iter_fields(path, fields, display_filter=None):
    """
    Yield one list of field values per packet (aggregated multi-values joined by ',').
    Raises TsharkError on non-zero exit or timeout, after the iteration ends.
    """
    args = [TSHARK, "-n", "-r", path, "-T", "fields", "-E", "separator=\t", "-E", "occurrence=a",
            "-E", "aggregator=,", "-E", "quote=n"]
    for field in fields:
        args += ["-e", field]
    if display_filter:
        args += ["-Y", display_filter]

    home = tempfile.mkdtemp(prefix="pcap-home-")
    # Own process group, so a timeout kills TShark and anything it spawned.
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1 << 16,
                            env=_env(home), preexec_fn=_limits, start_new_session=True)
    stderr_chunks = []

    def drain():
        data = proc.stderr.read(MAX_STDERR_BYTES)
        stderr_chunks.append(data)
        proc.stderr.read()  # discard the rest so tshark never blocks on a full pipe

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    fired = threading.Event()

    def kill_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def kill():
        fired.set()
        kill_group()

    timer = threading.Timer(TIMEOUT_SECONDS, kill)
    timer.start()
    width = len(fields)
    try:
        for line in proc.stdout:
            values = line.rstrip("\n").split("\t")
            if len(values) < width:
                values += [""] * (width - len(values))
            yield values
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            kill_group()
            proc.wait()
        reader.join(timeout=5)
        shutil.rmtree(home, ignore_errors=True)

    if fired.is_set():
        raise TsharkError(f"Packet analysis timed out after {TIMEOUT_SECONDS} s")
    if proc.returncode != 0:
        raise TsharkError(f"Packet analysis failed: {_safe_message(''.join(stderr_chunks), path)}")
