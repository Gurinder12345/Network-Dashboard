"""
Celery-side PCAP analysis lifecycle (see worker.py for the task registrations).

    queued -> validating -> extracting -> analyzing [-> correlating] -> completed | failed

Uploaded files are deleted as soon as the analysis ends (success or failure). The hourly
cleanup task is the backstop: it removes any upload directory past its expiry (24 h), any
orphan directory, and fails analyses whose worker never finished.
"""

import json
import logging
import os
import re
import resource
import shutil
import time
import uuid

from celery.exceptions import SoftTimeLimitExceeded

from db import pcap as db
from db.jobs import create_audit_event
from pcap import analyze
from pcap.tshark import TsharkError

logger = logging.getLogger("network_worker.pcap")

PCAP_ROOT = os.getenv("PCAP_ROOT", "/pcap-analysis")
STALE_AFTER_SECONDS = int(os.getenv("PCAP_STALE_AFTER_SECONDS", "3600"))
ORPHAN_AFTER_SECONDS = int(os.getenv("PCAP_ORPHAN_AFTER_SECONDS", "3600"))
MAX_RESULT_BYTES = int(os.getenv("PCAP_MAX_RESULT_BYTES", str(5 * 1024 * 1024)))
INTERNAL_NAME = re.compile(r"^(client|server)\.(pcap|pcapng)$")


def analysis_dir(analysis_id):
    """Directory for an analysis: always ROOT/<canonical uuid>, never derived from user input."""
    return os.path.join(PCAP_ROOT, str(uuid.UUID(str(analysis_id))))


def _audit(event_type, message):
    try:
        create_audit_event(job_id=None, device_id=None, event_type=event_type, message=message)
    except Exception:
        logger.warning("pcap audit event %s not recorded", event_type)


def remove_files(analysis_id):
    path = analysis_dir(analysis_id)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    db.mark_files_deleted(analysis_id)


def _shrink(result):
    """Keep the stored JSON bounded; detail tables are trimmed first, findings never."""
    for key, limit in (("flows", 100), ("server_flows", 50)):
        if len(json.dumps(result)) <= MAX_RESULT_BYTES:
            break
        if result.get(key):
            result[key] = result[key][:limit]
    if len(json.dumps(result)) > MAX_RESULT_BYTES:
        result["dns"]["transactions"] = result["dns"]["transactions"][:50]
        result["tls"]["sessions"] = result["tls"]["sessions"][:50]
    return result


def run_analysis(analysis_id):
    row = db.get_analysis(analysis_id)
    if row is None or not db.claim(analysis_id):
        logger.info("pcap_analysis_skipped id=%s reason=not_queued", analysis_id)
        return {"analysis_id": analysis_id, "skipped": True}

    started = time.monotonic()
    base = analysis_dir(analysis_id)
    paths = {}

    try:
        for role in ("client", "server"):
            name = row[f"{role}_file"]
            if name:
                if not INTERNAL_NAME.match(name) or not name.startswith(role):
                    raise TsharkError("Unexpected stored capture name")
                paths[role] = os.path.join(base, name)

        missing = [role for role, path in paths.items() if not os.path.isfile(path)]
        if missing:
            raise TsharkError(f"Uploaded capture file missing ({', '.join(missing)}); it may have expired")

        result = analyze.run(paths, row["mode"], stage=lambda s: db.set_stage(analysis_id, s))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result["performance"] = {
            "analysis_ms": elapsed_ms,
            "worker_peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "tshark_peak_rss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1),
        }
        result = _shrink(result)
        result["performance"]["result_bytes"] = len(json.dumps(result))

        client_summary = result["observations"]["captures"]["client"]["summary"]
        packets = sum(c["summary"]["packets"] for c in result["observations"]["captures"].values())
        counts = {sev: sum(1 for f in result["findings"] if f["severity"] == sev) for sev in ("critical", "warning", "info")}
        db.complete(analysis_id, result, packets, client_summary["flows"], client_summary["duration_s"],
                    result["assessment"]["issue"], counts)
        _audit("pcap_analysis_completed",
               f"PCAP analysis {analysis_id} completed ({row['mode']}): {packets} packets, {client_summary['flows']} flows, "
               f"{counts['critical']} critical / {counts['warning']} warning findings, {elapsed_ms} ms")
        logger.info("pcap_analysis_completed id=%s mode=%s packets=%s flows=%s findings=%s duration_ms=%s result_bytes=%s",
                    analysis_id, row["mode"], packets, client_summary["flows"], counts, elapsed_ms,
                    result["performance"]["result_bytes"])
        return {"analysis_id": analysis_id, "status": "completed"}

    except (TsharkError, SoftTimeLimitExceeded, Exception) as exc:
        if isinstance(exc, TsharkError):
            error = str(exc)
        elif isinstance(exc, SoftTimeLimitExceeded):
            error = "Analysis exceeded the time limit"
        else:
            error = f"Internal analysis error ({type(exc).__name__})"
            logger.exception("pcap_analysis_error id=%s", analysis_id)
        db.fail(analysis_id, error)
        _audit("pcap_analysis_failed", f"PCAP analysis {analysis_id} failed: {error}")
        logger.warning("pcap_analysis_failed id=%s error=%s", analysis_id, error)
        return {"analysis_id": analysis_id, "status": "failed", "error": error}

    finally:
        remove_files(analysis_id)


def cleanup():
    """Hourly backstop: expired/orphan upload directories and stale analyses."""
    removed, failed = [], db.fail_stale(STALE_AFTER_SECONDS)
    now = time.time()
    try:
        entries = os.listdir(PCAP_ROOT)
    except FileNotFoundError:
        entries = []
    for name in entries:
        path = os.path.join(PCAP_ROOT, name)
        try:
            canonical = str(uuid.UUID(name))
        except ValueError:
            continue  # never touch anything that is not one of ours
        if canonical != name or not os.path.isdir(path):
            continue
        row = db.get_analysis(canonical)
        age = now - os.path.getmtime(path)
        expired = row is not None and row["expires_at"].timestamp() <= now
        finished = row is not None and row["status"] in ("completed", "failed")
        orphan = row is None and age >= ORPHAN_AFTER_SECONDS
        if expired or finished or orphan:
            shutil.rmtree(path, ignore_errors=True)
            if row is not None:
                db.mark_files_deleted(canonical)
            removed.append(canonical)
    if removed or failed:
        logger.info("pcap_cleanup removed=%s stale_failed=%s", len(removed), len(failed))
    return {"removed_dirs": removed, "stale_failed": failed}
