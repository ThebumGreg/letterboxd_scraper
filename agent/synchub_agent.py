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
import os
import platform
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

__version__ = "0.2.0"
PROTOCOL = 1                       # the SyncHub worker API version this agent speaks
MARKER = ".synchub-root"
OPS = ["copy"]                     # the operations (plan.op) this agent can do; SyncHub only hands it jobs it can run
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
    INTS = {"slots": (1, 16), "log_max_mb": (1, 1024), "log_backups": (0, 50), "copy_chunk_mb": (1, 256), "checkpoint_mb": (16, 8192),
            "failed_retention_days": (0, 3650), "request_timeout": (5, 300)}
    FLOATS = {"done_retention_hours": (0, 24 * 365), "max_copy_mb_s": (0, 100000)}

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
        self.log_level = (pick("worker", "log_level", "SYNCHUB_LOG_LEVEL", "INFO") or "INFO").upper()
        self.verify = (pick("worker", "verify", "SYNCHUB_VERIFY", "hash") or "hash").lower()
        self.use_proxy = (pick("synchub", "use_proxy", "SYNCHUB_USE_PROXY", "no") or "no").lower() in ("1", "yes", "true", "on")
        defaults = {"slots": 1, "log_max_mb": 10, "log_backups": 5, "copy_chunk_mb": 8, "checkpoint_mb": 256, "failed_retention_days": 14, "request_timeout": 30}
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
            raw = pick("worker", key, "SYNCHUB_" + key.upper(), "1" if key == "done_retention_hours" else "0")
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

    def validate(self):
        if not self.url or not re.match(r"^https?://[^/\s]+", self.url):
            raise ConfigError("SyncHub's address is missing or wrong. Set url in the [synchub] section (or SYNCHUB_URL), such as http://192.168.1.20:5001")
        if not self.token:
            raise ConfigError("The worker token is missing. Set token in the [synchub] section (or SYNCHUB_TOKEN); add the worker on SyncHub's Workers page to get one.")
        if self.verify not in ("hash", "size"):
            raise ConfigError("verify must be 'hash' (read every copy back and compare checksums) or 'size'.")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("log_level must be DEBUG, INFO, WARNING or ERROR.")
        return self

    @property
    def jobs_dir(self):
        return os.path.join(self.work_dir, "jobs")

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


def detect_capabilities(cfg):
    """What this PC can do: ffmpeg and the encoders that really work (one test frame each), cores, space and the operations the agent knows."""
    caps = {"ops": list(OPS), "cores": os.cpu_count(), "ffmpeg": None, "ffprobe": None, "encoders": [], "vmaf": False, "python": platform.python_version()}
    code, out = run_quiet([cfg.ffmpeg, "-hide_banner", "-version"])
    if code == 0:
        caps["ffmpeg"] = out.splitlines()[0] if out else "ffmpeg"
        code, listing = run_quiet([cfg.ffmpeg, "-hide_banner", "-encoders"])
        have = set(re.findall(r"^\s*V\S*\s+(\S+)", listing, re.M)) if code == 0 else set()
        for name in ("libx265", "libx264", "hevc_nvenc", "h264_nvenc", "hevc_qsv", "h264_qsv", "hevc_vaapi", "h264_vaapi"):
            if name not in have:
                continue
            command = [cfg.ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10"]
            if name.endswith("_vaapi"):
                command = [cfg.ffmpeg, "-hide_banner", "-v", "error", "-vaapi_device", "/dev/dri/renderD128", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10", "-vf", "format=nv12,hwupload"]
            code, _ = run_quiet(command + ["-frames:v", "5", "-c:v", name, "-f", "null", "-"], timeout=30)
            if code == 0:
                caps["encoders"].append(name)
        code, filters = run_quiet([cfg.ffmpeg, "-hide_banner", "-filters"])
        caps["vmaf"] = code == 0 and "libvmaf" in filters
    code, out = run_quiet([cfg.ffprobe, "-version"])
    if code == 0:
        caps["ffprobe"] = out.splitlines()[0] if out else "ffprobe"
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
            if op != "copy":
                raise JobFailure("unsupported_job", f"This agent ({__version__}) cannot do '{op or self.job.get('kind')}' jobs. Update the agent.", retryable=False)
            final = ("complete", {"result": self.do_copy()}, "done")
        except Interrupted:
            self.log.info("Stopping for a restart or shutdown; the job resumes from its last checkpoint", stage=self.stage)
            self.m["state"] = "running"
            self.save()
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
        self.m["keep_until"] = iso(utcnow() + timedelta(seconds=keep)) if outcome in ("failed", "cancelled") else None
        self.save()
        self.agent.deliver(self.job["id"], self.folder)

    # ---- the copy job: copy in, copy out, verified, never overwriting
    def do_copy(self):
        source, dest = self.job.get("source"), self.job.get("dest")
        if not source or not dest:
            raise JobFailure("bad_job", "A copy job needs a source and a destination.", retryable=False)
        monitor = self.agent.monitor
        for root in (source["root"], dest["root"]):
            if self.agent.roots.path(root) is None:
                raise JobFailure("transient", f"SyncHub's share '{root}' is not known to this agent yet. It re-reads the list of shares when it starts.")
            monitor.require(root)
        try:
            src = safe_path(self.agent.roots.path(source["root"]), source["rel"])
            dst = safe_path(self.agent.roots.path(dest["root"]), dest["rel"])
        except ValueError as exc:
            raise JobFailure("bad_job", str(exc), retryable=False)
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

    def copy_out(self, local, dst, size, digest, source_mtime_ns):
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
        if os.path.lexists(dst):
            raise JobFailure("dest_exists", f"Someone created {self.job['dest']['rel']} while it was being copied. The finished copy is kept next to it as {os.path.basename(partial)}.", retryable=False)
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
        self.active = {}                               # job id -> JobRunner
        self.progress = {}                             # job id -> (stage, fraction), for the status summary
        self.lock = threading.Lock()
        self.capabilities = None
        self.started = time.monotonic()
        self.poll_seconds, self.registered, self.roots_hash = 5.0, False, None
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

    def register(self):
        if self.capabilities is None:
            self.capabilities = detect_capabilities(self.cfg)
            self.log.info("Capabilities", **{k: v for k, v in self.capabilities.items() if k != "ops"})
        body = {"protocol": PROTOCOL, "version": __version__, "os": os_name(), "hostname": self.cfg.hostname, "slots": self.cfg.slots, "capabilities": self.capabilities}
        answer = self.api.post("/register", body)
        self.poll_seconds = max(self.MIN_POLL, float(answer.get("poll_seconds") or 5))
        self.roots.update(answer.get("roots"))
        self.roots_hash = answer.get("roots_hash")
        self.monitor.wake.set()
        self.registered = True
        self.log.info(f"Registered with SyncHub as {answer.get('name')}", roots=self.roots.names(), poll_seconds=self.poll_seconds)

    def status(self):
        with self.lock:
            jobs = [{"id": i, "stage": s, "progress": p} for i, (s, p) in sorted(self.progress.items())][:10]
        roots = {name: {"ok": r["ok"], "state": r["state"], "message": (r["message"] or "")[:200], "free_bytes": r["free_bytes"]} for name, r in self.monitor.snapshot().items()}
        return {"version": __version__, "uptime_seconds": int(time.monotonic() - self.started), "work_free_bytes": self.work_free(), "roots": roots, "jobs": jobs}

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
        answer = self.api.post("/poll", body)
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
    log = build_logger(cfg, console=not (args.check or args.init_roots))
    if args.check:
        return command_check(cfg)
    if args.init_roots:
        return command_init_roots(cfg, args.force)
    agent = Agent(cfg, log)
    signal.signal(signal.SIGTERM, agent.stop)
    signal.signal(signal.SIGINT, agent.stop)
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
