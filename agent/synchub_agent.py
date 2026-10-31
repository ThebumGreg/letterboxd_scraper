#!/usr/bin/env python3
"""The SyncHub worker agent: runs on a worker PC, asks SyncHub for jobs over HTTP and does them.

One file, Python standard library only (Python 3.8 or newer). ffmpeg is used by the encoding job types that come later and is
optional for copy jobs. SyncHub never connects to this PC: the agent polls, so nothing needs to be opened on it.

Everything a job needs to survive a crash lives in its own folder under <work>/jobs/ (no database): a manifest.json written
atomically, the job's own log, and the verified copy of its source. After a restart the agent finds those folders, tells SyncHub what
it holds and carries on from the first unfinished step. A finished job's report to SyncHub is saved before it is sent and sent
again until it is accepted, so a dropped connection never loses a result.

    python3 synchub_agent.py --config synchub-agent.ini      run
    python3 synchub_agent.py --check                         say what works and what does not, then exit
    python3 synchub_agent.py --init-roots                    write the marker file into each share (once, when you ask)
"""
import argparse
import configparser
import errno
import hashlib
import json
import logging
import logging.handlers
import math
import os
import platform
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import media_analysis                  # next to this file; without it the agent still copies, it just cannot scan
except ImportError:                        # pragma: no cover - exercised by tests that remove the module
    media_analysis = None

__version__ = "1.4.0"      # kept equal to SyncHub's version (synchub/__init__.py); see docs/RELEASING.md
PROTOCOL = 1                       # the SyncHub worker API version this agent speaks
MARKER = ".synchub-root"
OPS = ["copy", "rename", "delete_source", "originals"]     # the operations (plan.op) every agent can do; scan, hash and convert are added when the analysis module and ffmpeg are there
FEATURES = ["profiles", "scale", "audio_add", "benchmark", "two_outputs"]   # plan fields this agent understands; a job that needs one is only given to an agent that lists it (see jobs.can_run). "dovi" is added when dovi_tool and mkvmerge work
MANIFEST_VERSION = 1
HASH_BUFFER = 8 * 1024 * 1024
SPACE_MARGIN = 1024 ** 3           # room left over on a disk after a copy

DEFAULT_CONFIG_FILES = ("synchub-agent.ini", "/etc/synchub-agent.ini")


# ============================================================================ small helpers
def utcnow():
    return datetime.now(timezone.utc)


def iso(moment=None):
    return (moment or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def human_bytes(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_duration(seconds):
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def atomic_write_json(path, data):
    """Writes the file so that a crash leaves either the old complete file or the new complete file, never half of one."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass                                           # not every file system allows syncing a folder


def hash_file(path, upto=None, check=None):
    """sha256 of a file (or of its first `upto` bytes). `check` is called between blocks so a long hash can be interrupted."""
    digest, left = hashlib.sha256(), upto
    with open(path, "rb") as handle:
        while left is None or left > 0:
            block = handle.read(HASH_BUFFER if left is None else min(HASH_BUFFER, left))
            if not block:
                break
            digest.update(block)
            if left is not None:
                left -= len(block)
            if check:
                check(len(block))
    return digest.hexdigest()


def safe_path(root_path, rel):
    """The real path of `rel` inside the root, or ValueError if it would leave it (a '..', an absolute path or a symlink out)."""
    rel = str(rel or "").replace("\\", "/")
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\x00" in rel:
        raise ValueError(f"'{rel}' is not a path inside the share")
    if rel == MARKER:
        raise ValueError("The marker file is not a job's to touch")
    root_real = os.path.realpath(root_path)
    target = os.path.join(root_real, *parts)
    probe = target
    while not os.path.lexists(probe):                  # the closest folder that exists must still be inside the root
        probe = os.path.dirname(probe)
    real = os.path.realpath(probe)
    if real != root_real and not real.startswith(root_real + os.sep):
        raise ValueError(f"'{rel}' leads out of the share")
    return target


# ============================================================================ configuration
class ConfigError(Exception):
    """The settings are missing or wrong; the message tells the user what to change."""


class Config:
    INTS = {"slots": (1, 16), "scan_threads": (1, 16), "scan_batch": (1, 200), "log_max_mb": (1, 1024), "log_backups": (0, 50), "copy_chunk_mb": (1, 256), "checkpoint_mb": (16, 8192),
            "failed_retention_days": (0, 3650), "request_timeout": (5, 300), "chunk_minutes": (0, 120), "vmaf_samples": (1, 12)}
    FLOATS = {"done_retention_hours": (0, 24 * 365), "max_copy_mb_s": (0, 100000), "scan_retry_seconds": (0, 300)}

    def __init__(self, path=None, env=None):
        env = os.environ if env is None else env
        parser = configparser.ConfigParser(interpolation=None)
        self.path = path
        if path:
            if not os.path.isfile(path):
                raise ConfigError(f"The settings file {path} does not exist.")
            parser.read(path, encoding="utf-8")
        elif env is os.environ:
            for candidate in DEFAULT_CONFIG_FILES:
                if os.path.isfile(candidate):
                    parser.read(candidate, encoding="utf-8")
                    self.path = candidate
                    break

        def pick(section, key, env_key, default=None):
            if env.get(env_key):
                return env[env_key].strip()
            return parser.get(section, key, fallback=default) if parser.has_section(section) else default

        self.url = (pick("synchub", "url", "SYNCHUB_URL") or "").rstrip("/")
        self.token = pick("synchub", "token", "SYNCHUB_TOKEN") or ""
        self.name = pick("synchub", "name", "SYNCHUB_NAME") or ""
        self.work_dir = pick("worker", "work_dir", "SYNCHUB_WORK", "./work")
        self.hostname = pick("worker", "hostname", "SYNCHUB_HOSTNAME") or socket.gethostname()
        self.ffmpeg = pick("worker", "ffmpeg", "SYNCHUB_FFMPEG", "ffmpeg")
        self.ffprobe = pick("worker", "ffprobe", "SYNCHUB_FFPROBE", "ffprobe")
        self.dovi_tool = pick("worker", "dovi_tool", "SYNCHUB_DOVI_TOOL", "dovi_tool")
        self.mkvmerge = pick("worker", "mkvmerge", "SYNCHUB_MKVMERGE", "mkvmerge")
        self.log_level = (pick("worker", "log_level", "SYNCHUB_LOG_LEVEL", "INFO") or "INFO").upper()
        self.verify = (pick("worker", "verify", "SYNCHUB_VERIFY", "hash") or "hash").lower()
        self.video_encoder = (pick("worker", "video_encoder", "SYNCHUB_VIDEO_ENCODER", "auto") or "auto").lower()      # which GPU encoder to use when a plan allows GPU encoding
        self.vaapi_device = pick("worker", "vaapi_device", "SYNCHUB_VAAPI_DEVICE", "/dev/dri/renderD128") or "/dev/dri/renderD128"
        self.gpu_slots = 1                                 # GPU encodes at once; set from SyncHub (an integrated GPU should not be given more)
        self.use_proxy = (pick("synchub", "use_proxy", "SYNCHUB_USE_PROXY", "no") or "no").lower() in ("1", "yes", "true", "on")
        defaults = {"scan_threads": 2, "scan_batch": 25, "slots": 1, "log_max_mb": 10, "log_backups": 5, "copy_chunk_mb": 8, "checkpoint_mb": 256, "failed_retention_days": 14, "request_timeout": 30, "chunk_minutes": 10, "vmaf_samples": 5}
        for key, (low, high) in self.INTS.items():
            raw = pick("worker", key, "SYNCHUB_" + key.upper(), str(defaults[key]))
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{key} must be a whole number (it is '{raw}').")
            if not low <= value <= high:
                raise ConfigError(f"{key} must be between {low} and {high}.")
            setattr(self, key, value)
        for key, (low, high) in self.FLOATS.items():
            raw = pick("worker", key, "SYNCHUB_" + key.upper(), {"done_retention_hours": "1", "scan_retry_seconds": "3"}.get(key, "0"))
            try:
                value = float(raw)
            except ValueError:
                raise ConfigError(f"{key} must be a number (it is '{raw}').")
            if not low <= value <= high:
                raise ConfigError(f"{key} must be between {low} and {high}.")
            setattr(self, key, value)
        self.root_overrides = {}
        if parser.has_section("roots"):
            self.root_overrides.update({k.lower(): v.strip() for k, v in parser.items("roots") if v.strip()})
        for key, value in env.items():
            if key.startswith("SYNCHUB_ROOT_") and value.strip():
                self.root_overrides[key[len("SYNCHUB_ROOT_"):].lower()] = value.strip()

    REMOTE = {"slots": ("slots", int, 1, 16), "gpu_slots": ("gpu_slots", int, 1, 16), "max_copy_mb_s": ("max_copy_mb_s", float, 0, 100000), "scan_threads": ("scan_threads", int, 1, 16)}

    def apply_remote(self, remote):
        """Settings chosen on SyncHub's Workers page (they win over the .env and ini values). A setting SyncHub does not mention goes back to the local value.
        Returns what changed. A value that is not understood is ignored, never an error: SyncHub being newer than this agent must not stop a worker."""
        changed = {}
        if not isinstance(remote, dict):
            return changed
        if not hasattr(self, "_local"):
            self._local = {k: getattr(self, k) for k in ("slots", "gpu_slots", "max_copy_mb_s", "scan_threads", "video_encoder", "log_level")}
        wanted = dict(self._local)
        for key, (attr, kind, low, high) in self.REMOTE.items():
            try:
                value = kind(remote[key])
            except (KeyError, TypeError, ValueError):
                continue
            if low <= value <= high:
                wanted[attr] = value
        encoder = str(remote.get("encoder") or "").lower()
        if encoder in ("auto", "cpu", "nvenc", "qsv", "vaapi"):
            wanted["video_encoder"] = encoder
        level = str(remote.get("log_level") or "").upper()
        if level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            wanted["log_level"] = level
        for attr, value in wanted.items():
            if getattr(self, attr) != value:
                changed["encoder" if attr == "video_encoder" else attr] = value
                setattr(self, attr, value)
        return changed

    def validate(self):
        if not self.url or not re.match(r"^https?://[^/\s]+", self.url):
            raise ConfigError("SyncHub's address is missing or wrong. Set url in the [synchub] section (or SYNCHUB_URL), such as http://192.168.1.20:5001")
        if not self.token:
            raise ConfigError("The worker token is missing. Set token in the [synchub] section (or SYNCHUB_TOKEN); add the worker on SyncHub's Workers page to get one.")
        if self.verify not in ("hash", "size"):
            raise ConfigError("verify must be 'hash' (read every copy back and compare checksums) or 'size'.")
        if self.video_encoder not in ("auto", "cpu", "nvenc", "qsv", "vaapi"):
            raise ConfigError("video_encoder must be auto, cpu, nvenc, qsv or vaapi.")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("log_level must be DEBUG, INFO, WARNING or ERROR.")
        return self

    @property
    def jobs_dir(self):
        return os.path.join(self.work_dir, "jobs")

    @property
    def originals_dir(self):
        return os.path.join(self.work_dir, "originals")

    @property
    def logs_dir(self):
        return os.path.join(self.work_dir, "logs")

    def masked_token(self):
        return (self.token[:3] + "..." + self.token[-4:]) if len(self.token) > 10 else "(set)" if self.token else "(missing)"


# ============================================================================ logging
class JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to read with a pager and easy to search with grep or jq."""

    def format(self, record):
        data = {"ts": iso(datetime.fromtimestamp(record.created, timezone.utc)), "level": record.levelname, "msg": record.getMessage()}
        data.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            data["error"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        fields = " ".join(f"{k}={v}" for k, v in (getattr(record, "fields", None) or {}).items() if k != "job")
        job = (getattr(record, "fields", None) or {}).get("job")
        line = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {'[job ' + str(job) + '] ' if job else ''}{record.getMessage()}{' ' + fields if fields else ''}"
        return line + ("\n" + self.formatException(record.exc_info) if record.exc_info else "")


class Log:
    """The agent's logger: log.info("message", key=value) puts the keys into the JSON line next to the message."""

    def __init__(self, logger):
        self._logger = logger

    @property
    def handlers(self):
        return self._logger.handlers

    def set_level(self, name):
        self._logger.setLevel(getattr(logging, name, logging.INFO))

    def log(self, level, message, *args, extra=None, exc_info=None, **fields):
        merged = dict((extra or {}).get("fields") or {})
        merged.update(fields)
        self._logger.log(level, message, *args, extra={"fields": merged}, exc_info=exc_info)

    def debug(self, message, *args, **fields):
        self.log(logging.DEBUG, message, *args, **fields)

    def info(self, message, *args, **fields):
        self.log(logging.INFO, message, *args, **fields)

    def warning(self, message, *args, **fields):
        self.log(logging.WARNING, message, *args, **fields)

    def error(self, message, *args, **fields):
        self.log(logging.ERROR, message, *args, **fields)

    def exception(self, message, *args, **fields):
        self.log(logging.ERROR, message, *args, exc_info=True, **fields)


def build_logger(cfg, console=True):
    logger = logging.getLogger("synchub_agent")
    logger.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    for old in list(logger.handlers):
        old.close()
        logger.removeHandler(old)
    logger.propagate = False
    try:
        os.makedirs(cfg.logs_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(os.path.join(cfg.logs_dir, "worker.jsonl"), maxBytes=cfg.log_max_mb * 1024 * 1024,
                                                       backupCount=cfg.log_backups, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    except OSError as exc:
        print(f"Warning: cannot write the log file in {cfg.logs_dir}: {exc}", file=sys.stderr)
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(ConsoleFormatter())
        logger.addHandler(stream)
    return Log(logger)


class JobLog:
    """A job's own log (JSON lines, in its folder), also copied into the worker log. Everything about one job is in one place."""
    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, folder, job_id, base):
        self.path, self.job_id, self.base, self._lock = os.path.join(folder, "job.log"), job_id, base, threading.Lock()

    def event(self, level, message, **fields):
        fields["job"] = self.job_id
        line = json.dumps({"ts": iso(), "level": logging.getLevelName(level), "msg": message, **fields}, default=str)
        with self._lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.MAX_BYTES:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass                                   # a log that cannot be written must never stop the job
        self.base.log(level, message, extra={"fields": fields})

    def info(self, message, **fields):
        self.event(logging.INFO, message, **fields)

    def warning(self, message, **fields):
        self.event(logging.WARNING, message, **fields)

    def error(self, message, **fields):
        self.event(logging.ERROR, message, **fields)

    def tail(self, lines=30):
        return "".join(self._read_lines()[-lines:])

    def _read_lines(self):
        out = []
        for name in (self.path + ".1", self.path):
            try:
                with open(name, "r", encoding="utf-8", errors="replace") as handle:
                    out.extend(handle.readlines())
            except OSError:
                continue
        return out

    def full(self, limit=900_000):
        """The whole log as text (the end of it when it is huge), and whether the start was left out."""
        text = "".join(self._read_lines())
        return (text[-limit:], True) if len(text) > limit else (text, False)


# ============================================================================ SyncHub API
def read_log_tail(logs_dir, limit=900_000, name="worker.jsonl"):
    """The end of the agent's log across its rotated files (worker.jsonl.1 is older than worker.jsonl): at most `limit` bytes of whole lines,
    and whether older lines were left out."""
    paths = [os.path.join(logs_dir, name)] + [os.path.join(logs_dir, f"{name}.{i}") for i in range(1, 100)]
    pieces, room, cut = [], limit, False
    for path in paths:
        try:
            with open(path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - room)
                handle.seek(start)
                data = handle.read()
        except OSError:
            continue
        if start > 0:
            data = data.partition(b"\n")[2]                                 # the first line is cut in half
            cut = True
        pieces.append(data)
        room -= len(data)
        if cut or room <= 0:
            break
    text = b"".join(reversed(pieces)).decode("utf-8", errors="replace")
    return text, cut


class ApiError(Exception):
    def __init__(self, status, message, body=None):
        super().__init__(f"{status}: {message}")
        self.status, self.message, self.body = status, message, body or {}


class Unreachable(Exception):
    """SyncHub did not answer (network down, SyncHub restarting, wrong address)."""


class Api:
    def __init__(self, cfg):
        self.base = cfg.url + "/api/worker/v1"
        self.token = cfg.token
        self.timeout = cfg.request_timeout
        # SyncHub is on the local network: a system proxy setting would only get in the way unless asked for
        self.opener = urllib.request.build_opener() if cfg.use_proxy else urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, path, body=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body or {}, default=str).encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                                                  "User-Agent": f"synchub-agent/{__version__}"})
        return self._send(request)

    def upload(self, path, filepath):
        """Sends a file as the raw body of a POST (a preview clip). The file is streamed, never read into memory."""
        with open(filepath, "rb") as handle:
            request = urllib.request.Request(self.base + path, data=handle, method="POST",
                                             headers={"Authorization": f"Bearer {self.token}", "Content-Type": "video/mp4", "Content-Length": str(os.path.getsize(filepath)),
                                                      "User-Agent": f"synchub-agent/{__version__}"})
            return self._send(request)

    def _send(self, request):
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8", "replace"))
            except ValueError:
                body = {}
            raise ApiError(exc.code, body.get("message") or exc.reason, body)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise Unreachable(str(getattr(exc, "reason", exc)))
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(502, "SyncHub sent something that is not JSON. Is the address pointing at SyncHub?")


# ============================================================================ shares
def check_root(path, text, write_test=False):
    """What is true of one share right now: mounted, the right one (its marker file), writable, free space.
    state is one of ok, not_mounted, no_marker, wrong_marker, read_only, error."""
    out = {"ok": False, "state": "error", "message": "", "free_bytes": None, "checked": iso()}
    try:
        if not os.path.isdir(path):
            out.update(state="not_mounted", message=f"{path} does not exist or is not a folder. Is the share mounted?")
            return out
        try:
            with open(os.path.join(path, MARKER), "r", encoding="utf-8", errors="replace") as handle:
                found = handle.read(256).strip()
        except FileNotFoundError:
            out.update(state="no_marker", message=f"{path} has no {MARKER} file. It may be an unmounted (empty) folder or not set up yet: mount the share, then run --init-roots.")
            return out
        if found != text:
            out.update(state="wrong_marker", message=f"{path} is marked '{found}' but should be '{text}': the wrong share is mounted here.")
            return out
        out["free_bytes"] = shutil.disk_usage(path).free
        if write_test:
            probe = os.path.join(path, f".synchub-write-test-{os.getpid()}")
            try:
                with open(probe, "wb") as handle:
                    handle.write(b"x")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.remove(probe)
            except OSError as exc:
                out.update(state="read_only", message=f"{path} cannot be written to: {exc.strerror or exc}")
                return out
        elif not os.access(path, os.W_OK):
            out.update(state="read_only", message=f"{path} is read-only for this user.")
            return out
        out.update(ok=True, state="ok", message="")
    except OSError as exc:
        out.update(state="error", message=f"{path}: {exc.strerror or exc}")
    return out


class Roots:
    """The shares SyncHub defines (name, mount point, marker) with this PC's own overrides for where they are mounted."""

    def __init__(self, overrides=None):
        self.overrides = dict(overrides or {})
        self.defs = {}
        self._lock = threading.Lock()

    def update(self, roots):
        with self._lock:
            self.defs = {r["name"]: r for r in roots or [] if isinstance(r, dict) and r.get("name")}

    def names(self):
        with self._lock:
            return sorted(self.defs)

    def path(self, name):
        with self._lock:
            d = self.defs.get(name)
        if name in self.overrides:
            return self.overrides[name]
        return d["mount_point"] if d else None

    def text(self, name):
        with self._lock:
            d = self.defs.get(name)
        return (d or {}).get("marker_text") or name


class RootMonitor(threading.Thread):
    """Checks every share in the background. A hung mount (a stale NFS handle, a dead SMB server) blocks any program that touches it,
    so each check runs in its own thread and a check that does not return is reported as 'not responding' instead of freezing the agent."""
    CHECK_SECONDS, WRITE_TEST_SECONDS, HANG_SECONDS = 15, 300, 20

    def __init__(self, roots, log):
        super().__init__(name="roots", daemon=True)
        self.roots, self.log, self.results, self._busy, self._last_write = roots, log, {}, {}, {}
        self._lock, self.wake, self.stopping = threading.Lock(), threading.Event(), threading.Event()

    def snapshot(self):
        with self._lock:
            return {k: dict(v) for k, v in self.results.items()}

    def _check(self, name, write_test):
        result = check_root(self.roots.path(name), self.roots.text(name), write_test)
        with self._lock:
            self.results[name] = result

    def check_all(self):
        now = time.monotonic()
        for name in self.roots.names():
            busy = self._busy.get(name)
            if busy is not None and busy.is_alive():
                self._store(name, {"ok": False, "state": "error", "message": f"{self.roots.path(name)} is not responding (a stale or hung mount?).", "free_bytes": None, "checked": iso()})
                continue
            write_test = now - self._last_write.get(name, -1e9) >= self.WRITE_TEST_SECONDS
            thread = threading.Thread(target=self._check, args=(name, write_test), name=f"check-{name}", daemon=True)
            thread.start()
            thread.join(self.HANG_SECONDS)
            if thread.is_alive():
                self._busy[name] = thread
                self._store(name, {"ok": False, "state": "error", "message": f"{self.roots.path(name)} did not answer within {self.HANG_SECONDS} s (a stale or hung mount?).", "free_bytes": None, "checked": iso()})
            else:
                self._busy.pop(name, None)
                if write_test:
                    self._last_write[name] = now
        for name in list(self.results):
            if name not in self.roots.names():
                with self._lock:
                    self.results.pop(name, None)

    def _store(self, name, result):
        with self._lock:
            self.results[name] = result

    def run(self):
        while not self.stopping.is_set():
            try:
                before = self.snapshot()
                self.check_all()
                after = self.snapshot()
                for name, result in after.items():
                    if before.get(name, {}).get("state") != result["state"]:
                        (self.log.info if result["ok"] else self.log.warning)(f"Share {name} is {result['state']}" + (f": {result['message']}" if result["message"] else ""),
                                                                              extra={"fields": {"root": name, "state": result["state"]}})
            except Exception:                           # the monitor must outlive anything odd it meets
                self.log.exception("The share check failed")
            self.wake.wait(self.CHECK_SECONDS)
            self.wake.clear()

    def require(self, name, wait=25.0):
        """Raises JobFailure unless the share is ready. Waits a little for the first check after a start; a result that is
        old (the monitor is stuck behind a hung mount) does not count."""
        deadline = time.monotonic() + wait
        result = self.snapshot().get(name)
        while result is None and time.monotonic() < deadline:
            time.sleep(0.2)
            result = self.snapshot().get(name)
        age = (utcnow() - (parse_iso((result or {}).get("checked")) or utcnow())).total_seconds()
        if result is None or age > 90 or not result["ok"]:
            reason = "it has not been checked yet" if result is None else "it was last checked a while ago and is not answering" if age > 90 else result["message"]
            raise JobFailure("transient", f"The share '{name}' is not ready: {reason}")


# ============================================================================ capabilities
def run_quiet(command, timeout=20):
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, text=True, errors="replace")
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""


# The video encoders worth knowing about, in the order SyncHub's Worker Status tab lists them. The agent tests the first group with a real frame;
# the rest are only reported as built into ffmpeg.
TESTED_ENCODERS = ("libx265", "libx264", "libsvtav1", "hevc_nvenc", "h264_nvenc", "av1_nvenc", "hevc_qsv", "h264_qsv", "av1_qsv", "hevc_vaapi", "h264_vaapi", "av1_vaapi")
OTHER_ENCODERS = ("libaom-av1", "librav1e", "libvpx-vp9", "hevc_amf", "h264_amf", "av1_amf", "hevc_videotoolbox", "h264_videotoolbox")



def media_duration(probe):
    """Seconds of a file from ffprobe's JSON, or None."""
    try:
        return float((probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None


VIDEO_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
PROFILE_CRF = {"hevc": (14, 35), "h264": (12, 32)}


def clean_profile(profile):
    """The conversion profile of a plan, checked. Everything that ends up in the ffmpeg command is validated here: no free text reaches it."""
    if not profile:
        return None
    if not isinstance(profile, dict):
        raise JobFailure("bad_job", "The conversion profile in this job is not readable.", retryable=False)
    codec, kbps = profile.get("video_codec") or "keep", profile.get("audio_kbps") or 192
    try:
        kbps = int(kbps)
        crf = int(profile["crf"]) if profile.get("crf") is not None else None
    except (TypeError, ValueError):
        raise JobFailure("bad_job", "The conversion profile has a quality or bitrate that is not a number.", retryable=False)
    if codec not in ("keep", "hevc", "h264") or not 64 <= kbps <= 640:
        raise JobFailure("bad_job", "The conversion profile has a codec or audio bitrate this agent does not accept.", retryable=False)
    out = {"video_codec": codec, "audio_kbps": kbps, "crf": None, "preset": None, "ten_bit": False, "name": str(profile.get("name") or "")[:60]}
    if codec != "keep":
        low, high = PROFILE_CRF[codec]
        preset = profile.get("preset") or "medium"
        if crf is None or not low <= crf <= high or preset not in VIDEO_PRESETS:
            raise JobFailure("bad_job", f"The conversion profile's quality or encoder speed is outside what this agent accepts for {codec}.", retryable=False)
        out.update(crf=crf, preset=preset, ten_bit=bool(profile.get("ten_bit")) and codec == "hevc")
    if codec == "hevc":
        out.update(clean_size_cap(profile))
        out["gpu"] = bool(profile.get("gpu"))
        out["vmaf_mode"] = profile.get("vmaf_mode") if profile.get("vmaf_mode") in ("off", "warn", "hold") else "off"
    return out


def _positive_int(value, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= high else None


_HDR_TEXT = re.compile(r"^[A-Za-z0-9(),.\-]{1,200}$")


def clean_size_cap(profile):
    """The optional size policy fields of a profile (a bitrate cap that keeps a constant-quality encode under the target, a size to check the result against,
    deinterlacing and the HDR facts to copy). Numbers and a short list of known words only: nothing here is free text that reaches ffmpeg."""
    out = {}
    maxrate = _positive_int(profile.get("maxrate_kbps"), 400_000)
    if maxrate:
        out.update(maxrate_kbps=maxrate, bufsize_kbps=_positive_int(profile.get("bufsize_kbps"), 800_000) or maxrate * 2)
    if _positive_int(profile.get("max_bytes"), 1 << 50):
        out["max_bytes"] = int(profile["max_bytes"])
    if profile.get("deinterlace"):
        out["deinterlace"] = True
    height = _positive_int(profile.get("max_height"), 4320)
    if height and height >= 240:
        out["max_height"] = height                       # the picture is scaled down to at most this height (never up)
    hdr = profile.get("hdr")
    if isinstance(hdr, dict) and hdr.get("kind") in ("hdr10", "hlg"):
        clean = {"kind": hdr["kind"]}
        for key in ("master_display", "max_cll"):
            if isinstance(hdr.get(key), str) and _HDR_TEXT.match(hdr[key]):
                clean[key] = hdr[key]
        out["hdr"] = clean
    return out


def hevc_extra_args(profile):
    """x265 parameters and colour tags for a size-capped or HDR HEVC encode, as the (-x265-params value, extra ffmpeg arguments)."""
    params, extra = ["log-level=error"], []
    if profile.get("maxrate_kbps"):
        params += [f"vbv-maxrate={profile['maxrate_kbps']}", f"vbv-bufsize={profile.get('bufsize_kbps') or profile['maxrate_kbps'] * 2}"]
    hdr = profile.get("hdr")
    if hdr:
        transfer = "arib-std-b67" if hdr["kind"] == "hlg" else "smpte2084"
        params += ["colorprim=bt2020", f"transfer={transfer}", "colormatrix=bt2020nc", "range=limited", "hdr10-opt=1" if hdr["kind"] == "hdr10" else "hdr10=0", "repeat-headers=1"]
        if hdr.get("master_display"):
            params.append(f"master-display={hdr['master_display']}")
        if hdr.get("max_cll"):
            params.append(f"max-cll={hdr['max_cll']}")
        extra += ["-color_primaries", "bt2020", "-color_trc", transfer, "-colorspace", "bt2020nc"]
    return ":".join(params), extra


def scale_filter(profile):
    """The ffmpeg filter that scales the picture down to the profile's `max_height` (never up, width follows the aspect ratio), or None."""
    height = (profile or {}).get("max_height")
    return f"scale=-2:'min({int(height)},ih)':flags=lanczos" if height else None


def video_arguments(video, profile):
    """The ffmpeg arguments for a video the plan says to re-encode. The profile chooses codec and quality; without one it is H.264 at CRF 20, as before."""
    codec = (profile or {}).get("video_codec") if (profile or {}).get("video_codec") not in (None, "keep") else (video.get("codec") or "h264")
    if codec == "hevc":
        pix = "yuv420p10le" if (profile or {}).get("ten_bit") or (profile or {}).get("hdr") else "yuv420p"
        params, extra = hevc_extra_args(profile or {})
        filters = (["yadif"] if (profile or {}).get("deinterlace") else []) + ([scale_filter(profile)] if scale_filter(profile) else [])
        yadif = ["-vf", ",".join(filters)] if filters else []
        return yadif + ["-c:v", "libx265", "-crf", str((profile or {}).get("crf") or 23), "-preset", (profile or {}).get("preset") or "medium", "-pix_fmt", pix, "-tag:v", "hvc1", "-x265-params", params] + extra
    return ["-c:v", "libx264", "-crf", str((profile or {}).get("crf") or 20), "-preset", (profile or {}).get("preset") or "medium", "-pix_fmt", "yuv420p"]


GPU_ENCODERS = {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "vaapi": "hevc_vaapi"}
GPU_ORDER = ("nvenc", "qsv", "vaapi")
NVENC_PRESETS = {"ultrafast": "p1", "superfast": "p2", "veryfast": "p3", "faster": "p3", "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7"}
QSV_PRESETS = {"ultrafast": "veryfast", "superfast": "veryfast", "veryfast": "veryfast", "faster": "faster", "fast": "fast", "medium": "medium", "slow": "slow", "slower": "slower", "veryslow": "veryslow"}


def choose_encoder(profile, working, preference="auto"):
    """'cpu', or the GPU kind (nvenc, qsv, vaapi) a plan that allows it should use on this worker. HDR stays on the CPU: the GPU encoders cannot write the
    mastering-display data that x265 does, and a size-capped HDR encode is the one that has to be right."""
    if not profile or not profile.get("gpu") or profile.get("video_codec") != "hevc" or profile.get("hdr") or preference == "cpu":
        return "cpu"
    for kind in (GPU_ORDER if preference == "auto" else (preference,)):
        if GPU_ENCODERS[kind] in (working or ()):
            return kind
    return "cpu"


def gpu_video_arguments(kind, profile, device):
    """(options before the input, options after it) for an HEVC encode on a GPU. The quality number is the profile's CRF used as the encoder's own constant-quality
    value; a size-capped encode (the size policy) is an average-bitrate encode at the target bitrate, because the GPU encoders cannot be held under a size any other way."""
    crf, ten = int(profile.get("crf") or 23), bool(profile.get("ten_bit"))
    preset, maxrate = profile.get("preset") or "medium", profile.get("maxrate_kbps")
    bufsize = profile.get("bufsize_kbps") or (maxrate or 0) * 2
    deinterlace = (["yadif"] if profile.get("deinterlace") else []) + ([scale_filter(profile)] if scale_filter(profile) else [])      # run on the CPU, before the picture goes to the card
    target = int(maxrate / MAXRATE_HEADROOM) if maxrate else None
    if kind == "nvenc":
        args = ["-c:v", "hevc_nvenc", "-preset", NVENC_PRESETS[preset], "-tune", "hq", "-rc", "vbr", "-cq", str(crf), "-b:v", f"{target}k" if target else "0"]
        args += ["-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k"] if maxrate else []
        args += ["-pix_fmt", "p010le" if ten else "yuv420p", "-tag:v", "hvc1"] + (["-profile:v", "main10"] if ten else [])
        return [], (["-vf", ",".join(deinterlace)] if deinterlace else []) + args
    chain = deinterlace + ["format=p010le" if ten else "format=nv12"]
    if kind == "qsv":
        args = ["-c:v", "hevc_qsv", "-preset", QSV_PRESETS[preset]] + (["-b:v", f"{target}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k"] if maxrate else ["-global_quality", str(crf)])
        return ["-init_hw_device", f"vaapi=va:{device}", "-init_hw_device", "qsv=hw@va", "-filter_hw_device", "hw"], \
            ["-vf", ",".join(chain + ["hwupload=extra_hw_frames=64"])] + args + ["-tag:v", "hvc1"] + (["-profile:v", "main10"] if ten else [])
    args = ["-c:v", "hevc_vaapi"] + (["-rc_mode", "VBR", "-b:v", f"{target}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k"] if maxrate else ["-rc_mode", "CQP", "-qp", str(crf)])
    return ["-vaapi_device", device], ["-vf", ",".join(chain + ["hwupload"])] + args + ["-tag:v", "hvc1"] + (["-profile:v", "main10"] if ten else [])


def video_plan(video, profile, encoder="cpu", device="/dev/dri/renderD128"):
    """(options before the input, video options) for re-encoding a video with `encoder` ('cpu' or a GPU kind)."""
    if encoder in GPU_ENCODERS:
        return gpu_video_arguments(encoder, profile, device)
    return [], video_arguments(video, profile)


AUDIO_ADD_CODECS = {"eac3": (64, 768), "ac3": (64, 640), "aac": (64, 640)}       # what an added compatible track may be, with its bitrate range in kbps


def clean_audio_add(change):
    """The compatible audio track a plan wants added (see synchub.library.audio): the source track to base it on, its codec, channels and bitrate. None when the plan has none.
    Checked here because it ends up in the ffmpeg command."""
    add = change.get("audio_add")
    if add is None:
        return None
    if not isinstance(add, dict) or add.get("codec") not in AUDIO_ADD_CODECS:
        raise JobFailure("bad_job", "The plan asks for a compatible audio track this agent does not know.", retryable=False)
    try:
        source, channels, kbps = int(add["from"]), int(add.get("channels") or 2), int(add.get("kbps") or 192)
    except (KeyError, TypeError, ValueError):
        raise JobFailure("bad_job", "The compatible audio track in this job has numbers that are not readable.", retryable=False)
    low, high = AUDIO_ADD_CODECS[add["codec"]]
    if source < 0 or not 1 <= channels <= 8 or not low <= kbps <= high:
        raise JobFailure("bad_job", "The compatible audio track in this job is outside what this agent accepts.", retryable=False)
    title = re.sub(r"[^A-Za-z0-9 .()+-]", "", str(add.get("title") or ""))[:60]
    return {"from": source, "codec": add["codec"], "channels": channels, "kbps": kbps, "title": title}


def _track_arguments(change, profile, n):
    """The -map options for the audio and subtitle tracks the plan keeps and the codec options for them, with the source as ffmpeg input number `n`.
    A plan with `audio_add` gets one more audio track, first and the default, made from one of the source's tracks."""
    keep_audio, keep_subtitle = change.get("keep_audio"), change.get("keep_subtitle")
    add = clean_audio_add(change) if isinstance(keep_audio, list) else None
    maps = ["-map", f"{n}:{add['from']}"] if add else []
    for indexes, everything in ((keep_audio, f"{n}:a?"), (keep_subtitle, f"{n}:s?")):
        maps += [part for i in indexes for part in ("-map", f"{n}:{int(i)}")] if isinstance(indexes, list) else ["-map", everything]
    codecs = ["-c:a", "copy", "-c:s", "copy"]
    shift = 1 if add else 0
    if add:
        codecs += ["-c:a:0", add["codec"], "-b:a:0", f"{add['kbps']}k", "-ac:a:0", str(add["channels"])] + (["-metadata:s:a:0", f"title={add['title']}"] if add["title"] else [])
    converting = change.get("audio_convert") or {}
    bitrate = (profile or {}).get("audio_kbps") or 192
    for position, index in enumerate(keep_audio if isinstance(keep_audio, list) else []):
        if str(index) in converting:
            entry = converting[str(index)]
            kbps = _positive_int(entry.get("kbps"), 768) or bitrate
            codecs += [f"-c:a:{position + shift}", entry.get("codec") or "aac", f"-b:a:{position + shift}", f"{kbps}k"]
            channels = _positive_int(entry.get("channels"), 8)
            if channels:
                codecs += [f"-ac:a:{position + shift}", str(channels)]
    default = change.get("default_audio")
    if add:
        codecs += ["-disposition:a", "0", "-disposition:a:0", "default"]
    elif default is not None and isinstance(keep_audio, list) and default in keep_audio:
        codecs += ["-disposition:a", "0", f"-disposition:a:{keep_audio.index(default)}", "default"]
    return maps, codecs


def _container_arguments(change, out):
    container = "mp4" if change.get("container") == "mp4" else "matroska"
    return ["-f", container] + (["-movflags", "+faststart"] if container == "mp4" else []) + [out]


def build_convert_command(ffmpeg, src, out, change, profile=None, encoder="cpu", device="/dev/dri/renderD128", chunk_list=None):
    """The ffmpeg command for a conversion plan (SyncHub's approved `change`: tracks to keep, codecs to convert, the default audio track).
    Anything the plan does not change is copied bit for bit. `profile` (see clean_profile) sets the video codec and quality and the audio bitrate.
    `encoder` is 'cpu' or a GPU kind (see choose_encoder). With `chunk_list` (a concat list of already encoded video chunks) the video is taken from those
    and everything else from `src`."""
    profile = clean_profile(profile)
    video = change.get("video") or {}
    base = [ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]
    if chunk_list:
        maps, codecs = _track_arguments(change, profile, 1)
        return base + ["-f", "concat", "-safe", "0", "-i", chunk_list, "-i", src, "-map_metadata", "1", "-map_chapters", "1", "-map", "0:v"] + maps + ["-c:v", "copy"] + codecs + _container_arguments(change, out)
    pre, video_args = video_plan(video, profile, encoder, device) if video.get("action") == "transcode" else ([], ["-c:v", "copy"])
    maps, codecs = _track_arguments(change, profile, 0)
    return base + pre + ["-i", src, "-map_metadata", "0", "-map_chapters", "0", "-map", "0:V?"] + maps + video_args + codecs + _container_arguments(change, out)


def build_chunk_command(ffmpeg, src, out, change, profile, encoder, device, start, length=None):
    """The ffmpeg command for one video-only chunk of a long encode: `length` seconds from `start` (to the end when length is None), same settings as every other chunk."""
    profile = clean_profile(profile)
    pre, video_args = video_plan(change.get("video") or {}, profile, encoder, device)
    window = ["-ss", f"{start:.3f}"] + (["-t", f"{length:.3f}"] if length else [])
    return [ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"] + pre + window + ["-i", src, "-map", "0:V?", "-an", "-sn", "-dn"] + video_args + ["-f", "matroska", out]


def dovi_extract_command(ffmpeg, src):
    """ffmpeg writing the first video stream of `src` as a raw HEVC stream on stdout, the form dovi_tool reads."""
    return [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", src, "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "pipe:1"]


def dovi_convert_command(dovi_tool, out):
    """dovi_tool reading that stream on stdin, converting the Dolby Vision data to profile 8.1 (mode 2) and dropping the enhancement layer."""
    return [dovi_tool, "-m", "2", "convert", "--discard", "-i", "-", "-o", out]


def mkvmerge_command(mkvmerge, video, fps, src, out):
    """mkvmerge joining the converted picture with everything else in `src` (audio, subtitles, chapters, attachments, tags), none of which is touched."""
    return [mkvmerge, "-o", out, "--default-duration", f"0:{fps}p", video, "--no-video", src]


MAXRATE_HEADROOM = 1.05              # the same cap as synchub.library.size_policy: maxrate is 5 % over the target video bitrate
VMAF_FLOORS = ((2000, 95.0), (0, 93.0))     # the lowest mean VMAF score that still counts as visually clean, by picture height (see docs/DESIGN_ENCODING.md 11.2)
VMAF_WINDOW_SECONDS = 20


def vmaf_floor(height):
    return next(floor for limit, floor in VMAF_FLOORS if (height or 0) >= limit)


def vmaf_windows(duration, count, seconds=VMAF_WINDOW_SECONDS):
    """(start, length) of `count` short windows spread over the video, skipping its first and last 5 %."""
    if not duration or duration <= 0:
        return []
    length = min(float(seconds), duration)
    if duration <= length * 2:
        return [(0.0, round(length, 3))]
    low, high = duration * 0.05, duration * 0.95 - length
    if count <= 1 or high <= low:
        return [(round((low + max(low, high)) / 2, 3), length)]
    return [(round(low + (high - low) * i / (count - 1), 3), length) for i in range(count)]


def vmaf_command(ffmpeg, converted, source, start, length, threads):
    """Scores one window of the converted file against the same window of the source. Both are scaled to at most 1080p, the size the VMAF model was trained for."""
    scale = "setpts=PTS-STARTPTS,scale=-2:'min(1080,ih)':flags=bicubic,format=yuv420p"
    graph = f"[0:v:0]{scale}[d];[1:v:0]{scale}[r];[d][r]libvmaf=n_threads={max(1, int(threads))}"
    return [ffmpeg, "-nostdin", "-hide_banner", "-v", "info", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", converted, "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", source,
            "-lavfi", graph, "-f", "null", "-"]


def parse_vmaf(text):
    found = re.findall(r"VMAF score[:=]\s*([0-9]+(?:\.[0-9]+)?)", text or "")
    return float(found[-1]) if found else None


PREVIEW_PROBE_SECONDS = 10         # how much of the film one "most demanding part" sample reads
PREVIEW_SAMPLES = 16
PREVIEW_COPY_LIMIT = 450 * 1024 ** 2     # a copied (unmodified) clip bigger than this is re-encoded at a high quality instead, so it stays under SyncHub's upload limit


def choose_window(duration, seconds, mode, seed, measure=None):
    """(start, length) in seconds of the part of a film to preview. 'random' picks a window away from the first and last 10 %; 'worst' samples the film and takes
    the stretch that needs the most bits (`measure(start)` returns the bytes of video in a short stretch from `start`)."""
    seconds = max(1.0, float(seconds))
    if duration <= seconds * 1.5:
        return 0.0, round(float(duration), 1)
    length = seconds
    low, high = duration * 0.10, duration * 0.90 - length
    if high <= low:
        return round(max(0.0, (duration - length) / 2), 1), float(length)
    if mode == "worst" and measure is not None:
        spots = [low + (high - low) * i / (PREVIEW_SAMPLES - 1) for i in range(PREVIEW_SAMPLES)]
        start = max(spots, key=lambda spot: measure(max(0.0, spot + length / 2 - PREVIEW_PROBE_SECONDS / 2)))
    else:
        start = random.Random(seed).uniform(low, high)
    return round(start, 1), float(length)


def segment_command(ffmpeg, src, out, start, length, audio_index=None):
    """Cuts a window out of the film without re-encoding: its first video track and one audio track (the planned default, else the first)."""
    audio = ["-map", f"0:{int(audio_index)}"] if audio_index is not None else ["-map", "0:a:0?"]
    return [ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}", "-map", "0:v:0", *audio,
            "-c", "copy", "-avoid_negative_ts", "make_zero", "-f", "matroska", out]


def preview_audio_index(change):
    keep, default = change.get("keep_audio"), change.get("default_audio")
    if isinstance(keep, list) and keep:
        return default if default in keep else keep[0]
    return default if isinstance(default, int) else None


def clip_command(ffmpeg, src, out, video_args):
    """An MP4 a browser can play: the given video arguments, one stereo AAC track, the index at the front."""
    return [ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", "-i", src, "-map", "0:v:0", "-map", "0:a:0?", *video_args,
            "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-sn", "-dn", "-movflags", "+faststart", "-f", "mp4", out]


def source_clip_arguments(codec, hdr, capped=False):
    """How the unmodified clip's picture is carried into the MP4: copied when MP4 can hold it, otherwise re-encoded at a high quality (HEVC for HDR so the colours stay)."""
    if codec in ("h264", "hevc") and not capped:
        return ["-c:v", "copy"] + (["-tag:v", "hvc1"] if codec == "hevc" else [])
    cap = {"maxrate_kbps": 40000, "bufsize_kbps": 80000}
    if hdr or codec == "hevc":
        return video_arguments({}, {"video_codec": "hevc", "crf": 14, "preset": "fast", "ten_bit": True, **({"hdr": hdr} if hdr else {}), **cap})
    return video_arguments({}, {"video_codec": "h264", "crf": 16, "preset": "fast"}) + (["-maxrate", "40M", "-bufsize", "80M"] if capped else [])


def parse_ffmpeg_progress(line, duration):
    """(fraction done, speed) from one line of `ffmpeg -progress pipe:1`; either is None when the line does not say."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms") and duration:
        try:
            return min(max(int(value) / 1_000_000 / duration, 0.0), 1.0), None
        except ValueError:
            return None, None
    if key == "speed":
        return None, value.strip().rstrip("x") if value.strip() not in ("", "N/A") else None
    return None, None


def detect_capabilities(cfg):
    """What this PC can do: ffmpeg and the encoders that really work (one test frame each), cores, space and the operations the agent knows,
    and (under "host") the hardware details the Worker Status tab shows."""
    caps = {"ops": list(OPS), "features": list(FEATURES), "cores": os.cpu_count(), "ffmpeg": None, "ffprobe": None, "encoders": [], "vmaf": False, "python": platform.python_version(),
            "analysis": media_analysis.LOGIC_VERSION if media_analysis else None}
    if media_analysis:
        caps["ops"].append("hash")
    caps["host"] = host_info()
    code, out = run_quiet([cfg.ffmpeg, "-hide_banner", "-version"])
    if code == 0:
        caps["ffmpeg"] = out.splitlines()[0] if out else "ffmpeg"
        code, listing = run_quiet([cfg.ffmpeg, "-hide_banner", "-encoders"])
        have = set(re.findall(r"^\s*V\S*\s+(\S+)", listing, re.M)) if code == 0 else set()
        for name in TESTED_ENCODERS:
            if name not in have:
                continue
            command = [cfg.ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10"]
            if name.endswith("_vaapi"):
                command = [cfg.ffmpeg, "-hide_banner", "-v", "error", "-vaapi_device", cfg.vaapi_device, "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10", "-vf", "format=nv12,hwupload"]
            elif name.endswith("_qsv"):
                command = [cfg.ffmpeg, "-hide_banner", "-v", "error", "-init_hw_device", f"vaapi=va:{cfg.vaapi_device}", "-init_hw_device", "qsv=hw@va", "-filter_hw_device", "hw",
                           "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10", "-vf", "format=nv12,hwupload=extra_hw_frames=64"]
            code, _ = run_quiet(command + ["-frames:v", "5", "-c:v", name, "-f", "null", "-"], timeout=30)
            if code == 0:
                caps["encoders"].append(name)
        code, hwaccels = run_quiet([cfg.ffmpeg, "-hide_banner", "-hwaccels"])
        caps["host"]["ffmpeg"] = {"encoders_built": [n for n in TESTED_ENCODERS + OTHER_ENCODERS if n in have],
                                  "hwaccels": [line.strip() for line in hwaccels.splitlines() if line.strip() and not line.startswith("Hardware")][:20] if code == 0 else []}
        code, filters = run_quiet([cfg.ffmpeg, "-hide_banner", "-filters"])
        caps["vmaf"] = code == 0 and "libvmaf" in filters
    code, out = run_quiet([cfg.ffprobe, "-version"])
    if code == 0:
        caps["ffprobe"] = out.splitlines()[0] if out else "ffprobe"
        if media_analysis and caps["ffmpeg"]:
            caps["ops"].extend(["scan", "convert", "preview"])          # a scan needs ffprobe to read files and ffmpeg to check them; a conversion also runs ffmpeg, then checks the result
            caps["features"].append("preview")
    if caps["ffmpeg"] and caps["ffprobe"] and media_analysis:
        dovi_code, _ = run_quiet([cfg.dovi_tool, "--version"])
        mkv_code, _ = run_quiet([cfg.mkvmerge, "--version"])
        if dovi_code == 0 and mkv_code == 0:
            caps["features"].append("dovi")                              # Dolby Vision profile 7 to 8.1 (see remux_dolby_vision)
    return caps


def os_name():
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


# ============================================================================ the host (SyncHub's Worker Status tab)
CPU_FLAGS = ("sse4_2", "avx", "avx2", "fma", "aes", "avx512f", "avx512bw", "avx512vl", "avx512_vnni", "neon", "asimd", "sve")     # the ones that matter to software encoders


def read_file(path, limit=2_000_000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def cpu_details():
    """The processor: model, cores, threads, the fastest clock it reports and the instruction sets worth knowing. Nothing here may raise."""
    try:
        threads = len(os.sched_getaffinity(0))                         # the cores this process may really use (a container can be limited)
    except (AttributeError, OSError):
        threads = os.cpu_count()
    cpu = {"model": None, "threads": threads, "cores": None, "mhz": None, "flags": []}
    text = read_file("/proc/cpuinfo")
    packages, cores, mhz, per_package, names = set(), set(), [], None, {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key in ("model name", "hardware", "cpu model", "model") and value and key not in names and not (key == "model" and value.isdigit()):
            names[key] = value                                          # x86 has a numeric "model" too; it is not a name
        elif key == "physical id":
            packages.add(value)
        elif key == "core id" and packages:
            cores.add((sorted(packages)[-1] if packages else "0", value))
        elif key == "cpu cores" and value.isdigit():
            per_package = int(value)
        elif key == "cpu mhz":
            try:
                mhz.append(float(value))
            except ValueError:
                pass
        elif key in ("flags", "features") and not cpu["flags"]:
            have = set(value.split())
            cpu["flags"] = [f for f in CPU_FLAGS if f in have]
    cpu["model"] = next((names[k] for k in ("model name", "hardware", "cpu model", "model") if k in names), None)
    cpu["cores"] = per_package * max(len(packages), 1) if per_package else (len(cores) or None)
    cpu["mhz"] = round(max(mhz)) if mhz else None
    if not cpu["model"]:
        code, out = run_quiet(["lscpu"], timeout=5)
        match = re.search(r"^Model name:\s*(.+)$", out, re.M) if code == 0 else None
        cpu["model"] = match.group(1).strip() if match else (platform.processor() or None)
    if cpu["cores"] and threads and cpu["cores"] > threads:
        cpu["cores"] = threads
    return cpu


def memory_bytes(field="MemTotal"):
    match = re.search(rf"^{field}:\s+(\d+)\s+kB", read_file("/proc/meminfo"), re.M)
    return int(match.group(1)) * 1024 if match else None


def gpu_details():
    """Graphics cards this process can see: NVIDIA ones from nvidia-smi (with driver and memory), others from lspci. A container sees only what was passed in."""
    gpus = []
    code, out = run_quiet(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"], timeout=10)
    if code == 0:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 3 and parts[0]:
                gpus.append({"name": parts[0][:80], "driver": parts[1][:30], "memory_mb": int(parts[2]) if parts[2].isdigit() else None, "source": "nvidia-smi"})
    code, out = run_quiet(["lspci", "-mm"], timeout=10)
    if code == 0:
        for line in out.splitlines():
            try:
                fields = shlex.split(line)
            except ValueError:
                continue
            if len(fields) >= 4 and any(k in fields[1] for k in ("VGA", "3D controller", "Display controller")):
                vendor, device = fields[2], fields[3]
                if gpus and "nvidia" in vendor.lower() and any(g["source"] == "nvidia-smi" for g in gpus):
                    continue                                          # nvidia-smi already told us, with more detail
                gpus.append({"name": f"{vendor} {device}"[:80], "driver": None, "memory_mb": None, "source": "lspci"})
    if not gpus:
        gpus = drm_gpus()
    return gpus[:8]


DRM_VENDORS = {"0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA"}


def drm_gpus(root="/sys/class/drm"):
    """Cards seen through /sys/class/drm, for a container that has no lspci: vendor and kernel driver of each render node that was passed in."""
    found = []
    try:
        names = sorted(n for n in os.listdir(root) if re.fullmatch(r"renderD\d+", n))
    except OSError:
        return found
    for node in names:
        base = os.path.join(root, node, "device")
        vendor = read_file(os.path.join(base, "vendor"), 40).strip().lower()
        driver = os.path.basename(os.path.realpath(os.path.join(base, "driver"))) if os.path.islink(os.path.join(base, "driver")) else None
        found.append({"name": f"{DRM_VENDORS.get(vendor, 'GPU')} graphics ({node})"[:80], "driver": driver, "memory_mb": None, "source": "drm"})
    return found


def container_kind():
    if os.path.exists("/.dockerenv"):
        return "docker"
    marks = read_file("/proc/1/cgroup", 20000)
    for kind in ("docker", "kubepods", "lxc", "containerd", "podman"):
        if kind in marks:
            return kind
    return None


def host_info():
    """The static hardware details sent once at registration. Any one detail that cannot be read is left out; this never raises."""
    try:
        try:
            render = sorted(n for n in os.listdir("/dev/dri") if n.startswith("renderD"))
        except OSError:
            render = []
        return {"arch": platform.machine(), "kernel": platform.release(), "cpu": cpu_details(), "memory_bytes": memory_bytes(), "gpus": gpu_details(),
                "render_devices": render, "container": container_kind()}
    except Exception:                                                  # noqa: B902  (a detail we cannot read must not stop the agent starting)
        return {}


def cpu_times():
    """(busy+idle, idle) jiffies since boot, from /proc/stat; None where there is no /proc/stat."""
    parts = read_file("/proc/stat", 4096).split("\n", 1)[0].split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        values = [int(v) for v in parts[1:9]]
    except ValueError:
        return None
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def live_host_stats(previous):
    """The numbers that change: load, how busy the CPU was since `previous`, free memory and how long the PC has been up. Returns (stats, sample)."""
    stats, sample = {}, cpu_times()
    try:
        stats["load"] = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        pass
    if sample and previous and sample[0] > previous[0]:
        stats["cpu_percent"] = round(100.0 * (1 - (sample[1] - previous[1]) / (sample[0] - previous[0])), 1)
    available = memory_bytes("MemAvailable")
    if available is not None:
        stats["mem_available_bytes"] = available
    up = read_file("/proc/uptime", 200).split()
    if up:
        try:
            stats["host_uptime_seconds"] = int(float(up[0]))
        except ValueError:
            pass
    return stats, sample


# ============================================================================ per-job state on disk
class JobStore:
    """<work>/jobs/<id>-<hash of what the job is>/ : manifest.json, job.log and the files. The hash keeps a folder from an old
    SyncHub database (whose job ids start again at 1) from being mistaken for a new job with the same id."""

    def __init__(self, cfg, log):
        self.root, self.cfg, self.log = cfg.jobs_dir, cfg, log
        os.makedirs(self.root, exist_ok=True)

    @staticmethod
    def spec_of(job):
        return {"kind": job.get("kind"), "plan": job.get("plan") or {}, "source": job.get("source"), "dest": job.get("dest")}

    def folder_name(self, job):
        digest = hashlib.sha1(json.dumps(self.spec_of(job), sort_keys=True).encode("utf-8")).hexdigest()[:8]
        return f"{int(job['id'])}-{digest}"

    def folder(self, job):
        return os.path.join(self.root, self.folder_name(job))

    def open(self, job, attempt):
        """The folder and manifest for a job, created if it is new. An existing one is reused: that is how a job resumes."""
        folder = self.folder(job)
        os.makedirs(folder, exist_ok=True)
        manifest = self.read(folder)
        if manifest is None:
            manifest = {"version": MANIFEST_VERSION, "job_id": int(job["id"]), "spec": self.spec_of(job), "title": job.get("title"), "created": iso(),
                        "stages": {}, "state": "running", "outcome": None, "report": None, "attempts": []}
        manifest.update(state="running", outcome=None, report=None, keep_until=None, stage=None, title=job.get("title"))
        manifest["attempts"] = (manifest.get("attempts") or [])[-9:] + [{"attempt": attempt, "started": iso()}]
        self.save(folder, manifest)
        return folder, manifest

    def read(self, folder):
        try:
            with open(os.path.join(folder, "manifest.json"), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) and data.get("version") == MANIFEST_VERSION else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self.log.warning(f"The manifest in {folder} is unreadable; that job starts again from the beginning", extra={"fields": {"folder": folder}})
            return None

    def save(self, folder, manifest):
        manifest["updated"] = iso()
        atomic_write_json(os.path.join(folder, "manifest.json"), manifest)

    def folders(self):
        """(job id, folder) for every job folder on disk."""
        out = []
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return out
        for name in names:
            m = re.match(r"^(\d+)-[0-9a-f]{8}$", name)
            if m and os.path.isdir(os.path.join(self.root, name)):
                out.append((int(m.group(1)), os.path.join(self.root, name)))
        return out

    def holding(self):
        return sorted({job_id for job_id, _ in self.folders()})

    def remove_dest_partial(self, manifest, log=None):
        """Deletes the half-written file a job left on a share (only ever a *.synchub-partial file), so nothing half-finished stays there."""
        path = (manifest or {}).get("dest_partial")
        if path and path.endswith(".synchub-partial") and os.path.isfile(path):
            try:
                os.remove(path)
                (log or self.log).info(f"Removed the half-written file {path}")
            except OSError as exc:
                self.log.warning(f"Could not remove the half-written file {path}: {exc}")

    def remove(self, folder):
        self.remove_dest_partial(self.read(folder))
        shutil.rmtree(folder, ignore_errors=True)

    def purge_files(self, folder):
        """Deletes the big files and keeps the manifest and log (they go with the folder at the end of its retention)."""
        for name in ("source", "output", "chunks", "preview"):
            shutil.rmtree(os.path.join(folder, name), ignore_errors=True)

    def sweep(self, active_ids):
        """Removes folders whose retention has passed. Returns how many."""
        removed = 0
        for job_id, folder in self.folders():
            if job_id in active_ids:
                continue
            manifest = self.read(folder)
            until = parse_iso((manifest or {}).get("keep_until"))
            if manifest is None:
                age = time.time() - os.path.getmtime(folder)
                expired = age > self.cfg.failed_retention_days * 86400
            else:
                expired = until is not None and until < utcnow()
            if expired:
                self.remove(folder)
                removed += 1
                self.log.info(f"Removed the folder of job {job_id} (retention over)", extra={"fields": {"job": job_id}})
        return removed


# ============================================================================ doing a job
class Cancelled(Exception):
    """SyncHub asked for the job to stop."""


class NeedsReview(Exception):
    """The job found something a person has to decide on (a low quality score). The files stay where they are until the answer comes back."""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail or {}


class JobLost(Exception):
    """SyncHub says the job is no longer this worker's."""


class Interrupted(Exception):
    """The agent is shutting down: leave the job as it is so it resumes later."""


class JobFailure(Exception):
    def __init__(self, error_class, message, retryable=None, keep_state=True):
        super().__init__(message)
        self.error_class, self.message, self.retryable, self.keep_state = error_class, message, retryable, keep_state


TRANSIENT_ERRNOS = {errno.ENOSPC, errno.EIO, errno.ESTALE, errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETUNREACH, errno.ENOTCONN,
                    errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT, errno.EREMOTEIO, errno.EDQUOT, errno.EAGAIN, errno.EINTR}


def classify(exc):
    """A JobFailure for an unexpected exception: what went wrong, in words, and whether a retry could help."""
    if isinstance(exc, JobFailure):
        return exc
    if isinstance(exc, FileNotFoundError):
        return JobFailure("source_changed", f"A file or folder is missing: {exc.filename or exc}. The file may have been moved or deleted.")
    if isinstance(exc, PermissionError):
        return JobFailure("permission_denied", f"Permission denied: {exc.filename or exc}. Check the share's user and the folder's permissions.", retryable=False)
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            return JobFailure("transient", f"The disk is full: {exc.filename or exc}")
        if exc.errno in TRANSIENT_ERRNOS or exc.errno is None:
            return JobFailure("transient", f"A file or network error: {exc.strerror or exc} ({exc.filename or 'no file'})")
        return JobFailure("transient", f"A file error: {exc.strerror or exc} ({exc.filename or 'no file'})")
    return JobFailure("agent_error", f"The agent hit an unexpected error: {type(exc).__name__}: {exc}", retryable=True)


class Reporter(threading.Thread):
    """Tells SyncHub how the job is going, from its own thread, so a slow or unreachable SyncHub never slows the copy.
    The report is also the heartbeat that keeps the job's lease alive, and the answer says whether the job was cancelled or taken away.
    If the job itself stops making progress for STALL_SECONDS (a hung mount), the heartbeat stops too, so SyncHub takes the job back."""
    INTERVAL, STALL_SECONDS = 2.0, 120.0

    def __init__(self, api, job_id, joblog, stop_event):
        super().__init__(name=f"report-{job_id}", daemon=True)
        self.api, self.job_id, self.log, self.stop_event = api, job_id, joblog, stop_event
        self.cancel = self.lost = False
        self._state, self._touched, self._lock = ("claimed", None, None, None), time.monotonic(), threading.Lock()
        self._wake, self._closed, self._warned = threading.Event(), False, 0.0

    def update(self, stage, progress=None, message=None, eta=None, now=False):
        with self._lock:
            self._state, self._touched = (stage, progress, message, eta), time.monotonic()
        if now:
            self._wake.set()

    def check(self):
        """Called by the job between blocks of work: raises if it should stop."""
        if self.lost:
            raise JobLost()
        if self.cancel:
            raise Cancelled()
        if self.stop_event.is_set():
            raise Interrupted()

    def close(self):
        self._closed = True
        self._wake.set()
        self.join(timeout=10)

    def run(self):
        failures = 0
        while not self._closed:
            self._wake.wait(self.INTERVAL if not failures else min(5.0 * failures, 30.0))
            self._wake.clear()
            if self._closed or self.lost:
                break
            with self._lock:
                stage, progress, message, eta = self._state
                idle = time.monotonic() - self._touched
            if idle > self.STALL_SECONDS:
                self._warn(f"No progress for {int(idle)} s: not sending heartbeats, so SyncHub can give the job to someone else")
                continue
            try:
                answer = self.api.post(f"/jobs/{self.job_id}/progress", {"stage": stage, "progress": None if progress is None else round(progress, 4), "message": message, "eta_seconds": eta})
                failures = 0
                if answer.get("cancel"):
                    self.cancel = True
            except ApiError as exc:
                if exc.status == 409 or exc.body.get("stop"):
                    self.lost = True
                else:
                    failures += 1
                    self._warn(f"SyncHub did not accept a progress report ({exc})")
            except Unreachable as exc:
                failures += 1
                self._warn(f"Cannot reach SyncHub ({exc}); carrying on and trying again")

    def _warn(self, text):
        if time.monotonic() - self._warned > 30:
            self._warned = time.monotonic()
            self.log.warning(text)


class JobRunner(threading.Thread):
    """Runs one job to its end, or until the agent stops. The manifest says where it is, so a new runner for the same job carries on."""

    def __init__(self, agent, job, folder, manifest):
        super().__init__(name=f"job-{job['id']}", daemon=False)
        self.agent, self.job, self.folder, self.m = agent, job, folder, manifest
        self.cfg, self.api = agent.cfg, agent.api
        self.log = JobLog(folder, job["id"], agent.log)
        self.reporter = Reporter(self.api, job["id"], self.log, agent.stopping)
        self.stage, self.progress = "claimed", 0.0

    # ---- bookkeeping
    def save(self):
        self.agent.store.save(self.folder, self.m)

    def stage_info(self, name):
        return self.m["stages"].setdefault(name, {})

    def report_progress(self, stage, fraction, message=None, eta=None, force=False):
        self.stage, self.progress = stage, fraction
        self.reporter.update(stage, fraction, message, eta, now=force)
        self.agent.note_progress(self.job["id"], stage, fraction)

    def guard(self, _nbytes=0):
        self.reporter.check()

    def throttle(self, started, done_bytes):
        """Keeps a copy under max_copy_mb_s (0 = no limit), so a big copy cannot take the whole network from whoever is watching a film."""
        limit = self.cfg.max_copy_mb_s
        if limit > 0:
            ahead = done_bytes / (limit * 1048576) - (time.monotonic() - started)
            if ahead > 0.001:
                time.sleep(min(ahead, 1.0))

    # ---- the run
    def run(self):
        job_id, final = self.job["id"], None
        self.log.info(f"Starting: {self.job.get('title') or self.job.get('kind')}", attempt=self.job.get("attempt"), plan=self.job.get("plan"), source=self.job.get("source"), dest=self.job.get("dest"))
        self.reporter.start()
        try:
            op = (self.job.get("plan") or {}).get("op")
            work = {"copy": self.do_copy, "scan": self.do_scan, "hash": self.do_hash, "convert": self.do_convert, "preview": self.do_preview, "rename": self.do_rename, "delete_source": self.do_delete_source, "originals": self.do_originals}.get(op)
            if work is None or (op in ("scan", "hash", "convert", "preview") and media_analysis is None):
                raise JobFailure("unsupported_job", f"This agent ({__version__}) cannot do '{op or self.job.get('kind')}' jobs. Update the agent"
                                 + (" (media_analysis.py must be next to synchub_agent.py)." if op in ("scan", "hash", "convert", "preview") else "."), retryable=False)
            final = ("complete", {"result": work()}, "done")
        except Interrupted:
            self.log.info("Stopping for a restart or shutdown; the job resumes from its last checkpoint", stage=self.stage)
            self.m["state"] = "running"
            self.save()
        except NeedsReview as exc:
            self.log.warning(f"Waiting for a decision: {exc.reason}", stage=self.stage)
            final = ("needs_review", {"reason": exc.reason, "detail": exc.detail, "keep_state": True}, "review")
        except JobLost:
            self.log.warning("SyncHub says this job is no longer ours; stopping and keeping the files", stage=self.stage)
            self.m["state"] = "lost"
            self.save()
        except Cancelled:
            self.log.info("Cancelled from SyncHub", stage=self.stage)
            self.agent.store.remove_dest_partial(self.m, self.log)
            final = ("fail", {"error_class": "cancelled", "message": "Cancelled.", "retryable": False, "keep_state": True}, "cancelled")
        except Exception as exc:                        # a failed job must always be reported, whatever went wrong
            failure = classify(exc)
            if not isinstance(exc, (JobFailure, OSError)):
                self.agent.log.exception("Unexpected error in job %s", job_id)
            self.log.error(failure.message, stage=self.stage, error_class=failure.error_class)
            final = ("fail", {"error_class": failure.error_class, "message": failure.message, "retryable": failure.retryable, "keep_state": failure.keep_state,
                              "log_tail": self.log.tail(30)}, "failed")
        finally:
            self.reporter.close()                       # no progress report may arrive after the final one
        try:
            if final:
                self.finish(*final)
        finally:
            self.agent.runner_done(job_id)

    def finish(self, kind, payload, outcome):
        """Saves the report first, then tries to send it. A report that cannot be sent now is sent later."""
        self.m.update(state="finished", outcome=outcome, report={"kind": kind, "payload": payload, "sent": False}, finished=iso(), stage=self.stage)
        keep = self.cfg.failed_retention_days * 86400
        self.m["keep_until"] = iso(utcnow() + timedelta(seconds=keep)) if outcome in ("failed", "cancelled", "review") else None
        self.save()
        self.agent.deliver(self.job["id"], self.folder)

    # ---- reports SyncHub must accept
    def tell(self, path, body, patience=300.0, poster=None):
        """A batch of results for this job. Retried through a short outage (and a busy SyncHub); never silently dropped."""
        deadline, delay = time.monotonic() + patience, 2.0
        while True:
            self.reporter.check()
            try:
                return (poster or self.api.post)(f"/jobs/{self.job['id']}{path}", body)
            except ApiError as exc:
                if exc.status == 409 or exc.body.get("stop"):
                    raise JobLost()
                if 400 <= exc.status < 500 and exc.status not in (401, 403, 426):
                    raise JobFailure("bad_job", f"SyncHub refused the report: {exc.message}", retryable=False)
                problem = f"SyncHub answered {exc.status}"
            except Unreachable as exc:
                problem = f"cannot reach SyncHub ({exc})"
            if time.monotonic() > deadline:
                raise JobFailure("transient", f"Gave up after {int(patience)} s: {problem}")
            self.log.warning(f"{problem}; trying again in {int(delay)} s", stage=self.stage)
            end = time.monotonic() + delay
            while time.monotonic() < end:
                self.reporter.check()
                time.sleep(0.25)
            delay = min(delay * 2, 30.0)

    def media_settings(self, plan):
        def note(level, message):
            self.log.event(getattr(logging, level, logging.INFO), message, stage=self.stage)
        return media_analysis.Settings(ffprobe=self.cfg.ffprobe, ffmpeg=self.cfg.ffmpeg, integrity_mode=plan.get("integrity") or "tail", tail_seconds=plan.get("tail_seconds") or 30,
                                       retry_delay=self.cfg.scan_retry_seconds, check=self.reporter.check, log=note)

    def folder_in(self, root, rel=""):
        self.agent.monitor.require(root)
        path = self.agent.roots.path(root)
        if rel:
            try:
                path = safe_path(path, rel)
            except ValueError as exc:
                raise JobFailure("bad_job", str(exc), retryable=False)
        return path

    # ---- the scan: list the files, say which need looking at, analyse those, report
    def do_scan(self):
        plan, ma = self.job["plan"], media_analysis
        if plan.get("logic_version") != ma.LOGIC_VERSION:
            raise JobFailure("unsupported_job", f"This agent's analysis (version {ma.LOGIC_VERSION}) differs from SyncHub's ({plan.get('logic_version')}). Update the agent.", retryable=False)
        root, prefix = plan["root"], plan.get("prefix") or ""
        base = self.folder_in(root, prefix)
        if not os.path.isdir(base):
            raise JobFailure("bad_job", f"The folder {prefix or '(top)'} does not exist in the share {root}.", retryable=False)
        settings, rules = self.media_settings(plan), ma.ExcludeRules(plan.get("exclude"), root)
        where = f"{root}/{prefix}" if prefix else root
        self.report_progress("scan", 0.0, f"Looking for video files in {where}", force=True)
        files, unreadable, walk_errors = ma.walk_inventory(
            settings, self.agent.roots.path(root), prefix, rules, on_skip=lambda d: self.log.info("Skipping a folder that is left out", folder=d),
            progress=lambda n: self.report_progress("scan", 0.0, f"Looking for video files in {where}: {n} found so far"))
        self.log.info(f"Found {len(files)} video file(s)", unreadable=len(unreadable), walk_errors=len(walk_errors), folder=where)
        if walk_errors:
            self.log.warning("Some folders could not be listed; files in them are not treated as missing", errors=walk_errors[:5])
        need, step = set(), 1000
        for start in range(0, max(len(files), 1), step):
            batch = files[start:start + step]
            answer = self.tell("/scan/inventory", {"files": batch, "unreadable": unreadable if start == 0 else []})
            need.update(answer.get("need") or [])
            self.report_progress("scan", 0.02, f"Compared {min(start + step, len(files))} of {len(files)} files with what SyncHub knows")
        todo = [f for f in files if f["rel"] in need]
        self.log.info(f"{len(todo)} of {len(files)} file(s) need analysing", skipped=len(files) - len(todo))
        analysed = self.analyse_files(settings, root, todo)
        summary = self.tell("/scan/finish", {"complete": True, "walk_errors": walk_errors, "files_found": len(files)})
        result = {"op": "scan", "files": len(files), "analysed": analysed, "unchanged": len(files) - len(todo), "unreadable": len(unreadable), "walk_errors": len(walk_errors),
                  "duplicate_groups": summary.get("groups")}
        self.log.info("Scan finished", **{k: v for k, v in result.items() if k != "op"})
        return result

    def analyse_files(self, settings, root, todo):
        """Analyses the files that need it, a few at a time, and reports them in batches as they finish. Returns how many were analysed."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        root_path = self.agent.roots.path(root)

        def one(item):
            path = safe_path(root_path, item["rel"])
            try:
                stat = os.stat(path)
            except OSError as exc:
                self.log.warning("A file vanished before it could be analysed", rel=item["rel"], error=str(exc))
                return None
            record = media_analysis.analyze_file(settings, item["rel"], path, stat.st_size, stat.st_mtime)
            record["rel"] = item["rel"]
            return record

        batch, done, started, last_flush = [], 0, time.monotonic(), time.monotonic()

        def flush():
            nonlocal batch, last_flush
            if batch:
                answer = self.tell("/scan/records", {"records": batch})
                if answer.get("failed"):
                    self.log.warning("SyncHub could not store some records", failed=answer["failed"])
                batch = []
            last_flush = time.monotonic()

        pool = ThreadPoolExecutor(max_workers=self.cfg.scan_threads)
        futures = [pool.submit(one, item) for item in todo]
        try:
            for future in as_completed(futures):
                self.reporter.check()
                record = future.result()
                done += 1
                if record is not None:
                    batch.append(record)
                elapsed = max(time.monotonic() - started, 0.001)
                eta = int((len(todo) - done) * elapsed / done)
                name = record["file_name"] if record else "(gone)"
                self.report_progress("scan", 0.05 + 0.9 * done / len(todo), f"Analysing {done} of {len(todo)}: {name}", eta)
                if len(batch) >= self.cfg.scan_batch or time.monotonic() - last_flush > 20:
                    flush()
            flush()
        except BaseException:
            for pending in futures:
                pending.cancel()
            raise
        finally:
            pool.shutdown(wait=True)
        return done

    # ---- reading files to find exact duplicates
    def do_hash(self):
        plan = self.job["plan"]
        for root in plan.get("roots") or []:
            self.agent.monitor.require(root)
        groups = plan.get("groups") or []
        settings = self.media_settings(plan)
        total = max(sum(f["size"] for g in groups for f in g["files"]), 1)
        state = {"bytes": 0, "files": 0, "started": time.monotonic()}

        def progress(n):
            state["bytes"] += n
            elapsed = max(time.monotonic() - state["started"], 0.001)
            rate = state["bytes"] / elapsed
            self.report_progress("scan", min(state["bytes"] / total, 0.99), f"Reading files to compare them: {human_bytes(state['bytes'])} of {human_bytes(total)} at {human_bytes(rate)}/s",
                                 int((total - state["bytes"]) / rate) if rate else None)

        def result(item):
            state["files"] += 1
            self.tell("/scan/hashes", {"results": [item]})

        self.report_progress("scan", 0.0, f"Reading {sum(len(g['files']) for g in groups)} files to compare them", force=True)
        for group in groups:
            present = []
            for item in group["files"]:
                try:
                    path = safe_path(self.agent.roots.path(item["root"]), item["rel"])
                    stat = os.stat(path)
                except (ValueError, OSError) as exc:
                    self.log.warning("A file could not be read for the comparison", rel=item["rel"], error=str(exc))
                    self.tell("/scan/hashes", {"results": [{"id": item["id"], "size": item["size"], "mtime": item["mtime"], "error": f"Could not read the file: {exc}"}]})
                    continue
                present.append({"id": item["id"], "path": path, "size": stat.st_size, "mtime": stat.st_mtime})
            same = [f for f in present if f["size"] == group["size"]]
            for changed in (f for f in present if f["size"] != group["size"]):
                self.tell("/scan/hashes", {"results": [{"id": changed["id"], "size": changed["size"], "mtime": changed["mtime"], "error": "The file changed since the search was planned"}]})
            if same:
                media_analysis.hash_group(settings, same, group.get("known") or 0, progress=progress, on_result=result)
        summary = self.tell("/scan/finish", {})
        outcome = {"op": "hash", "files": state["files"], "groups": summary.get("groups")}
        self.log.info("Duplicate search finished", **{k: v for k, v in outcome.items() if k != "op"})
        return outcome

    # ---- the copy job: copy in, copy out, verified, never overwriting
    def endpoints(self, what):
        """The paths of this job's source file and destination file on this PC, after checking that both shares are known and mounted."""
        source, dest = self.job.get("source"), self.job.get("dest")
        if not source or not dest:
            raise JobFailure("bad_job", f"A {what} job needs a source and a destination.", retryable=False)
        monitor = self.agent.monitor
        for root in (source["root"], dest["root"]):
            if self.agent.roots.path(root) is None:
                raise JobFailure("transient", f"SyncHub's share '{root}' is not known to this agent yet. It re-reads the list of shares when it starts.")
            monitor.require(root)
        try:
            return safe_path(self.agent.roots.path(source["root"]), source["rel"]), safe_path(self.agent.roots.path(dest["root"]), dest["rel"])
        except ValueError as exc:
            raise JobFailure("bad_job", str(exc), retryable=False)

    def do_copy(self):
        source = self.job.get("source")
        src, dst = self.endpoints("copy")
        stat = os.stat(src)
        if not os.path.isfile(src):
            raise JobFailure("source_changed", f"The source is not a file: {source['rel']}")
        size, mtime = stat.st_size, int(stat.st_mtime)
        name = os.path.basename(src)
        local_dir = os.path.join(self.folder, "source")
        local = os.path.join(local_dir, name)
        t0 = time.monotonic()
        self.m["source"] = {"size": size, "mtime": mtime, "name": name}
        in_seconds = self.copy_in(src, local, size, mtime)
        digest = self.stage_info("copy_in")["sha256"]
        out_seconds = self.copy_out(local, dst, size, digest, stat.st_mtime_ns)
        elapsed = time.monotonic() - t0
        result = {"op": "copy", "bytes": size, "sha256": digest, "verified": "sha256" if self.cfg.verify == "hash" else "size", "seconds": round(elapsed, 1),
                  "copy_in_mb_s": round(size / 1048576 / in_seconds, 1) if in_seconds else None, "copy_out_mb_s": round(size / 1048576 / out_seconds, 1) if out_seconds else None}
        self.log.info(f"Copied {human_bytes(size)} and verified it ({result['verified']}) in {human_duration(elapsed)}", **{k: v for k, v in result.items() if k != "op"})
        return result

    # ---- rename: move a file to its proper place inside one share. No copy, nothing in the file changes.
    SIDECAR = r"(\.[A-Za-z]{2,3}(-[A-Za-z]{2,4})?)?(\.(forced|sdh|hi|cc|default))*\.(srt|ass|ssa|sub|idx|vtt|nfo)"

    def sidecars(self, src, dst):
        """[(from, to)] for the subtitle and info files that sit next to the video and carry its name (Heat.mkv, Heat.en.srt, Heat.nfo)."""
        folder, stem = os.path.dirname(src), os.path.splitext(os.path.basename(src))[0]
        pattern = re.compile("^" + re.escape(stem) + "(" + self.SIDECAR + ")$", re.I)
        new_stem, found = os.path.splitext(os.path.basename(dst))[0], []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return found
        for name in names:
            match = pattern.match(name)
            if match and os.path.isfile(os.path.join(folder, name)):
                found.append((os.path.join(folder, name), os.path.join(os.path.dirname(dst), new_stem + match.group(1))))
        return found

    def prune_empty(self, folder, root_path):
        """Removes the folders a move left empty, up to (never including) the share itself."""
        root_path = os.path.realpath(root_path)
        folder = os.path.realpath(folder)
        while folder != root_path and folder.startswith(root_path + os.sep):
            try:
                os.rmdir(folder)
            except OSError:
                return
            folder = os.path.dirname(folder)

    def do_rename(self):
        source, dest = self.job["source"], self.job["dest"]
        src, dst = self.endpoints("rename")
        if source["root"] != dest["root"]:
            raise JobFailure("bad_job", "A rename stays inside one share.", retryable=False)
        plan = self.job["plan"]
        if not os.path.exists(src):
            if os.path.isfile(dst) and plan.get("source_size") in (None, os.path.getsize(dst)):
                self.log.info("The file was already renamed")
                return {"op": "rename", "bytes": os.path.getsize(dst), "verified": "size", "already_done": True}
            raise JobFailure("source_changed", f"The file is not at {source['rel']} any more.")
        stat = os.stat(src)
        if not os.path.isfile(src) or (plan.get("source_size") is not None and stat.st_size != plan["source_size"]) or abs(stat.st_mtime - float(plan.get("source_mtime") or stat.st_mtime)) > 2:
            raise JobFailure("source_changed", f"{source['rel']} is not the file SyncHub analysed (its size or date differs). It will be looked at again.")
        if src == dst:
            return {"op": "rename", "bytes": stat.st_size, "verified": "size", "unchanged": True}
        case_only = src.casefold() == dst.casefold()
        if os.path.exists(dst) and not case_only:
            raise JobFailure("dest_exists", f"{dest['rel']} already exists, so nothing was renamed.", retryable=False)
        self.report_progress("publish", 0.2, "Renaming", force=True)
        pairs = self.sidecars(src, dst)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if case_only:                                   # a share that ignores case needs a step in between
                os.rename(src, dst + ".synchub-rename")
                os.rename(dst + ".synchub-rename", dst)
            else:
                os.rename(src, dst)
            moved = 0
            for old, new in pairs:
                if not os.path.exists(new):
                    os.rename(old, new)
                    moved += 1
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise JobFailure("bad_job", "The two paths are on different disks, so this cannot be a rename.", retryable=False)
            raise
        self.prune_empty(os.path.dirname(src), self.agent.roots.path(source["root"]))
        self.report_progress("publish", 1.0, "Renamed", force=True)
        self.log.info(f"Renamed {source['rel']} to {dest['rel']}" + (f" with {moved} subtitle/info file(s)" if moved else ""))
        return {"op": "rename", "bytes": stat.st_size, "verified": "size", "sidecars": moved}

    # ---- delete the source of a file that was copied into the library, after checking the library copy is still whole
    def do_delete_source(self):
        source, dest = self.job["source"], self.job["dest"]
        src, dst = self.endpoints("delete_source")
        plan = self.job["plan"]
        if source["root"] == dest["root"] and source["rel"].casefold() == dest["rel"].casefold():
            raise JobFailure("bad_job", "The source and the library copy are the same file, so nothing was deleted.", retryable=False)
        if not os.path.isfile(dst):
            raise JobFailure("verification_failed", f"The library copy {dest['rel']} is not there, so the source was kept.")
        size = os.path.getsize(dst)
        if plan.get("dest_size") is not None and size != plan["dest_size"]:
            raise JobFailure("verification_failed", f"The library copy is {size} bytes but {plan['dest_size']} were verified, so the source was kept.")
        if plan.get("dest_sha256"):
            self.report_progress("verify", 0.1, "Checking the library copy", force=True)
            if hash_file(dst, check=self.guard) != plan["dest_sha256"]:
                raise JobFailure("verification_failed", "The library copy no longer matches what was verified, so the source was kept.")
        if not os.path.exists(src):
            self.log.info("The source was already gone")
            return {"op": "delete_source", "already_gone": True}
        stat = os.stat(src)
        if (plan.get("source_size") is not None and stat.st_size != plan["source_size"]) or abs(stat.st_mtime - float(plan.get("source_mtime") or stat.st_mtime)) > 2:
            raise JobFailure("source_changed", f"{source['rel']} is not the file that was copied (its size or date differs), so it was kept.")
        self.report_progress("publish", 0.9, "Deleting the source", force=True)
        os.remove(src)
        self.prune_empty(os.path.dirname(src), self.agent.roots.path(source["root"]))
        self.log.info(f"Deleted the source {source['rel']} ({human_bytes(stat.st_size)}); the library copy is verified")
        return {"op": "delete_source", "bytes": stat.st_size, "deleted": True}

    # ---- the originals kept after a file was replaced: restore one, keep one longer, or delete one now (docs/DESIGN_ENCODING.md section 9)
    def do_originals(self):
        plan, name = self.job["plan"], str(self.job["plan"].get("original") or "")
        action = plan.get("action")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", name) or name in (".", ".."):
            raise JobFailure("bad_job", "That is not the name of a kept original.", retryable=False)
        folder = os.path.join(self.cfg.originals_dir, name)
        try:
            with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            if action == "purge":
                return {"op": "originals", "action": action, "already_gone": True}
            raise JobFailure("source_changed", "This worker no longer holds that original (it was deleted or its time ran out).", retryable=False)
        if action == "purge":
            shutil.rmtree(folder, ignore_errors=True)
            self.log.info(f"Deleted the kept original {meta.get('rel')} on request")
            return {"op": "originals", "action": action, "bytes": meta.get("size"), "deleted": True}
        if action == "extend":
            try:
                days = int(plan.get("days"))
            except (TypeError, ValueError):
                days = 0
            if not 1 <= days <= 3650:
                raise JobFailure("bad_job", "Keep for between 1 and 3650 days.", retryable=False)
            meta.update(expires_at=iso(utcnow() + timedelta(days=days)), retention_days=days)
            atomic_write_json(os.path.join(folder, "meta.json"), meta)
            self.log.info(f"Keeping {meta.get('rel')} for another {days} day(s)")
            return {"op": "originals", "action": action, "expires_at": meta["expires_at"]}
        if action == "restore":
            return self.restore_original(folder, meta)
        raise JobFailure("bad_job", "Unknown action for a kept original.", retryable=False)

    def restore_original(self, folder, meta):
        """Puts a kept original back on its share under its old name. The converted file that replaced it is moved aside (never deleted) into its own kept folder, so
        nothing is lost whichever of the two turns out to be wanted. The copy is checked against the checksum recorded when the original was kept."""
        if (meta.get("kind") or "original") != "original":
            raise JobFailure("bad_job", "This is a converted copy set aside by an earlier restore; it cannot be restored.", retryable=False)
        src, dst = self.endpoints("restore")                     # src: the converted file on the share, dst: where the original goes
        kept = os.path.join(folder, str(meta.get("name") or ""))
        if not os.path.isfile(kept) or os.path.getsize(kept) != meta.get("size"):
            raise JobFailure("source_changed", "The kept original is missing or incomplete on this worker, so nothing was restored.", retryable=False)
        if os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(src):
            raise JobFailure("dest_exists", f"{self.job['dest']['rel']} already exists, so the original was not restored over it.", retryable=False)
        temp = dst + ".synchub-restore"
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        digest, size, done = hashlib.sha256(), meta["size"], 0
        self.report_progress("copy_out", 0.0, "Copying the original back", force=True)
        try:
            with open(kept, "rb") as fin, open(temp, "wb") as fout:
                while True:
                    block = fin.read(self.cfg.copy_chunk_mb * 1048576)
                    if not block:
                        break
                    fout.write(block)
                    digest.update(block)
                    done += len(block)
                    self.guard(len(block))
                    self.report_progress("copy_out", 0.9 * done / max(size, 1), f"Copying the original back: {done * 100 // max(size, 1)}%")
                fout.flush()
                os.fsync(fout.fileno())
            if meta.get("sha256") and digest.hexdigest() != meta["sha256"]:
                raise JobFailure("verification_failed", "The restored copy does not match the checksum recorded when the original was kept, so nothing was changed.", retryable=False)
            aside = None
            if os.path.isfile(src):
                aside = folder + "-converted"
                os.makedirs(aside, exist_ok=True)
                shutil.move(src, os.path.join(aside, os.path.basename(src)))
            os.replace(temp, dst)
        except BaseException:
            self._remove(temp)
            raise
        if aside:
            days = 7
            atomic_write_json(os.path.join(aside, "meta.json"), {"kind": "converted", "job_id": self.job["id"], "root": self.job["source"]["root"], "rel": self.job["source"]["rel"],
                                                                "name": os.path.basename(src), "size": os.path.getsize(os.path.join(aside, os.path.basename(src))), "saved_at": iso(),
                                                                "retention_days": days, "expires_at": iso(utcnow() + timedelta(days=days))})
        shutil.rmtree(folder, ignore_errors=True)
        self.report_progress("copy_out", 1.0, "Restored", force=True)
        self.log.info(f"Restored {self.job['dest']['rel']}" + (f"; the converted file was set aside for {days} days" if aside else ""))
        return {"op": "originals", "action": "restore", "bytes": size, "sha256": digest.hexdigest(), "converted_set_aside": bool(aside)}

    # ---- a conversion: copy in, run ffmpeg on the local copy, check the result, copy the result out to the destination share
    CONVERT_BANDS = {"copy_in": (0.0, 0.25), "copy_out": (0.85, 0.95), "verify": (0.95, 1.0)}      # the encode itself is 25 - 85 %

    def do_convert(self):
        src, dst = self.endpoints("convert")
        plan = self.job["plan"]
        change = plan.get("change") or {}
        replace = bool(plan.get("replace"))
        if replace and self.stage_info("copy_out").get("done") and self.m.get("replace_result"):
            self.log.info("The file was already replaced; only the report is left")
            self.remove_replaced_source(src, dst)
            return self.m["replace_result"]
        stat = os.stat(src)
        if not os.path.isfile(src):
            raise JobFailure("source_changed", f"The source is not a file: {self.job['source']['rel']}")
        if replace and (stat.st_size != plan.get("source_size") or abs(stat.st_mtime - float(plan.get("source_mtime") or 0)) > 2):
            raise JobFailure("source_changed", f"{self.job['source']['rel']} is not the file SyncHub analysed (its size or date differs). It will be looked at again.")
        size, mtime, name = stat.st_size, int(stat.st_mtime), os.path.basename(src)
        local = os.path.join(self.folder, "source", name)
        converted = os.path.join(self.folder, "output", os.path.basename(dst))
        self.BANDS = self.CONVERT_BANDS
        t0 = time.monotonic()
        self.m["source"] = {"size": size, "mtime": mtime, "name": name}
        self.copy_in(src, local, size, mtime)
        done = self.stage_info("encode")
        if not (done.get("done") and os.path.isfile(converted) and os.path.getsize(converted) == done.get("bytes")):
            done.clear()
            if (change.get("video") or {}).get("dv") == "p8_1":
                done.update(self.remux_dolby_vision(local, converted, plan.get("duration")))
            else:
                done.update(self.encode(local, converted, change, self.job["plan"].get("duration"), plan.get("profile")))
            self.save()
        quality = self.quality_check(local, converted, change, plan.get("profile"))
        extra = self.make_extra(local, plan)                           # a second output of the same job: made before the main file is swapped in, so a failure here costs nothing
        out_size = os.path.getsize(converted)
        if not done.get("sha256"):
            done["sha256"] = hash_file(converted, check=self.guard)
            self.save()
        keep = (lambda: self.keep_original(src, local, size, mtime, plan)) if replace else None
        out_seconds = self.copy_out(converted, dst, out_size, done["sha256"], stat.st_mtime_ns, overwrite=src if replace else None, before_swap=keep)
        extra_result = self.deliver_extra(plan, extra, stat.st_mtime_ns) if extra is not None else None
        elapsed = time.monotonic() - t0
        result = {"op": "convert", "bytes": out_size, "source_bytes": size, "sha256": done["sha256"], "verified": "sha256" if self.cfg.verify == "hash" else "size",
                  "seconds": round(elapsed, 1), "encode_seconds": done.get("seconds"), "command": done.get("command"), "output": done.get("probe"),
                  "encoder": done.get("encoder"), "gpu_fallback": done.get("fallback"), "chunked": done.get("chunked"), "vmaf": quality}
        if extra_result is not None:
            result["extra"] = extra_result
        if replace:
            original = self.m.get("original") or {}
            result.update(replaced=True, kept_original=bool(original.get("path")), original_expires=original.get("expires_at"), path_changed=dst != src)
            self.m["replace_result"] = result
            self.save()
            self.remove_replaced_source(src, dst)
            self.log.info(f"Replaced {name} in place ({human_bytes(size)} to {human_bytes(out_size)}); " + (f"the original is kept until {original['expires_at']}" if original.get("path") else "the original was not kept"))
            return result
        self.log.info(f"Converted {name} ({human_bytes(size)} to {human_bytes(out_size)}) and brought it into the destination in {human_duration(elapsed)}")
        return result

    # ---- the second output of a job (a smaller copy made in the same job as a Dolby Vision conversion)
    def make_extra(self, local, plan):
        """Encodes the plan's `extra` output from the local copy of the source (its track numbers are the ones the plan was made with). Returns None when the plan has
        none, else {'path', 'sha256', ...} or {'error': why}. A failure here is not the job's failure: the main file is still converted and the report says the copy was not made."""
        extra = plan.get("extra")
        if not isinstance(extra, dict):
            return None
        rel = str(extra.get("rel") or "")
        if not rel or rel.startswith("/") or ".." in rel.split("/") or not rel.lower().endswith(".mkv"):
            return {"error": "the plan names a destination for the smaller copy that cannot be used"}
        if self.m.get("extra_error"):
            return {"error": self.m["extra_error"]}
        path = os.path.join(self.folder, "extra", os.path.basename(rel))
        done = self.stage_info("extra_encode")
        if not (done.get("done") and os.path.isfile(path) and os.path.getsize(path) == done.get("bytes")):
            done.clear()
            self.log.info(f"Making the {extra.get('label') or 'smaller'} copy from the same local copy of the film")
            try:
                done.update(self.encode(local, path, extra.get("change") or {}, plan.get("duration"), extra.get("profile")))
            except JobFailure as exc:
                self.m["extra_error"] = exc.message[:300]
                self.save()
                self.log.warning(f"The smaller copy could not be made ({exc.message[:300]}); the main file carries on")
                return {"error": self.m["extra_error"]}
            self.save()
        if not done.get("sha256"):
            done["sha256"] = hash_file(path, check=self.guard)
            self.save()
        return {"path": path, "sha256": done["sha256"], "bytes": done["bytes"], "encode_seconds": done.get("seconds"), "encoder": done.get("encoder")}

    def deliver_extra(self, plan, made, source_mtime_ns):
        """Copies the smaller copy next to the main file (never over a different file) and verifies it. Returns what the report says about it."""
        if made.get("error"):
            return {"error": made["error"]}
        rel = plan["extra"]["rel"]
        try:
            dst = safe_path(self.agent.roots.path(self.job["dest"]["root"]), rel)
        except ValueError as exc:
            return {"error": str(exc)}
        info = self.stage_info("extra_copy_out")
        size, digest = made["bytes"], made["sha256"]
        if os.path.lexists(dst):
            if info.get("done") or (os.path.isfile(dst) and os.path.getsize(dst) == size and (self.cfg.verify == "size" or hash_file(dst, check=self.guard) == digest)):
                return {"bytes": size, "sha256": digest, "rel": rel, "encode_seconds": made.get("encode_seconds"), "encoder": made.get("encoder")}
            return {"error": f"{rel} already exists and is different. SyncHub never overwrites a file."}
        partial = dst + ".synchub-partial"
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            self.report_progress("copy_out", 0.95, "Copying out the smaller copy", force=True)
            with open(made["path"], "rb") as fin, open(partial, "wb") as fout:
                while True:
                    block = fin.read(self.cfg.copy_chunk_mb * 1048576)
                    if not block:
                        break
                    fout.write(block)
                    self.reporter.check()
                fout.flush()
                os.fsync(fout.fileno())
            if os.path.getsize(partial) != size or (self.cfg.verify == "hash" and hash_file(partial, check=self.guard) != digest):
                raise JobFailure("verification_failed", "The copy on the share does not match what was made.")
            os.replace(partial, dst)
            try:
                os.utime(dst, ns=(time.time_ns(), source_mtime_ns))
            except OSError:
                pass
        except (OSError, JobFailure) as exc:
            self._remove(partial)
            return {"error": f"could not copy it to the share: {getattr(exc, 'message', None) or exc}"[:300]}
        info.update(done=True, bytes=size, finished=iso())
        self.save()
        self.log.info(f"Copied out the smaller copy to {rel}")
        return {"bytes": size, "sha256": digest, "rel": rel, "encode_seconds": made.get("encode_seconds"), "encoder": made.get("encoder")}

    def do_preview(self):
        """Makes the two clips of a preview (docs/DESIGN_ENCODING.md section 12) and uploads them: a window of the film encoded with the planned settings, and the same
        window unmodified. Reads the film where it is; only the short window is ever copied to the work disk."""
        plan = self.job["plan"]
        if not self.job.get("source"):
            raise JobFailure("bad_job", "A preview job needs a source.", retryable=False)
        src = self.folder_in(self.job["source"]["root"], self.job["source"]["rel"])
        if not os.path.isfile(src):
            raise JobFailure("source_changed", f"The film is not there any more: {self.job['source']['rel']}")
        stat = os.stat(src)
        if plan.get("source_size") and stat.st_size != plan["source_size"]:
            raise JobFailure("source_changed", f"{self.job['source']['rel']} is not the file SyncHub analysed (its size differs). It will be looked at again.", retryable=False)
        change = plan.get("change") or {}
        profile = clean_profile(plan.get("profile"))
        if not profile or profile["video_codec"] == "keep":
            raise JobFailure("bad_job", "This preview has no compression settings to show.", retryable=False)
        free = shutil.disk_usage(self.folder).free
        if free < SPACE_MARGIN:
            raise JobFailure("transient", f"Not enough free space on the work disk to make a preview: {human_bytes(free)} is free.")
        work = os.path.join(self.folder, "preview")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        settings = self.media_settings({})
        self.report_progress("preview", 0.02, "Reading the film", force=True)
        data = media_analysis.run_ffprobe(settings, src)
        duration = media_duration(data) or plan.get("duration")
        if not duration:
            raise JobFailure("bad_job", "The film's length could not be read, so a window cannot be chosen.", retryable=False)
        video = next((st for st in data.get("streams") or [] if isinstance(st, dict) and st.get("codec_type") == "video"), None) or {}
        profile = self.with_source_colours(settings, src, profile)
        seconds = min(60, max(30, int(plan.get("seconds") or 45)))
        mode = plan.get("mode") if plan.get("mode") in ("random", "worst") else "random"
        if mode == "worst":
            self.report_progress("preview", 0.05, "Looking for the most demanding part", force=True)

        def measure(start):
            return self.video_packet_bytes(settings, src, start, PREVIEW_PROBE_SECONDS)

        start, length = choose_window(float(duration), seconds, mode, plan.get("seed") or 0, measure)
        self.log.info(f"Preview window: {start:.0f} s to {start + length:.0f} s of {duration:.0f} s ({mode})")
        segment = os.path.join(work, "segment.mkv")
        self.run_clip(segment_command(self.cfg.ffmpeg, src, segment, start, length, preview_audio_index(change)), None, 0.08, 0.18, "Cutting the window")
        got = media_duration(media_analysis.run_ffprobe(settings, segment)) or length
        encoded, original = os.path.join(work, "encoded.mp4"), os.path.join(work, "source.mp4")
        command = clip_command(self.cfg.ffmpeg, segment, encoded, video_arguments({"action": "transcode"}, profile))
        self.log.info("Encoding the preview clip", command=shlex.join(command))
        self.run_clip(command, got, 0.18, 0.70, "Compressing the window")
        codec = str(video.get("codec_name") or "").lower()
        hdr = profile.get("hdr")
        self.run_clip(clip_command(self.cfg.ffmpeg, segment, original, source_clip_arguments(codec, hdr)), got, 0.70, 0.80, "Making the matching original")
        if os.path.getsize(original) > PREVIEW_COPY_LIMIT:
            self.log.info("The unmodified clip is large; making it again at a high quality")
            self.run_clip(clip_command(self.cfg.ffmpeg, segment, original, source_clip_arguments(codec, hdr, capped=True)), got, 0.70, 0.80, "Making the matching original")
        for name, path in (("encoded", encoded), ("source", original)):
            if os.path.getsize(path) > PREVIEW_COPY_LIMIT + 100 * 1024 ** 2:
                raise JobFailure("preview_too_large", f"The {name} clip came out at {human_bytes(os.path.getsize(path))}, too big to send. Try a shorter clip.", retryable=False)
        encoded_video = self.video_packet_bytes(settings, encoded, 0, None)
        source_video = self.video_packet_bytes(settings, segment, 0, None)
        enc_dur = media_duration(media_analysis.run_ffprobe(settings, encoded)) or got
        result = {"start": start, "length": round(got, 1), "mode": mode, "encoded_bytes": os.path.getsize(encoded), "source_clip_bytes": os.path.getsize(original),
                  "encoded_video_kbps": round(encoded_video * 8 / enc_dur / 1000), "source_video_kbps": round(source_video * 8 / got / 1000),
                  "crf": profile.get("crf"), "hdr": (profile.get("hdr") or {}).get("kind"), "source_codec": codec}
        for index, (name, path) in enumerate((("encoded", encoded), ("source", original))):
            self.report_progress("preview", 0.82 + 0.08 * index, f"Sending the {name} clip", force=True)
            self.tell(f"/preview/{name}", path, poster=self.api.upload)
        shutil.rmtree(work, ignore_errors=True)
        self.report_progress("preview", 1.0, "Preview ready", force=True)
        self.log.info(f"Preview clips sent ({human_bytes(result['encoded_bytes'])} compressed, {human_bytes(result['source_clip_bytes'])} original)")
        return result

    def video_packet_bytes(self, settings, path, start, seconds):
        """Bytes of video in `seconds` from `start` (the whole file when `seconds` is None), summed from the packet sizes."""
        command = [settings.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size", "-of", "csv=p=0"]
        if seconds:
            command += ["-read_intervals", f"{start:.2f}%+{seconds}"]
        result, outcome = media_analysis.run_tool(settings, command + [path], 300, f"ffprobe packets {path}")
        if outcome != "ok" or result is None or result.returncode != 0:
            return 0
        total = 0
        for line in result.stdout.splitlines():
            line = line.strip().split(",")[0]
            if line.isdigit():
                total += int(line)
        return total

    def run_clip(self, command, duration, low, high, label):
        """Runs one ffmpeg command for a preview, reporting progress between `low` and `high`; a failure ends the job with ffmpeg's own words."""
        errors = os.path.join(self.folder, "ffmpeg.err")
        started = time.monotonic()
        with open(errors, "w", encoding="utf-8", errors="replace") as err:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", errors="replace", start_new_session=True)
            try:
                for line in proc.stdout:
                    self.reporter.check()
                    fraction, _ = parse_ffmpeg_progress(line, duration)
                    if fraction is not None:
                        elapsed = time.monotonic() - started
                        self.report_progress("preview", low + (high - low) * fraction, f"{label}: {fraction * 100:.0f}%", int(elapsed * (1 - fraction) / fraction) if fraction > 0.01 else None)
                proc.wait()
            except BaseException:
                self._stop_ffmpeg(proc)
                raise
        if proc.returncode != 0:
            raise JobFailure("encode_failed", f"ffmpeg stopped with exit code {proc.returncode} ({label.lower()}): {read_file(errors).strip()[-800:] or 'no message'}", retryable=False)

    def remove_replaced_source(self, src, dst):
        """When the converted file has a different name (an .avi that became an .mkv), the old file is removed once the new one is in place."""
        if dst != src and os.path.isfile(src) and os.path.isfile(dst):
            os.remove(src)

    def keep_original(self, src, local, size, mtime, plan):
        """Called just before the converted file is swapped in: checks the share still holds the file that was copied and moves the verified local copy
        into the originals folder, with a meta.json saying where it came from and when it may go. Does nothing if the retention is 0."""
        now = os.stat(src)
        if now.st_size != size or int(now.st_mtime) != mtime:
            raise JobFailure("source_changed", "The file on the share changed while it was being converted; it was not replaced.")
        days = plan.get("originals_retention_days")
        days = int(days) if isinstance(days, (int, float)) else 30
        if days <= 0:
            self.log.info("Originals are not kept (retention is 0); the replacement is verified, so the original is dropped")
            return
        info = self.stage_info("copy_in")
        folder = os.path.join(self.cfg.originals_dir, f"{self.job['id']}-{hashlib.sha1(self.job['source']['rel'].encode('utf-8')).hexdigest()[:8]}")
        os.makedirs(folder, exist_ok=True)
        kept = os.path.join(folder, os.path.basename(src))
        if not (os.path.isfile(kept) and os.path.getsize(kept) == size):
            if not os.path.isfile(local):
                raise JobFailure("transient", "The verified copy of the original is missing from the work folder.")
            shutil.move(local, kept)
        expires = iso(utcnow() + timedelta(days=days))
        meta = {"job_id": self.job["id"], "root": self.job["source"]["root"], "rel": self.job["source"]["rel"], "name": os.path.basename(src), "size": size,
                "sha256": info.get("sha256"), "mtime": mtime, "saved_at": iso(), "retention_days": days, "expires_at": expires, "replaced_with": self.job["dest"]["rel"]}
        atomic_write_json(os.path.join(folder, "meta.json"), meta)
        self.m["original"] = {"path": kept, "expires_at": expires}
        self.save()
        self.log.info(f"Kept the original in {folder} for {days} day(s)")

    def encode(self, local, converted, change, expected_duration, profile=None):
        """Runs ffmpeg on the local copy and checks what it made. Returns what the job remembers about the encode. The output only gets its
        real name once it has passed the checks, so a half-made file is never mistaken for a finished one. A long video is encoded in chunks (see
        encode_chunks) so an interruption loses at most one; a plan that allows the GPU uses it, and falls back to the CPU encoder if it cannot."""
        os.makedirs(os.path.dirname(converted), exist_ok=True)
        partial = converted + ".partial"
        for leftover in (partial, converted):
            if os.path.exists(leftover):
                os.remove(leftover)
        size = os.path.getsize(local)
        free = shutil.disk_usage(self.folder).free
        if free < size + SPACE_MARGIN:
            raise JobFailure("transient", f"Not enough free space on the work disk to convert this file: about {human_bytes(size)} is needed and {human_bytes(free)} is free.")
        settings = self.media_settings({})
        if not expected_duration:
            expected_duration = media_duration(media_analysis.run_ffprobe(settings, local))
        profile = clean_profile(profile)
        transcode = bool(profile and profile["video_codec"] != "keep" and (change.get("video") or {}).get("action") == "transcode")
        if transcode:
            profile = self.with_source_colours(settings, local, profile)
            bump = int(self.m.get("crf_bump") or 0)
            if bump and profile.get("max_bytes"):
                profile["crf"] = min(PROFILE_CRF["hevc"][1], profile["crf"] + 2 * bump)
                self.log.info(f"The last attempt came out over the size limit; encoding at quality {profile['crf']} this time", crf=profile["crf"])
        kind = choose_encoder(profile, (self.agent.capabilities or {}).get("encoders"), self.cfg.video_encoder) if transcode else "cpu"
        if profile and profile.get("gpu") and transcode and kind == "cpu":
            self.log.info("The plan allows GPU encoding but this worker has no GPU encoder that works (or the picture is HDR); using the CPU encoder")
        started, tried = time.monotonic(), []
        holding = False
        if kind != "cpu":
            holding = self.agent.gpu_take()
            if not holding:
                self.log.info(f"The GPU is already encoding {self.cfg.gpu_slots} job(s) (the limit set in SyncHub); using the CPU encoder for this one")
                kind = "cpu"
        try:
            while True:
                try:
                    command_text = self.encode_with(kind, local, partial, change, profile, expected_duration, transcode)
                    break
                except JobFailure as exc:
                    if kind == "cpu" or exc.error_class not in ("encode_failed", "size_target_missed"):
                        raise
                    tried.append(f"{kind}: {exc.message[:300]}")
                    self.log.warning(f"The {kind} GPU encoder did not work out ({exc.message[:300]}); encoding again with the CPU encoder", encoder=kind)
                    self.m["gpu_fallback"] = tried
                    self.save()
                    self._remove(partial)
                    shutil.rmtree(os.path.join(self.folder, "chunks"), ignore_errors=True)
                    self.m.pop("chunks", None)
                    kind = "cpu"
        finally:
            if holding:
                self.agent.gpu_give()
        try:
            probe = self.check_conversion(settings, partial, change, expected_duration, profile)
            produced = os.path.getsize(partial)
            if profile and profile.get("max_bytes") and produced > profile["max_bytes"]:
                self.m["crf_bump"] = int(self.m.get("crf_bump") or 0) + 1
                self.save()
                raise JobFailure("size_target_missed", f"The compressed file is {human_bytes(produced)}, over the {human_bytes(profile['max_bytes'])} limit"
                                 + (" (it will be tried once more at a lower quality)." if self.m["crf_bump"] < 2 else "."))
        except JobFailure:
            self._remove(partial)
            raise
        os.replace(partial, converted)
        shutil.rmtree(os.path.join(self.folder, "chunks"), ignore_errors=True)
        self.m.pop("chunks", None)
        return {"done": True, "bytes": os.path.getsize(converted), "seconds": round(time.monotonic() - started, 1), "command": command_text[-4000:], "probe": probe, "finished": iso(),
                "encoder": kind, "fallback": tried or None, "chunked": bool(self.m.get("chunked"))}

    def encode_with(self, kind, local, partial, change, profile, expected_duration, transcode):
        """One encode of the whole video with `kind` ('cpu' or a GPU kind), into `partial`. Returns the ffmpeg command it ran (the last one, for chunks)."""
        chunk = self.cfg.chunk_minutes * 60
        self.m["chunked"] = bool(transcode and chunk and expected_duration and expected_duration > 2 * chunk)
        if self.m["chunked"]:
            return self.encode_chunks(kind, local, partial, change, profile, expected_duration, chunk)
        command = build_convert_command(self.cfg.ffmpeg, local, partial, change, profile, kind, self.cfg.vaapi_device)
        text = shlex.join(command)
        self.log.info("Converting", command=text, encoder=kind)
        self.report_progress("encode", 0.25, "Starting ffmpeg", force=True)
        self.run_ffmpeg(command, partial, expected_duration, 0.25, 0.85, "Converting")
        return text

    def run_ffmpeg(self, command, partial, duration, low, high, label):
        """Runs one ffmpeg command, reporting progress between `low` and `high` of the job, and raises if it fails. `partial` is removed on any failure."""
        started, errors, speed = time.monotonic(), os.path.join(self.folder, "ffmpeg.err"), None
        with open(errors, "w", encoding="utf-8", errors="replace") as err:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", errors="replace", start_new_session=True)
            try:
                for line in proc.stdout:
                    self.reporter.check()
                    fraction, said = parse_ffmpeg_progress(line, duration)
                    speed = said or speed
                    if fraction is not None:
                        elapsed = time.monotonic() - started
                        eta = int(elapsed * (1 - fraction) / fraction) if fraction > 0.01 else None
                        self.report_progress("encode", low + (high - low) * fraction, f"{label}: {fraction * 100:.0f}%" + (f" at {speed}x" if speed else ""), eta if high - low >= 0.5 else None)
                proc.wait()
            except BaseException:
                self._stop_ffmpeg(proc)
                self._remove(partial)
                raise
        if proc.returncode != 0:
            tail = read_file(errors).strip()[-800:]
            self._remove(partial)
            raise JobFailure("encode_failed", f"ffmpeg stopped with exit code {proc.returncode}: {tail or 'no message'}", retryable=False)

    def encode_chunks(self, kind, local, partial, change, profile, duration, seconds):
        """Encodes the video in pieces of `seconds` (each a video-only file in the job's chunks folder), then joins them without re-encoding and adds the audio,
        subtitles, chapters and metadata from the source in one piece. A finished chunk is never encoded again: after an interruption or a reboot this carries on
        with the first unfinished one. The chunks are only reused when every setting that shapes them is unchanged."""
        folder = os.path.join(self.folder, "chunks")
        count = max(1, math.ceil(duration / seconds - 0.001))
        key = hashlib.sha1(json.dumps({"profile": profile, "encoder": kind, "seconds": seconds, "video": (change.get("video") or {})}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        info = self.m.get("chunks")
        if not isinstance(info, dict) or info.get("key") != key or info.get("count") != count:
            shutil.rmtree(folder, ignore_errors=True)
            info = self.m["chunks"] = {"key": key, "count": count, "done": {}}
        os.makedirs(folder, exist_ok=True)
        command = None
        for index in range(count):
            path = os.path.join(folder, f"v{index:04d}.mkv")
            if os.path.isfile(path) and info["done"].get(str(index)) == os.path.getsize(path):
                continue
            start = index * seconds
            length = None if index == count - 1 else seconds
            expected = duration - start if length is None else length
            command = build_chunk_command(self.cfg.ffmpeg, local, path + ".partial", change, profile, kind, self.cfg.vaapi_device, start, length)
            self.log.info(f"Encoding chunk {index + 1} of {count}", command=shlex.join(command), encoder=kind)
            self.run_ffmpeg(command, path + ".partial", expected, 0.25 + 0.60 * index / count, 0.25 + 0.60 * (index + 1) / count, f"Converting part {index + 1} of {count}")
            got = media_duration(media_analysis.run_ffprobe(self.media_settings({}), path + ".partial"))
            if got is None or abs(got - expected) > max(1.5, expected * 0.02):
                self._remove(path + ".partial")
                raise JobFailure("encode_failed", f"Part {index + 1} of {count} came out {'unreadable' if got is None else f'{got:.0f} s long instead of {expected:.0f} s'}.", retryable=False)
            os.replace(path + ".partial", path)
            info["done"][str(index)] = os.path.getsize(path)
            self.save()
        listing = os.path.join(folder, "chunks.txt")
        with open(listing, "w", encoding="utf-8") as handle:
            for index in range(count):
                handle.write("file '" + os.path.join(folder, f"v{index:04d}.mkv").replace("'", "'\\''") + "'\n")
        self.report_progress("encode", 0.85, "Joining the parts", force=True)
        join = build_convert_command(self.cfg.ffmpeg, local, partial, change, profile, kind, self.cfg.vaapi_device, chunk_list=listing)
        self.log.info("Joining the encoded parts with the source's audio, subtitles and chapters", command=shlex.join(join))
        self.run_ffmpeg(join, partial, duration, 0.85, 0.85, "Joining")
        return shlex.join(command or join)

    def quality_check(self, local, converted, change, profile):
        """Scores the converted video against the source with VMAF (a few short windows spread over the film) when the plan asks for it and ffmpeg has libvmaf.
        Returns what was measured ({mean, min, samples, floor}, or {skipped: why}), remembered in the job so a restart does not measure again. In 'hold' mode a mean
        under the floor pauses the job for a person; approving it carries on."""
        profile = clean_profile(profile)
        mode = (profile or {}).get("vmaf_mode") or "off"
        if mode == "off" or not profile or profile["video_codec"] == "keep" or (change.get("video") or {}).get("action") != "transcode":
            return None
        size = os.path.getsize(converted)
        info = self.m.get("vmaf")
        if not isinstance(info, dict) or info.get("file_bytes") != size:
            info = self.m["vmaf"] = {"file_bytes": size, **self.measure_vmaf(local, converted)}
            self.save()
        mean = info.get("mean")
        if mean is None:
            self.log.info(f"No quality score: {info.get('skipped')}")
            return {k: v for k, v in info.items() if k != "file_bytes"}
        self.log.info(f"Quality score (VMAF) {mean:.1f} (worst window {info['min']:.1f}; the floor is {info['floor']:g})")
        approved = (self.job.get("decision") or {}).get("action") in ("approve", "approve_changes")
        if mode == "hold" and mean < info["floor"] and not approved:
            raise NeedsReview(f"The converted video scored {mean:.1f} for quality (VMAF), under {info['floor']:g}. It may look visibly worse than the original: "
                              "approve to use it anyway, or skip or reject to keep the original.", {"vmaf": {k: v for k, v in info.items() if k != "file_bytes"}})
        return {k: v for k, v in info.items() if k != "file_bytes"}

    def measure_vmaf(self, local, converted):
        if not (self.agent.capabilities or {}).get("vmaf"):
            return {"skipped": "this worker's ffmpeg has no libvmaf"}
        settings = self.media_settings({})
        data = media_analysis.run_ffprobe(settings, local)
        video = next((st for st in data.get("streams") or [] if isinstance(st, dict) and st.get("codec_type") == "video"), None)
        facts = media_analysis.video_facts(video, data.get("format"))
        if facts["hdr"] != "sdr":
            return {"skipped": "VMAF is not meaningful for HDR pictures"}
        height = int((video or {}).get("height") or 0)
        windows = vmaf_windows(media_duration(data), self.cfg.vmaf_samples)
        scores = []
        for position, (start, length) in enumerate(windows):
            self.report_progress("encode", 0.85, f"Checking quality {position + 1} of {len(windows)}", force=True)
            command = vmaf_command(self.cfg.ffmpeg, converted, local, start, length, min(4, os.cpu_count() or 1))
            try:
                done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=1800)
            except (OSError, subprocess.SubprocessError) as exc:
                return {"skipped": f"the VMAF run failed ({exc})"}
            score = parse_vmaf(done.stdout) if done.returncode == 0 else None
            if score is None:
                return {"skipped": "ffmpeg could not compute a VMAF score: " + (done.stdout or "")[-300:].strip()}
            scores.append(score)
            self.reporter.check()
        if not scores:
            return {"skipped": "the video is too short to sample"}
        return {"mean": round(sum(scores) / len(scores), 2), "min": round(min(scores), 2), "samples": len(scores), "floor": vmaf_floor(height)}

    def with_source_colours(self, settings, local, profile):
        """Looks at the source's picture before an HEVC or H.264 re-encode. HDR10 and HLG are kept for HEVC (10-bit, colour tags, mastering data read from the file itself,
        whatever SyncHub guessed); an HDR picture is never squeezed into H.264, and Dolby Vision profile 5 is never re-encoded: both would wash the colours out."""
        data = media_analysis.run_ffprobe(settings, local)
        video = next((st for st in data.get("streams") or [] if isinstance(st, dict) and st.get("codec_type") == "video"), None)
        facts = media_analysis.video_facts(video, data.get("format"))
        if facts["hdr"] != "sdr" and not (facts.get("master_display") and facts.get("max_cll")):
            frame = media_analysis.video_facts({**(video or {}), "side_data_list": [*((video or {}).get("side_data_list") or []), *media_analysis.first_frame_side_data(settings, local)]})
            facts = {**frame, **{k: v for k, v in facts.items() if v not in (None, "")}}
        profile = dict(profile)
        profile.pop("hdr", None)
        if facts["hdr"] == "sdr":
            if facts.get("interlaced"):
                profile["deinterlace"] = True
            return profile
        if profile["video_codec"] != "hevc":
            raise JobFailure("encode_failed", "This is an HDR video. Re-encoding it as H.264 would wash out the colours, so it was not converted. Use an HEVC, Standard or Move only profile for it.", retryable=False)
        if facts["hdr"] == "dv" and str(facts.get("dv_profile")) == "5":
            raise JobFailure("encode_failed", "This is Dolby Vision profile 5. Re-encoding it would turn the colours green and purple, so it was not converted.", retryable=False)
        kind = "hlg" if facts.get("transfer") == "arib-std-b67" else "hdr10"
        if facts["hdr"] == "dv" and facts.get("transfer") != "smpte2084":
            raise JobFailure("encode_failed", "This Dolby Vision video has no HDR10 base layer, so it cannot be kept as HDR10. It was not converted.", retryable=False)
        profile["hdr"] = {"kind": kind, **{k: facts[k] for k in ("master_display", "max_cll") if facts.get(k)}}
        profile["ten_bit"] = True
        if facts["hdr"] == "dv":
            self.log.warning(f"Dolby Vision profile {facts.get('dv_profile')}: the Dolby layer is dropped and the HDR10 picture is kept")
        return profile

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _stop_ffmpeg(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL) if hasattr(os, "killpg") else proc.kill()
        except (OSError, ProcessLookupError):
            proc.kill()
        proc.wait()

    def check_conversion(self, settings, path, change, expected_duration, profile=None):
        """The converted file must be readable, as long as the source (within half a percent), have the tracks the plan kept and decode cleanly at its end."""
        data = media_analysis.run_ffprobe(settings, path)
        streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
        counts = {kind: sum(1 for s in streams if s.get("codec_type") == kind) for kind in ("video", "audio", "subtitle")}
        problems = []
        if counts["video"] < 1:
            problems.append("it has no video")
        wanted = (profile or {}).get("video_codec")
        if wanted in ("hevc", "h264") and (change.get("video") or {}).get("action") == "transcode":
            got_codec = next((str(st.get("codec_name")) for st in streams if st.get("codec_type") == "video"), None)
            if got_codec and got_codec not in ({"hevc"} if wanted == "hevc" else {"h264"}):
                problems.append(f"the video is {got_codec} but the profile asks for {wanted}")
        limit = (profile or {}).get("max_height")
        got_height = next((int(st.get("height") or 0) for st in streams if st.get("codec_type") == "video"), 0)
        if limit and got_height > limit:
            problems.append(f"the picture is {got_height} pixels high but the plan asked for at most {limit}")
        for kind, key in (("audio", "keep_audio"), ("subtitle", "keep_subtitle")):
            planned = len(change[key]) + (1 if kind == "audio" and change.get("audio_add") else 0) if isinstance(change.get(key), list) else None
            if planned is not None and counts[kind] != planned:
                problems.append(f"it has {counts[kind]} {kind} track(s) but {planned} were planned")
        got = media_duration(data)
        if expected_duration and got is not None and abs(got - expected_duration) > max(1.0, expected_duration * 0.005):
            problems.append(f"it is {got:.0f} s long but the source is {expected_duration:.0f} s")
        if not problems:
            status, detail = media_analysis.decode_check(settings, path)
            if status == media_analysis.INT_DECODE_ERROR:
                problems.append(f"it does not decode cleanly: {detail[:300]}")
        if problems:
            raise JobFailure("verification_failed", "The converted file failed its checks: " + "; ".join(problems))
        return {"duration": got, **counts}

    def remux_dolby_vision(self, local, converted, expected_duration):
        """Dolby Vision profile 7 to 8.1 without re-encoding the picture: the video is streamed through dovi_tool (which drops the enhancement layer and rewrites the Dolby
        data) into a raw stream, then mkvmerge joins it with the source's other tracks. The result must read back as profile 8 with the same tracks and length."""
        settings = self.media_settings({})
        data = media_analysis.run_ffprobe(settings, local)
        video = next((st for st in data.get("streams") or [] if isinstance(st, dict) and st.get("codec_type") == "video"), None)
        facts = media_analysis.video_facts(video, data.get("format"))
        self.require_profile_7(facts)
        fps = str((video or {}).get("r_frame_rate") or "")
        if not re.match(r"^\d{1,6}(/\d{1,6})?$", fps) or fps in ("0/0", "0"):
            raise JobFailure("encode_failed", "The frame rate of the video is not known, so it cannot be joined back together.", retryable=False)
        size = os.path.getsize(local)
        free = shutil.disk_usage(self.folder).free
        if free < size * 2 + SPACE_MARGIN:
            raise JobFailure("transient", f"Not enough free space on the work disk: about {human_bytes(size * 2)} is needed for the converted video and the joined file, and {human_bytes(free)} is free.")
        folder = os.path.join(self.folder, "dv")
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)
        stream, partial, errors = os.path.join(folder, "video.hevc"), converted + ".partial", os.path.join(folder, "tools.err")
        os.makedirs(os.path.dirname(converted), exist_ok=True)
        for leftover in (partial, converted):
            self._remove(leftover)
        started = time.monotonic()
        extract, convert = dovi_extract_command(self.cfg.ffmpeg, local), dovi_convert_command(self.cfg.dovi_tool, stream)
        self.log.info("Converting Dolby Vision profile 7 to 8.1", command=shlex.join(extract) + " | " + shlex.join(convert))
        self.report_progress("encode", 0.25, "Converting the Dolby Vision data", force=True)
        with open(errors, "w", encoding="utf-8", errors="replace") as err:
            reader = subprocess.Popen(extract, stdout=subprocess.PIPE, stderr=err, start_new_session=True)
            try:
                writer = subprocess.Popen(convert, stdin=reader.stdout, stdout=err, stderr=err, start_new_session=True)
            except OSError as exc:
                self._stop_ffmpeg(reader)
                raise JobFailure("encode_failed", f"dovi_tool could not be started: {exc}", retryable=False)
            reader.stdout.close()
            try:
                while writer.poll() is None:
                    self.reporter.check()
                    written = os.path.getsize(stream) if os.path.exists(stream) else 0
                    self.report_progress("encode", 0.25 + 0.50 * min(0.99, written / max(1, size * 0.95)), f"Converting the Dolby Vision data: {human_bytes(written)}")
                    time.sleep(1.0)
                reader.wait()
            except BaseException:
                self._stop_ffmpeg(writer)
                self._stop_ffmpeg(reader)
                shutil.rmtree(folder, ignore_errors=True)
                raise
        if reader.returncode != 0 or writer.returncode != 0:
            tail = read_file(errors).strip()[-800:]
            shutil.rmtree(folder, ignore_errors=True)
            raise JobFailure("encode_failed", f"The Dolby Vision conversion stopped (ffmpeg exit {reader.returncode}, dovi_tool exit {writer.returncode}): {tail or 'no message'}", retryable=False)
        self.report_progress("encode", 0.78, "Joining the video with the audio and subtitles", force=True)
        join = mkvmerge_command(self.cfg.mkvmerge, stream, fps, local, partial)
        self.log.info("Joining with mkvmerge", command=shlex.join(join))
        with open(errors, "w", encoding="utf-8", errors="replace") as err:
            try:
                done = subprocess.run(join, stdout=err, stderr=subprocess.STDOUT, timeout=6 * 3600)
            except (OSError, subprocess.SubprocessError) as exc:
                shutil.rmtree(folder, ignore_errors=True)
                raise JobFailure("encode_failed", f"mkvmerge could not run: {exc}", retryable=False)
        shutil.rmtree(folder, ignore_errors=True) if done.returncode in (0, 1) else None     # mkvmerge exits 1 for warnings only
        if done.returncode not in (0, 1):
            self._remove(partial)
            raise JobFailure("encode_failed", f"mkvmerge stopped with exit code {done.returncode}.", retryable=False)
        try:
            probe = self.check_dv_remux(settings, partial, data, expected_duration)
        except JobFailure:
            self._remove(partial)
            raise
        os.replace(partial, converted)
        return {"done": True, "bytes": os.path.getsize(converted), "seconds": round(time.monotonic() - started, 1), "command": " | ".join((shlex.join(extract), shlex.join(convert))) + " ; " + shlex.join(join),
                "probe": probe, "finished": iso(), "encoder": "dovi_tool", "fallback": None, "chunked": False}

    @staticmethod
    def require_profile_7(facts):
        if facts["hdr"] != "dv" or str(facts.get("dv_profile")) != "7":
            raise JobFailure("encode_failed", f"This file is not Dolby Vision profile 7 (it is {facts['hdr']}{' profile ' + str(facts.get('dv_profile')) if facts['hdr'] == 'dv' else ''}), so there is nothing to convert.", retryable=False)

    def check_dv_remux(self, settings, path, source, expected_duration):
        """The joined file must read back as Dolby Vision profile 8 (compatibility id 1, a plain HDR10 base layer), have the source's audio and subtitle tracks and its length, and decode cleanly."""
        data = media_analysis.run_ffprobe(settings, path)
        streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
        wanted = [s for s in source.get("streams") or [] if isinstance(s, dict)]
        counts = lambda items, kind: sum(1 for s in items if s.get("codec_type") == kind)          # noqa: E731
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        facts = media_analysis.video_facts(video, data.get("format")) if video else {"hdr": None}
        problems = []
        if facts.get("hdr") != "dv" or str(facts.get("dv_profile")) != "8":
            problems.append(f"the video does not read back as Dolby Vision profile 8 (it reads as {facts.get('hdr')}{' profile ' + str(facts.get('dv_profile')) if facts.get('hdr') == 'dv' else ''})")
        elif str(facts.get("dv_compat")) not in ("1", "None"):
            problems.append(f"the Dolby Vision data is not compatible with HDR10 (compatibility id {facts.get('dv_compat')})")
        for kind in ("audio", "subtitle"):
            if counts(streams, kind) != counts(wanted, kind):
                problems.append(f"it has {counts(streams, kind)} {kind} track(s) but the source has {counts(wanted, kind)}")
        got = media_duration(data)
        if expected_duration and got is not None and abs(got - expected_duration) > max(1.0, expected_duration * 0.005):
            problems.append(f"it is {got:.0f} s long but the source is {expected_duration:.0f} s")
        if not problems:
            status, detail = media_analysis.decode_check(settings, path)
            if status == media_analysis.INT_DECODE_ERROR:
                problems.append(f"it does not decode cleanly: {detail[:300]}")
        if problems:
            raise JobFailure("verification_failed", "The converted file failed its checks: " + "; ".join(problems))
        return {"duration": got, "dv_profile": facts.get("dv_profile"), "dv_compat": facts.get("dv_compat"), "audio": counts(streams, "audio"), "subtitle": counts(streams, "subtitle")}

    # progress: copy in 0 - 45 %, copy out 45 - 90 %, reading the copy back to verify it 90 - 100 %
    BANDS = {"copy_in": (0.0, 0.45), "copy_out": (0.45, 0.90), "verify": (0.90, 1.0)}

    def copy_in(self, src, local, size, mtime):
        info = self.stage_info("copy_in")
        os.makedirs(os.path.dirname(local), exist_ok=True)
        self.report_progress("copy_in", 0.0, "Starting the copy", force=True)
        if info.get("done") and os.path.isfile(local) and os.path.getsize(local) == info.get("bytes") == size and info.get("source_mtime") == mtime and info.get("sha256"):
            self.log.info("The source was already copied and verified; not copying it again", bytes=size)
            self.report_progress("copy_in", self.BANDS["copy_in"][1], "Already copied", force=True)
            return None
        if info and (info.get("source_size") != size or info.get("source_mtime") != mtime):
            self.log.warning("The source changed since the earlier copy; starting over", before={"size": info.get("source_size"), "mtime": info.get("source_mtime")}, now={"size": size, "mtime": mtime})
            shutil.rmtree(os.path.dirname(local), ignore_errors=True)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            info.clear()
        free = shutil.disk_usage(self.folder).free
        partial = local + ".partial"
        have = os.path.getsize(partial) if os.path.exists(partial) else 0
        if free < size - min(have, info.get("checkpoint", 0)) + SPACE_MARGIN:
            raise JobFailure("transient", f"Not enough free space on the work disk: this file needs {human_bytes(size)} and {human_bytes(free)} is free.")
        info.update(source_size=size, source_mtime=mtime, done=False)
        digest, offset = hashlib.sha256(), 0
        checkpoint = int(info.get("checkpoint") or 0)
        if checkpoint and os.path.exists(partial) and os.path.getsize(partial) >= checkpoint:
            self.log.info(f"Resuming the copy at {human_bytes(checkpoint)}", checkpoint=checkpoint)
            with open(partial, "r+b") as handle:
                handle.truncate(checkpoint)                     # anything past the last checkpoint was never synced: do not trust it
            digest, offset = self._rehash(partial, checkpoint), checkpoint
        else:
            if os.path.exists(partial):
                os.remove(partial)
            checkpoint = 0
        chunk, every = self.cfg.copy_chunk_mb * 1048576, self.cfg.checkpoint_mb * 1048576
        started, base, last_ck = time.monotonic(), offset, offset
        with open(src, "rb") as fin, open(partial, "r+b" if offset else "wb") as fout:
            fin.seek(offset)
            fout.seek(offset)
            while True:
                block = fin.read(chunk)
                if not block:
                    break
                fout.write(block)
                digest.update(block)
                offset += len(block)
                if offset - last_ck >= every:
                    fout.flush()
                    os.fsync(fout.fileno())
                    info["checkpoint"] = last_ck = offset
                    self.save()
                self._progress("copy_in", offset, size, base, started, "Copying in")
                self.reporter.check()
                self.throttle(started, offset - base)
            fout.flush()
            os.fsync(fout.fileno())
        now = os.stat(src)
        if now.st_size != size or int(now.st_mtime) != mtime:
            raise JobFailure("source_changed", "The source file changed while it was being copied.")
        if offset != size:
            raise JobFailure("transient", f"The copy stopped early ({human_bytes(offset)} of {human_bytes(size)}); the connection to the share may have dropped.")
        os.replace(partial, local)
        info.update(done=True, bytes=size, sha256=digest.hexdigest(), checkpoint=size, finished=iso())
        self.save()
        seconds = time.monotonic() - started
        self.log.info(f"Copied in {human_bytes(size - base)} in {human_duration(seconds)}", sha256=info["sha256"], resumed_from=base)
        return seconds

    def _rehash(self, path, upto):
        """The running sha256 of a partial file's first `upto` bytes, so the copy can carry on from there."""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            left = upto
            while left > 0:
                block = handle.read(min(HASH_BUFFER, left))
                if not block:
                    break
                digest.update(block)
                left -= len(block)
                self.reporter.check()
        return digest

    def _progress(self, band, done, total, base, started, verb):
        """Reports how far a copy has got: the overall fraction, a sentence with the speed, and an ETA that counts the steps still to come."""
        elapsed = max(time.monotonic() - started, 0.001)
        rate = (done - base) / elapsed
        steps_after = {"copy_in": ("copy_out", "verify"), "copy_out": ("verify",), "verify": ()}[band]
        remaining = (total - done) + sum(total for step in steps_after if step != "verify" or self.cfg.verify == "hash")
        low, high = self.BANDS[band]
        fraction = low + (high - low) * (done / total if total else 1)
        message = f"{verb}: {human_bytes(done)} of {human_bytes(total)}" + (f" at {human_bytes(rate)}/s" if rate > 0 else "")
        self.report_progress("copy_out" if band == "verify" else band, fraction, message, int(remaining / rate) if rate > 0 else None)

    def copy_out(self, local, dst, size, digest, source_mtime_ns, overwrite=None, before_swap=None):
        """Copies `local` to `dst` as a .synchub-partial, verifies it and renames it into place. Never overwrites, except the one file named in
        `overwrite` (a replacement in place); `before_swap` runs once the copy is verified and just before it is renamed in."""
        info = self.stage_info("copy_out")
        self.stage = "copy_out"
        ancestor = os.path.dirname(dst)
        while not os.path.isdir(ancestor):
            ancestor = os.path.dirname(ancestor)
        free_dest = shutil.disk_usage(ancestor).free
        if os.path.lexists(dst):
            if os.path.isfile(dst) and os.path.getsize(dst) == size and (self.cfg.verify == "size" or hash_file(dst, check=self.guard) == digest):
                self.log.info("The destination already holds this exact file; nothing to copy", path=self.job["dest"]["rel"])
                info.update(done=True, bytes=size, sha256=digest)
                self.save()
                return None
            if overwrite != dst:
                raise JobFailure("dest_exists", f"The destination already exists and is different: {self.job['dest']['rel']}. SyncHub never overwrites a file; pick another name.", retryable=False)
        partial = dst + ".synchub-partial"
        self.m["dest_partial"] = partial
        self.save()
        have = os.path.getsize(partial) if os.path.exists(partial) else 0
        if free_dest < size - min(have, int(info.get("checkpoint") or 0)) + SPACE_MARGIN:
            raise JobFailure("transient", f"Not enough free space on the destination share: this file needs {human_bytes(size)} and {human_bytes(free_dest)} is free.")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        checkpoint = int(info.get("checkpoint") or 0) if info.get("size") == size and info.get("sha256") == digest else 0
        info.update(size=size, sha256=digest, done=False)
        if checkpoint and os.path.exists(partial) and os.path.getsize(partial) >= checkpoint:
            self.log.info(f"Resuming the upload at {human_bytes(checkpoint)}", checkpoint=checkpoint)
            with open(partial, "r+b") as handle:
                handle.truncate(checkpoint)
        else:
            checkpoint = 0
            if os.path.exists(partial):
                os.remove(partial)
        chunk, every = self.cfg.copy_chunk_mb * 1048576, self.cfg.checkpoint_mb * 1048576
        offset = last_ck = base = checkpoint
        started = time.monotonic()
        with open(local, "rb") as fin, open(partial, "r+b" if offset else "wb") as fout:
            fin.seek(offset)
            fout.seek(offset)
            while True:
                block = fin.read(chunk)
                if not block:
                    break
                fout.write(block)
                offset += len(block)
                if offset - last_ck >= every:
                    fout.flush()
                    os.fsync(fout.fileno())
                    info["checkpoint"] = last_ck = offset
                    self.save()
                self._progress("copy_out", offset, size, base, started, "Copying out")
                self.reporter.check()
                self.throttle(started, offset - base)
            fout.flush()
            os.fsync(fout.fileno())
            try:
                os.posix_fadvise(fout.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)       # make the read-back come from the share, not this PC's cache
            except (AttributeError, OSError):
                pass
        wrote = time.monotonic() - started
        if os.path.getsize(partial) != size:
            found = os.path.getsize(partial)
            os.remove(partial)
            info.clear()
            raise JobFailure("verification_failed", f"The copy on the share is {human_bytes(found)} but should be {human_bytes(size)}. It was deleted so the retry starts clean.")
        if self.cfg.verify == "hash":
            self.log.info("Reading the copy back to verify it")
            read_started, read = time.monotonic(), 0

            def tick(n):
                nonlocal read
                read += n
                self._progress("verify", read, size, 0, read_started, "Verifying")
                self.reporter.check()
            found = hash_file(partial, check=tick)
            if found != digest:
                self.log.error("The copy on the share does not match the source; deleting it and starting over", expected=digest, found=found)
                os.remove(partial)
                info.clear()
                shutil.rmtree(os.path.dirname(local), ignore_errors=True)
                self.m["stages"].pop("copy_in", None)
                self.save()
                raise JobFailure("verification_failed", "The copy on the share does not match the source (checksums differ). Both copies were deleted so the retry starts clean.")
        if os.path.lexists(dst) and overwrite != dst:
            raise JobFailure("dest_exists", f"Someone created {self.job['dest']['rel']} while it was being copied. The finished copy is kept next to it as {os.path.basename(partial)}.", retryable=False)
        if before_swap is not None:
            try:
                before_swap()
            except JobFailure:
                self._remove(partial)
                info.clear()
                raise
        os.replace(partial, dst)
        try:
            os.utime(dst, ns=(time.time_ns(), source_mtime_ns))             # keep the original's modified date, which Jellyfin shows as "date added"
        except OSError:
            pass
        info.update(done=True, bytes=size, checkpoint=size, finished=iso())
        self.m.pop("dest_partial", None)
        self.save()
        self.log.info(f"Copied out {human_bytes(size - base)} to {self.job['dest']['rel']}", resumed_from=base)
        return wrote


# ============================================================================ the agent
class Agent:
    MIN_POLL = 2.0                                      # seconds; SyncHub may ask for longer, never shorter

    def __init__(self, cfg, log, api=None):
        self.cfg, self.log = cfg, log
        self.api = api or Api(cfg)
        self.store = JobStore(cfg, log)
        self.roots = Roots(cfg.root_overrides)
        self.monitor = RootMonitor(self.roots, log)
        self.stopping = threading.Event()
        self._benchmarking = threading.Event()
        self._relay_digest, self._relay_at, self._relay_names = None, 0.0, set()
        self.relay_state, self._relay_probed = None, 0.0
        self.gpu_busy = 0                              # GPU encodes running now (see gpu_take)
        self.active = {}                               # job id -> JobRunner
        self.progress = {}                             # job id -> (stage, fraction), for the status summary
        self.lock = threading.Lock()
        self.capabilities = None
        self._originals_digest, self._originals_sent = None, 0.0
        self.started = time.monotonic()
        self.poll_seconds, self.registered, self.roots_hash = 5.0, False, None
        self._cpu_sample = None
        self._last_sweep, self._backoff, self._warned = 0.0, 0.0, {}

    # ---- called by runners
    def note_progress(self, job_id, stage, fraction):
        with self.lock:
            self.progress[job_id] = (stage, round(fraction, 3))

    def runner_done(self, job_id):
        with self.lock:
            self.active.pop(job_id, None)
            self.progress.pop(job_id, None)

    # ---- talking to SyncHub
    def warn_once(self, key, text, every=300):
        if time.monotonic() - self._warned.get(key, -1e9) > every:
            self._warned[key] = time.monotonic()
            self.log.warning(text)

    def apply_settings(self, answer):
        """The settings SyncHub sent in a register or poll answer, applied at once."""
        changed = self.cfg.apply_remote((answer or {}).get("settings"))
        if changed:
            if "log_level" in changed:
                self.log.set_level(changed["log_level"])
            self.log.info("Settings changed in SyncHub", **{k: v for k, v in changed.items()})
        return changed

    def gpu_take(self):
        """True when a GPU encode may start (fewer than gpu_slots running); the caller must gpu_give() afterwards."""
        with self.lock:
            if self.gpu_busy >= self.cfg.gpu_slots:
                return False
            self.gpu_busy += 1
            return True

    def gpu_give(self):
        with self.lock:
            self.gpu_busy = max(self.gpu_busy - 1, 0)

    def register(self):
        if self.capabilities is None:
            self.capabilities = detect_capabilities(self.cfg)
            cpu = (self.capabilities.get("host") or {}).get("cpu") or {}
            self.log.info("Capabilities", cpu=cpu.get("model"), **{k: v for k, v in self.capabilities.items() if k not in ("ops", "host")})
        body = {"protocol": PROTOCOL, "version": __version__, "os": os_name(), "hostname": self.cfg.hostname, "slots": self.cfg.slots, "capabilities": self.capabilities}
        answer = self.api.post("/register", body)
        self.poll_seconds = max(self.MIN_POLL, float(answer.get("poll_seconds") or 5))
        self.apply_settings(answer)
        self.roots.update(answer.get("roots"))
        self.roots_hash = answer.get("roots_hash")
        self.monitor.wake.set()
        self.registered = True
        self.log.info(f"Registered with SyncHub as {answer.get('name')}", roots=self.roots.names(), poll_seconds=self.poll_seconds)

    def status(self):
        with self.lock:
            jobs = [{"id": i, "stage": s, "progress": p} for i, (s, p) in sorted(self.progress.items())][:10]
        roots = {name: {"ok": r["ok"], "state": r["state"], "message": (r["message"] or "")[:200], "free_bytes": r["free_bytes"]} for name, r in self.monitor.snapshot().items()}
        host, self._cpu_sample = live_host_stats(self._cpu_sample)
        count, held = self.originals_held()
        return {"version": __version__, "uptime_seconds": int(time.monotonic() - self.started), "work_free_bytes": self.work_free(), "roots": roots, "jobs": jobs, "host": host,
                "originals_count": count, "originals_bytes": held, **({"relay": self.relay_state} if self.relay_state else {})}

    def originals(self):
        """(folder, meta) for each original kept after a file was replaced in place."""
        out = []
        try:
            names = sorted(os.listdir(self.cfg.originals_dir))
        except OSError:
            return out
        for name in names:
            folder = os.path.join(self.cfg.originals_dir, name)
            try:
                with open(os.path.join(folder, "meta.json"), "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                out.append((folder, meta))
        return out

    def originals_listing(self):
        """What SyncHub's Originals tab shows: each kept original (and each converted copy set aside by a restore) with where it came from and when it may go."""
        out = []
        for folder, meta in self.originals()[:400]:
            out.append({"id": os.path.basename(folder), "kind": meta.get("kind") or "original", "root": meta.get("root"), "rel": meta.get("rel"), "name": meta.get("name"),
                        "size": meta.get("size"), "saved_at": meta.get("saved_at"), "expires_at": meta.get("expires_at"), "retention_days": meta.get("retention_days"),
                        "replaced_with": meta.get("replaced_with")})
        return out

    def originals_held(self):
        held = self.originals()
        return len(held), sum(int(m.get("size") or 0) for _, m in held)

    def sweep_originals(self):
        """Deletes the originals whose retention has passed (never before). Returns how many."""
        removed = 0
        for folder, meta in self.originals():
            until = parse_iso(meta.get("expires_at"))
            if until is not None and until < utcnow():
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
                self.log.info(f"Removed the kept original {meta.get('rel')} (retention of {meta.get('retention_days')} day(s) is over)")
        return removed

    def work_free(self):
        try:
            return shutil.disk_usage(self.cfg.work_dir).free
        except OSError:
            return None

    def poll_once(self):
        with self.lock:
            active = sorted(self.active)
        body = {"free_slots": max(self.cfg.slots - len(active), 0) if not self.stopping.is_set() else 0, "free_bytes": self.work_free(),
                "holding": sorted(set(self.store.holding()) | set(active)), "active": active, "status": self.status()}
        listing = self.originals_listing()
        digest = hashlib.sha1(json.dumps(listing, sort_keys=True).encode("utf-8")).hexdigest()
        if digest != self._originals_digest or time.monotonic() - self._originals_sent > 600:        # the list rides along when it changes, and now and then so SyncHub's copy stays fresh
            body["originals"] = listing
        answer = self.api.post("/poll", body)
        self.apply_settings(answer)
        if "originals" in body:
            self._originals_digest, self._originals_sent = digest, time.monotonic()
        for job_id in answer.get("cancel") or []:
            runner = self.active.get(job_id)
            if runner:
                runner.reporter.cancel = True
        for job_id in answer.get("release") or []:
            self.release(job_id)
        for job_id in answer.get("cleanup") or []:
            self.cleanup(job_id)
        for job_id in answer.get("log_requests") or []:
            self.upload_log(job_id)
        if answer.get("worker_log"):
            self.upload_worker_log()
        if answer.get("benchmark") and not active and not self._benchmarking.is_set() and not self.stopping.is_set():
            self.start_benchmark()
        if answer.get("relay") is not None:
            self.sync_relay(answer["relay"])
        if not self.stopping.is_set():
            for job in (answer.get("resume") or []) + (answer.get("jobs") or []):
                self.start(job)
        self.poll_seconds = max(self.MIN_POLL, float(answer.get("poll_seconds") or self.poll_seconds))
        if answer.get("roots_hash") and answer["roots_hash"] != self.roots_hash:
            fresh = self.api.post("/roots")
            self.roots.update(fresh.get("roots"))
            self.roots_hash = fresh.get("roots_hash") or answer["roots_hash"]
            self.monitor.wake.set()
            self.log.info("The list of shares changed in SyncHub", roots=self.roots.names())
        return answer

    RELAY_RESEND = 300           # seconds; go2rtc forgets streams added through its web address when it restarts, so they are given again now and then
    RELAY_PROBE = 30             # seconds between looks at what go2rtc really serves (SyncHub shows it)

    def sync_relay(self, relay):
        """The camera relay (go2rtc, in the same compose project as this worker): SyncHub says which streams it should serve, this puts them there,
        and then reads back what go2rtc serves so SyncHub can show it. The stream addresses carry Protect's stream keys, so only the names are ever
        logged or reported. Nothing happens unless GO2RTC_URL is set."""
        base = os.environ.get("GO2RTC_URL", "").rstrip("/")
        streams = {str(n): str(src) for n, src in ((relay or {}).get("streams") or {}).items()} if isinstance(relay, dict) else {}
        nonce = str((relay or {}).get("nonce") or "") if isinstance(relay, dict) else ""
        if not base:
            return
        digest = hashlib.sha1(json.dumps([streams, nonce], sort_keys=True).encode("utf-8")).hexdigest()
        now = time.monotonic()
        if digest == self._relay_digest and now - self._relay_at < self.RELAY_RESEND:
            if now - self._relay_probed >= self.RELAY_PROBE:
                self._relay_look(base, streams)
            return

        def call(method, query):
            request = urllib.request.Request(f"{base}/api/streams?{urllib.parse.urlencode(query)}", method=method)
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        try:
            for name, src in streams.items():
                call("PUT", {"name": name, "src": src})
            for name in self._relay_names - set(streams):
                call("DELETE", {"src": name})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if self._relay_digest is not None or self._relay_at == 0.0:
                self.log.warning(f"Could not give the camera relay at {base} its cameras: {exc}")
            self._relay_digest, self._relay_at = None, now
            self.relay_state = {"up": False, "streams": [], "error": f"go2rtc did not answer at {base} ({exc.__class__.__name__})"}
            return
        if digest != self._relay_digest:
            self.log.info(f"The camera relay now serves {len(streams)} camera(s): {', '.join(sorted(streams)) or 'none'}")
        self._relay_digest, self._relay_at, self._relay_names = digest, now, set(streams)
        self._relay_look(base, streams)

    def _relay_look(self, base, wanted):
        """What go2rtc serves right now, as the worker's status tells SyncHub: {"up", "streams": [names], "error"}."""
        self._relay_probed = time.monotonic()
        try:
            with urllib.request.urlopen(f"{base}/api/streams", timeout=5) as response:
                served = json.load(response)
            names = sorted(served) if isinstance(served, dict) else []
            missing = sorted(set(wanted) - set(names))
            self.relay_state = {"up": True, "streams": names, "error": f"missing: {', '.join(missing)}" if missing else ""}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.relay_state = {"up": False, "streams": [], "error": f"go2rtc did not answer at {base} ({exc.__class__.__name__})"}

    # ---- jobs
    def start(self, job):
        with self.lock:
            if job["id"] in self.active:
                return
        waiting = self.store.read(self.store.folder(job))
        if waiting and waiting.get("state") == "finished" and (waiting.get("report") or {}).get("sent") is False:
            self.deliver(job["id"], self.store.folder(job))          # the work is done; SyncHub only has not been told yet
            return
        folder, manifest = self.store.open(job, job.get("attempt") or 1)
        runner = JobRunner(self, job, folder, manifest)
        with self.lock:
            self.active[job["id"]] = runner
        runner.start()

    def folder_for(self, job_id):
        return [f for i, f in self.store.folders() if i == int(job_id)]

    def cleanup(self, job_id):
        if job_id in self.active:
            return
        for folder in self.folder_for(job_id):
            self.store.remove(folder)
            self.log.info(f"Removed the files of job {job_id} as SyncHub asked", extra={"fields": {"job": job_id}})
        try:
            self.api.post(f"/jobs/{job_id}/cleaned")
        except (ApiError, Unreachable) as exc:
            self.log.warning(f"Could not tell SyncHub that job {job_id} was cleaned up: {exc}")

    def release(self, job_id):
        """SyncHub no longer needs this folder: its job is done elsewhere, or SyncHub has never heard of it. Folders of jobs this agent
        finished are left to their own retention."""
        if job_id in self.active:
            return
        for folder in self.folder_for(job_id):
            manifest = self.store.read(folder)
            if manifest and manifest.get("outcome") == "done":
                continue
            self.store.remove(folder)
            self.log.info(f"Removed the leftover files of job {job_id} (SyncHub no longer needs them)", extra={"fields": {"job": job_id}})

    def upload_log(self, job_id):
        folders = self.folder_for(job_id)
        if folders:
            text, truncated = JobLog(folders[0], job_id, self.log).full()
            text = text or "(this job's log is empty)"
        else:
            text, truncated = "No log is kept for this job on this worker (its folder is gone: retention is over, or it was cleaned up).", False
        try:
            self.api.post(f"/jobs/{job_id}/log", {"text": text, "truncated": truncated})
            self.log.info(f"Uploaded the log of job {job_id}", extra={"fields": {"job": job_id, "bytes": len(text)}})
        except ApiError as exc:
            self.log.warning(f"SyncHub did not accept the log of job {job_id}: {exc}")
        except Unreachable as exc:
            self.log.warning(f"Could not upload the log of job {job_id}: {exc}")

    def start_benchmark(self):
        """The Workers page's Run benchmark button: run it in the background when no job is running, then send the report."""
        self._benchmarking.set()

        def work():
            try:
                self.log.info("Running the benchmark as SyncHub asked")
                caps = detect_capabilities(self.cfg)
                report = benchmark_report(self.cfg, caps, say=lambda text: self.log.info(text))
                self.api.post("/benchmark", {"report": report})
                self.log.info("Benchmark sent", ok=report["ok"])
            except (ApiError, Unreachable) as exc:
                self.log.warning(f"Could not send the benchmark: {exc}")
            except Exception as exc:                       # noqa: B902  (a benchmark must never take the worker down)
                self.log.warning(f"The benchmark failed: {exc}")
            finally:
                self._benchmarking.clear()
        threading.Thread(target=work, name="benchmark", daemon=True).start()

    def upload_worker_log(self):
        """The Workers page's Get logs button: send the tail of this agent's own log (worker.jsonl and its rotated files)."""
        text, truncated = read_log_tail(self.cfg.logs_dir)
        self.log.info("Uploading the worker log as SyncHub asked", bytes=len(text))
        try:
            self.api.post("/log", {"text": text or "(the log file is empty or cannot be read)", "truncated": truncated})
        except ApiError as exc:
            self.log.warning(f"SyncHub did not accept the worker log: {exc}")
        except Unreachable as exc:
            self.log.warning(f"Could not upload the worker log: {exc}")

    # ---- reports that must not be lost
    def deliver(self, job_id, folder):
        manifest = self.store.read(folder)
        report = (manifest or {}).get("report")
        if not report or report.get("sent"):
            return True
        path = f"/jobs/{job_id}/{report['kind']}"
        log = JobLog(folder, job_id, self.log)
        try:
            answer = self.api.post(path, report.get("payload") or {})
            log.info(f"SyncHub accepted the {report['kind']} report", answer=answer.get("outcome"))
        except ApiError as exc:
            if exc.status in (401, 403, 426) or exc.status >= 500:
                log.warning(f"SyncHub could not take the {report['kind']} report now ({exc}); will try again")
                return False
            log.warning(f"SyncHub no longer wants the {report['kind']} report ({exc}); the job is not ours any more")
            report["note"] = str(exc)
        except Unreachable as exc:
            log.warning(f"Cannot reach SyncHub to send the {report['kind']} report ({exc}); will try again")
            return False
        report["sent"] = True
        manifest["report"] = report
        if report["kind"] == "complete":
            self.store.purge_files(folder)
            manifest["keep_until"] = iso(utcnow() + timedelta(hours=self.cfg.done_retention_hours))
        self.store.save(folder, manifest)
        return True

    def deliver_pending(self):
        for job_id, folder in self.store.folders():
            if job_id in self.active:
                continue
            manifest = self.store.read(folder)
            if manifest and manifest.get("state") == "finished" and (manifest.get("report") or {}).get("sent") is False:
                self.deliver(job_id, folder)

    # ---- the loop
    def touch_alive(self):
        """A file whose age says the main loop is still turning (for Docker's health check). It says nothing about SyncHub being reachable:
        a worker that cannot reach SyncHub keeps trying and is healthy."""
        try:
            path = os.path.join(self.cfg.work_dir, "alive")
            with open(path, "a"):
                os.utime(path, None)
        except OSError:
            pass

    def housekeeping(self):
        if time.monotonic() - self._last_sweep < 600:
            return
        self._last_sweep = time.monotonic()
        with self.lock:
            active = set(self.active)
        removed = self.store.sweep(active)
        if removed:
            self.log.info(f"Housekeeping removed {removed} old job folder(s)")
        self.sweep_originals()

    def run(self):
        self.log.info(f"SyncHub worker agent {__version__} starting", url=self.cfg.url, work_dir=os.path.abspath(self.cfg.work_dir), slots=self.cfg.slots)
        self.monitor.start()
        while not self.stopping.is_set():
            try:
                if not self.registered:
                    self.register()
                self.poll_once()
                self.deliver_pending()
                self.housekeeping()
                self._backoff = 0.0
                delay = self.poll_seconds * random.uniform(0.9, 1.1)
            except Unreachable as exc:
                self.warn_once("unreachable", f"Cannot reach SyncHub at {self.cfg.url}: {exc}. Trying again.", 60)
                self._backoff = min(max(self._backoff * 2, 5.0), 60.0)
                delay = self._backoff
            except ApiError as exc:
                if exc.status == 401:
                    self.warn_once("auth", "SyncHub does not accept this worker's token. Was it replaced or revoked? Put the current token in the settings and restart the agent.", 120)
                elif exc.status == 403:
                    self.warn_once("auth", "This worker is disabled in SyncHub (Workers page). Waiting.", 120)
                elif exc.status == 426:
                    self.warn_once("protocol", f"SyncHub and this agent speak different protocol versions: {exc.message}", 120)
                else:
                    self.warn_once("api", f"SyncHub answered with an error: {exc}", 60)
                if exc.status in (401, 403, 426):
                    self.registered = False
                self._backoff = min(max(self._backoff * 2, 10.0), 60.0)
                delay = self._backoff
            except Exception:                                           # noqa: B902  (the loop must survive anything)
                self.log.exception("Unexpected error in the main loop")
                delay = 10.0
            self.touch_alive()
            self.stopping.wait(delay)
        self.shutdown()

    def shutdown(self):
        self.log.info("Stopping: finishing at the next safe point; unfinished jobs resume when the agent starts again")
        self.monitor.stopping.set()
        self.monitor.wake.set()
        with self.lock:
            runners = list(self.active.values())
        for runner in runners:
            runner.join(timeout=60)
        self.log.info("Stopped")

    def stop(self, *_):
        self.stopping.set()


# ============================================================================ command line
BENCHMARK_SECONDS = 30
BENCHMARK_PROFILE = {"video_codec": "hevc", "crf": 22, "preset": "medium", "ten_bit": True, "audio_kbps": 192, "gpu": True, "name": "benchmark"}


def benchmark_label(kind, caps):
    if kind == "cpu":
        model = ((caps.get("host") or {}).get("cpu") or {}).get("model")
        return f"CPU (libx265){' ' + model if model else ''}"[:80]
    return f"GPU ({kind.upper()}, {GPU_ENCODERS[kind]})"


def benchmark_report(cfg, caps, source=None, seconds=BENCHMARK_SECONDS, say=lambda text: None):
    """Times each working encoder on a short clip and scores the result with VMAF. Health first (ffmpeg, work folder, a two-second try of every encoder),
    then speed and quality. `source` is a real file to take the clip from; without one a synthetic 1080p clip with film-like noise is made.
    Returns a report dict that SyncHub stores and shows (see synchub.workers.store_benchmark). Never raises: a step that fails is reported as failed."""
    started = time.time()
    health, results = [], []
    host = caps.get("host") or {}
    report = {"agent": __version__, "when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "seconds": int(seconds),
              "cpu": ((host.get("cpu") or {}).get("model")), "cores": caps.get("cores"), "gpus": [g.get("name") for g in host.get("gpus") or [] if g.get("name")][:4],
              "source": "your file" if source else f"synthetic 1080p, 24 fps, {int(seconds)} s", "health": health, "results": results, "ok": False}
    health.append({"name": "ffmpeg", "ok": bool(caps.get("ffmpeg")), "detail": caps.get("ffmpeg") or "not found"})
    health.append({"name": "VMAF", "ok": bool(caps.get("vmaf")), "detail": "available" if caps.get("vmaf") else "libvmaf is not in this ffmpeg, so quality is not scored"})
    if not caps.get("ffmpeg"):
        return report
    work = tempfile.mkdtemp(prefix="benchmark-", dir=cfg.work_dir)
    try:
        health.append({"name": "work folder", "ok": True, "detail": f"{human_bytes(shutil.disk_usage(cfg.work_dir).free)} free"})
        clip = os.path.join(work, "source.mkv")
        if source:
            code, out = run_quiet([cfg.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", "60", "-t", str(int(seconds)), "-i", source, "-map", "0:v:0", "-an", "-sn", "-c:v", "copy", clip], timeout=300)
        else:
            code, out = run_quiet([cfg.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=24,noise=alls=8:allf=t",
                                   "-t", str(int(seconds)), "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p", clip], timeout=300)
        if code != 0 or not os.path.exists(clip) or os.path.getsize(clip) == 0:
            health.append({"name": "test clip", "ok": False, "detail": (out or "ffmpeg could not make or cut the clip").strip()[-300:]})
            return report
        health.append({"name": "test clip", "ok": True, "detail": human_bytes(os.path.getsize(clip))})
        fps = 24.0
        if source and caps.get("ffprobe"):
            code, out = run_quiet([cfg.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", clip], timeout=30)
            try:
                num, _, den = out.strip().partition("/")
                fps = float(num) / float(den or 1) or fps
            except (ValueError, ZeroDivisionError):
                pass
        kinds = ["cpu"] + [k for k in GPU_ORDER if GPU_ENCODERS[k] in (caps.get("encoders") or [])]
        profile = clean_profile(BENCHMARK_PROFILE)
        for kind in kinds:
            row = {"encoder": kind, "label": benchmark_label(kind, caps), "ok": False}
            results.append(row)
            say(f"Trying {row['label']}...")
            pre, video_args = video_plan({}, profile, kind, cfg.vaapi_device)
            out_path = os.path.join(work, f"out-{kind}.mkv")
            base = [cfg.ffmpeg, "-nostdin", "-hide_banner", "-y", "-v", "error"]
            code, out = run_quiet(base + pre + ["-t", "2", "-i", clip, "-map", "0:v:0", "-an"] + video_args + ["-f", "null", "-"], timeout=120)
            if code != 0:
                row["reason"] = (out or "the encoder did not start").strip().splitlines()[-1][:200] if (out or "").strip() else "the encoder did not start"
                continue
            began = time.monotonic()
            code, out = run_quiet(base + pre + ["-i", clip, "-map", "0:v:0", "-an"] + video_args + ["-f", "matroska", out_path], timeout=1800)
            took = time.monotonic() - began
            if code != 0 or not os.path.exists(out_path):
                row["reason"] = (out or "the encode failed").strip().splitlines()[-1][:200] if (out or "").strip() else "the encode failed"
                continue
            speed = seconds / took if took > 0 else None
            row.update(ok=True, seconds=round(took, 1), speed=round(speed, 2) if speed else None, fps=round(speed * fps, 1) if speed else None, size_mb=round(os.path.getsize(out_path) / 1024 ** 2, 1))
            if caps.get("vmaf"):
                say(f"Scoring {row['label']} with VMAF...")
                code, text = run_quiet(vmaf_command(cfg.ffmpeg, out_path, clip, 0, float(seconds), caps.get("cores") or 2), timeout=1800)
                score = parse_vmaf(text) if code == 0 else None
                row.update(vmaf=round(score, 1) if score is not None else None, vmaf_floor=vmaf_floor(1080))
            row["passed"] = row.get("vmaf") is None or row["vmaf"] >= row["vmaf_floor"]
    except OSError as exc:
        health.append({"name": "benchmark", "ok": False, "detail": str(exc)[:200]})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report["ok"] = all(h["ok"] for h in health if h["name"] != "VMAF") and any(r["ok"] for r in results)
    report["took_seconds"] = round(time.time() - started)
    return report


def format_benchmark(report):
    """The report as a table for the terminal."""
    lines = [f"SyncHub worker benchmark ({report.get('agent')}), {report.get('source')}", f"CPU: {report.get('cpu') or 'unknown'}   cores: {report.get('cores')}   GPU: {', '.join(report.get('gpus') or []) or 'none seen'}", ""]
    lines += [f"  [{'ok' if h['ok'] else '!!'}] {h['name']}: {h['detail']}" for h in report.get("health") or []]
    lines += ["", f"{'Encoder':<44}{'Speed':>9}{'fps':>8}{'VMAF':>8}  Result"]
    for r in report.get("results") or []:
        if not r.get("ok"):
            lines.append(f"{r['label']:<44}{'':>9}{'':>8}{'':>8}  failed: {r.get('reason') or 'unknown'}")
            continue
        vmaf = f"{r['vmaf']:.1f}" if r.get("vmaf") is not None else "n/a"
        lines.append(f"{r['label']:<44}{(str(r['speed']) + 'x') if r.get('speed') else '':>9}{r.get('fps') or '':>8}{vmaf:>8}  {'passed' if r.get('passed') else 'below the quality floor ' + str(r.get('vmaf_floor'))}")
    lines.append("")
    lines.append("The benchmark passed." if report.get("ok") else "The benchmark found problems.")
    return "\n".join(lines)


def command_benchmark(cfg, source=None):
    """--benchmark: runs the benchmark here and prints it. SyncHub is not needed; use the Workers page to run it and keep the result."""
    caps = detect_capabilities(cfg)
    report = benchmark_report(cfg, caps, source=source, say=print)
    print()
    print(format_benchmark(report))
    return 0 if report["ok"] else 1


def command_check(cfg):
    """Prints what works and what does not. Exit code 0 when everything needed is in order."""
    problems = 0

    def line(ok, label, detail=""):
        nonlocal problems
        problems += 0 if ok else 1
        print(f"  [{'ok' if ok else '!!'}] {label}{(': ' + detail) if detail else ''}")

    print(f"SyncHub worker agent {__version__}  (Python {platform.python_version()}, {os_name()})")
    print(f"Settings file: {cfg.path or '(none: environment only)'}")
    print(f"SyncHub: {cfg.url}   token {cfg.masked_token()}   name {cfg.name or '(set in SyncHub)'}")
    print(f"Work folder: {os.path.abspath(cfg.work_dir)}")
    try:
        os.makedirs(cfg.jobs_dir, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=cfg.work_dir, delete=True)
        probe.close()
        line(True, "work folder is writable", f"{human_bytes(shutil.disk_usage(cfg.work_dir).free)} free")
    except OSError as exc:
        line(False, "work folder is writable", str(exc))
    api = Api(cfg)
    roots = Roots(cfg.root_overrides)
    try:
        answer = api.post("/roots")
        line(True, "SyncHub accepts the token", f"worker '{answer.get('name')}', protocol {answer.get('protocol')}")
        line(answer.get("protocol") == PROTOCOL, "protocol version matches", f"agent {PROTOCOL}, SyncHub {answer.get('protocol')}")
        roots.update(answer.get("roots"))
    except ApiError as exc:
        line(False, "SyncHub accepts the token", {401: "the token was not accepted (replaced or revoked?)", 403: "this worker is disabled in SyncHub"}.get(exc.status, str(exc)))
    except Unreachable as exc:
        line(False, "SyncHub is reachable", f"{exc}. Check the address and that this PC can reach it.")
    caps = detect_capabilities(cfg)
    line(bool(caps["ffmpeg"]), "ffmpeg", caps["ffmpeg"] or "not found: copy jobs work, encoding jobs will not (install ffmpeg or set ffmpeg = /path/to/ffmpeg)")
    if caps["ffmpeg"]:
        line(bool(caps["encoders"]), "encoders that work", ", ".join(caps["encoders"]) or "none")
        print(f"       ffprobe: {caps['ffprobe'] or 'not found'}   VMAF: {'yes' if caps['vmaf'] else 'no'}   cores: {caps['cores']}")
    names = roots.names()
    if not names:
        print("  --  no shares are defined in SyncHub yet (Storage page), or SyncHub could not be reached")
    for name in names:
        path = roots.path(name)
        result = check_root(path, roots.text(name), write_test=True)
        line(result["ok"], f"share {name} at {path}", f"ready, {human_bytes(result['free_bytes'])} free" if result["ok"] else result["message"])
    print("Everything needed is in order." if not problems else f"{problems} problem(s) found.")
    return 0 if not problems else 1


def command_init_roots(cfg, force=False):
    """Writes the marker file into each share, once, when asked. Never creates a folder: a missing folder means the share is not mounted."""
    api = Api(cfg)
    try:
        answer = api.post("/roots")
    except (ApiError, Unreachable) as exc:
        print(f"Cannot get the list of shares from SyncHub: {exc}")
        return 1
    roots = Roots(cfg.root_overrides)
    roots.update(answer.get("roots"))
    if not roots.names():
        print("SyncHub has no shares yet. Add them on its Storage page first.")
        return 1
    failed = 0
    for name in roots.names():
        path, text = roots.path(name), roots.text(name)
        marker = os.path.join(path, MARKER)
        if not os.path.isdir(path):
            print(f"{name}: {path} does not exist. Mount the share first (this will not create the folder).")
            failed += 1
            continue
        note = "" if os.path.ismount(path) else "  (note: this is not a mount point; fine for a plain local folder, but check the share is really mounted)"
        try:
            with open(marker, "r", encoding="utf-8") as handle:
                current = handle.read(256).strip()
        except FileNotFoundError:
            current = None
        except OSError as exc:
            print(f"{name}: cannot read {marker}: {exc}")
            failed += 1
            continue
        if current == text:
            print(f"{name}: already set up at {path}{note}")
            continue
        if current is not None and not force:
            print(f"{name}: {path} is marked '{current}', not '{text}'. That looks like the wrong share. Check the mount, or use --force to replace the marker.")
            failed += 1
            continue
        try:
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError as exc:
            print(f"{name}: cannot write {marker}: {exc}")
            failed += 1
            continue
        print(f"{name}: wrote {marker}{note}")
    return 1 if failed else 0


def command_healthcheck(cfg, max_age=180):
    """For Docker's HEALTHCHECK: exit 0 when the agent's main loop has turned recently."""
    try:
        age = time.time() - os.path.getmtime(os.path.join(cfg.work_dir, "alive"))
    except OSError:
        print("The agent has not started its loop yet")
        return 1
    if age > max_age:
        print(f"The agent's loop has not turned for {int(age)} s")
        return 1
    print("ok")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="SyncHub worker agent")
    parser.add_argument("--config", help="settings file (default: ./synchub-agent.ini or /etc/synchub-agent.ini; environment variables override it)")
    parser.add_argument("--check", action="store_true", help="check the settings, SyncHub, ffmpeg and every share, then exit")
    parser.add_argument("--benchmark", action="store_true", help="time each encoder (CPU, GPU) on a short clip, score it with VMAF, print the table, then exit")
    parser.add_argument("--benchmark-file", help="with --benchmark: cut the clip from this film instead of making a synthetic one")
    parser.add_argument("--init-roots", action="store_true", help=f"write the {MARKER} marker file into each share, then exit")
    parser.add_argument("--force", action="store_true", help="with --init-roots: replace a marker that names a different share")
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 if a running agent's loop is alive (for Docker)")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    try:
        cfg = Config(args.config)
        if args.healthcheck:
            return command_healthcheck(cfg)
        cfg.validate()
    except ConfigError as exc:
        print(f"Settings problem: {exc}", file=sys.stderr)
        return 2
    log = build_logger(cfg, console=not (args.check or args.init_roots or args.benchmark))
    if args.check:
        return command_check(cfg)
    if args.benchmark:
        return command_benchmark(cfg, args.benchmark_file)
    if args.init_roots:
        return command_init_roots(cfg, args.force)
    agent = Agent(cfg, log)
    signal.signal(signal.SIGTERM, agent.stop)
    signal.signal(signal.SIGINT, agent.stop)
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
