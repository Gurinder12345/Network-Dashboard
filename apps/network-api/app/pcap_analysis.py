"""
PCAP Analyzer API: upload one capture (single) or a client + server pair (dual), then
read the structured, payload-free analysis produced by the worker.

POST   /api/v1/pcap-analysis                 multipart: mode, client_file, [server_file] -> 202
GET    /api/v1/pcap-analysis                 recent analyses (metadata only)
GET    /api/v1/pcap-analysis/{analysis_id}   status/stage + structured result
DELETE /api/v1/pcap-analysis/{analysis_id}   delete result (and any remaining upload files)

Upload safety:
  - Content-Length required; the whole body is capped before reading; each file is capped
    while streaming (default 200 MB), so nothing large is buffered in memory or in /tmp.
  - Only .pcap/.pcapng names AND pcap/pcapng magic bytes are accepted.
  - Files are written as /pcap-analysis/<uuid>/client.<fmt> (0600). The user's filename is
    only a sanitized display label; it never forms a path.
  - Raw packets are never returned; results contain protocol metadata only.
Analysis never runs in the API process: the worker reads the files with TShark.
"""

import logging
import os
import re
import shutil
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from python_multipart.multipart import MultipartParser, parse_options_header

from app.celery_client import PUBLISH_RETRY_POLICY, celery_app
from app.db import pcap as db
from app.db.audit import create_audit_event

logger = logging.getLogger("network_api.pcap")

router = APIRouter(prefix="/api/v1/pcap-analysis", tags=["pcap"])

PCAP_ROOT = os.path.realpath(os.getenv("PCAP_ROOT", "/pcap-analysis"))
MAX_FILE_BYTES = int(os.getenv("PCAP_MAX_UPLOAD_MB", "200")) * 1024 * 1024
MAX_ACTIVE = int(os.getenv("PCAP_MAX_ACTIVE", "2"))
RETENTION_HOURS = int(os.getenv("PCAP_RETENTION_HOURS", "24"))
MAX_BODY_BYTES = 2 * MAX_FILE_BYTES + 1024 * 1024  # two files + multipart overhead
ANALYZE_TASK = "network_worker.analyze_pcap"
REQUESTED_BY = "dashboard"  # no dashboard authentication yet

MAGIC = {
    b"\xd4\xc3\xb2\xa1": "pcap", b"\xa1\xb2\xc3\xd4": "pcap", b"\x4d\x3c\xb2\xa1": "pcap", b"\xa1\xb2\x3c\x4d": "pcap",
    b"\x0a\x0d\x0d\x0a": "pcapng",
}
EXTENSIONS = (".pcap", ".pcapng")
FILE_FIELDS = {"client_file": "client", "server_file": "server"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._()+\- ]")


class UploadError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def display_name(raw):
    """Sanitized label for the UI: basename only, safe characters, bounded length."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = SAFE_NAME.sub("_", name).strip(" .") or "capture"
    return name[-120:]


class StreamingForm:
    """python-multipart callbacks writing file parts straight to the analysis directory."""

    def __init__(self, directory):
        self.directory = directory
        self.fields, self.files = {}, {}
        self._header_field = self._header_value = b""
        self._headers = {}
        self._part = None

    # --- parser callbacks ---------------------------------------------------------
    def on_part_begin(self):
        self._headers, self._header_field, self._header_value = {}, b"", b""
        self._part = None

    def on_header_field(self, data, start, end):
        self._header_field += data[start:end]

    def on_header_value(self, data, start, end):
        self._header_value += data[start:end]

    def on_header_end(self):
        self._headers[self._header_field.decode("latin-1").lower()] = self._header_value
        self._header_field = self._header_value = b""

    def on_headers_finished(self):
        _, params = parse_options_header(self._headers.get("content-disposition", b""))
        name = params.get(b"name", b"").decode("latin-1")
        if name in FILE_FIELDS:
            role = FILE_FIELDS[name]
            if role in self.files:
                raise UploadError(400, f"{name} was sent more than once")
            filename = params.get(b"filename", b"").decode("utf-8", "replace")
            if not filename.lower().endswith(EXTENSIONS):
                raise UploadError(415, "Only .pcap and .pcapng files are accepted")
            path = os.path.join(self.directory, f"{role}.upload")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.files[role] = {"display_name": display_name(filename), "size": 0, "head": b"",
                                "handle": os.fdopen(fd, "wb"), "path": path}
            self._part = ("file", role)
        elif name == "mode":
            if name in self.fields:
                raise UploadError(400, "mode was sent more than once")
            self.fields[name] = b""
            self._part = ("field", name)
        else:
            raise UploadError(400, f"Unexpected form field: {display_name(name)[:40]}")

    def on_part_data(self, data, start, end):
        if self._part is None:
            return
        chunk = data[start:end]
        kind, key = self._part
        if kind == "field":
            self.fields[key] += chunk
            if len(self.fields[key]) > 16:
                raise UploadError(400, "Invalid mode")
            return
        f = self.files[key]
        f["size"] += len(chunk)
        if f["size"] > MAX_FILE_BYTES:
            raise UploadError(413, f"Capture files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB each")
        if len(f["head"]) < 4:
            f["head"] = (f["head"] + chunk)[:4]
        f["handle"].write(chunk)

    def on_part_end(self):
        if self._part and self._part[0] == "file":
            self.files[self._part[1]]["handle"].close()
        self._part = None

    def callbacks(self):
        return {name: getattr(self, name) for name in (
            "on_part_begin", "on_header_field", "on_header_value", "on_header_end", "on_headers_finished",
            "on_part_data", "on_part_end")}

    def close(self):
        for f in self.files.values():
            if not f["handle"].closed:
                f["handle"].close()

    # --- validation after the body is read ----------------------------------------
    def finish(self):
        mode = self.fields.get("mode", b"").decode("ascii", "replace").strip()
        if mode not in ("single", "dual"):
            raise UploadError(422, "mode must be 'single' or 'dual'")
        if "client" not in self.files:
            raise UploadError(422, "client_file is required")
        if mode == "single" and "server" in self.files:
            raise UploadError(422, "server_file is only accepted in dual mode")
        if mode == "dual" and "server" not in self.files:
            raise UploadError(422, "server_file is required in dual mode")
        for role, f in self.files.items():
            if f["size"] == 0:
                raise UploadError(422, f"The {role} file is empty")
            fmt = MAGIC.get(f["head"])
            if fmt is None:
                raise UploadError(415, f"The {role} file is not a pcap or pcapng capture")
            f["stored_name"] = f"{role}.{fmt}"
            os.replace(f["path"], os.path.join(self.directory, f["stored_name"]))
        return mode


def _analysis_dir(analysis_id):
    path = os.path.realpath(os.path.join(PCAP_ROOT, str(uuid.UUID(str(analysis_id)))))
    if os.path.dirname(path) != PCAP_ROOT:
        raise HTTPException(status_code=400, detail="Invalid analysis id")
    return path


def _audit(event_type, message):
    try:
        create_audit_event(job_id=None, device_id=None, event_type=event_type, message=message)
    except Exception:
        logger.exception("Failed to record %s audit event", event_type)


@router.post("", status_code=202)
async def create_analysis(request: Request):
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise HTTPException(status_code=415, detail="Expected multipart/form-data")
    try:
        length = int(request.headers.get("content-length", ""))
    except ValueError:
        raise HTTPException(status_code=411, detail="Content-Length is required")
    if length > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Capture files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB each")

    try:
        active = db.count_active()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if active >= MAX_ACTIVE:
        raise HTTPException(status_code=429, detail=f"{active} analyses are already in progress; try again when one finishes")

    analysis_id = str(uuid.uuid4())
    directory = _analysis_dir(analysis_id)
    os.makedirs(directory, mode=0o700)
    form = StreamingForm(directory)
    parser = MultipartParser(boundary, form.callbacks())

    try:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                raise UploadError(413, "Upload is larger than the allowed size")
            await run_in_threadpool(parser.write, chunk)
        parser.finalize()
        form.close()
        mode = form.finish()
    except UploadError as exc:
        form.close()
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except Exception:
        form.close()
        shutil.rmtree(directory, ignore_errors=True)
        logger.exception("pcap upload failed")
        raise HTTPException(status_code=400, detail="Malformed upload")

    try:
        db.insert_analysis(analysis_id, mode, form.files, REQUESTED_BY, RETENTION_HOURS)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        logger.exception("pcap analysis row not created")
        raise HTTPException(status_code=500, detail="Could not record the analysis")

    try:
        celery_app.send_task(ANALYZE_TASK, kwargs={"analysis_id": analysis_id}, retry=True, retry_policy=PUBLISH_RETRY_POLICY)
    except Exception as exc:
        db.mark_failed(analysis_id, "Task queue unavailable")
        shutil.rmtree(directory, ignore_errors=True)
        logger.exception("pcap analysis not queued")
        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {type(exc).__name__}")

    sizes = ", ".join(f"{role} {f['size']} bytes" for role, f in form.files.items())
    _audit("pcap_analysis_requested", f"PCAP analysis {analysis_id} requested ({mode}; {sizes}; requested_by={REQUESTED_BY})")
    return {"analysis_id": analysis_id, "status": "queued", "mode": mode}


@router.get("")
def list_analyses():
    try:
        return db.list_recent()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _uuid_or_400(analysis_id):
    try:
        return str(uuid.UUID(analysis_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid analysis id")


@router.get("/{analysis_id}")
def get_analysis(analysis_id: str):
    analysis_id = _uuid_or_400(analysis_id)
    try:
        row = db.get_analysis(analysis_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return row


@router.delete("/{analysis_id}")
def delete_analysis(analysis_id: str):
    analysis_id = _uuid_or_400(analysis_id)
    outcome = db.delete_analysis(analysis_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    if outcome == "active":
        raise HTTPException(status_code=409, detail="Analysis is still running")
    shutil.rmtree(_analysis_dir(analysis_id), ignore_errors=True)
    _audit("pcap_analysis_deleted", f"PCAP analysis {analysis_id} deleted (requested_by={REQUESTED_BY})")
    return {"analysis_id": analysis_id, "status": "deleted"}
