"""A small read-only status report for dashboards and uptime monitors (Homepage, Uptime Kuma).

`GET /healthz` is open and says nothing but ok or down. `GET /api/status` needs `Authorization: Bearer <STATUS_TOKEN>` and is switched off (404)
until STATUS_TOKEN is set. The report holds counts and times only: no names, titles, paths, addresses or error text. Nothing here writes to the
database or starts work, and the computed report is kept for 15 seconds so polling cannot load the database.
"""
import hmac
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone

import requests

from synchub import __version__, buildinfo, db, settings, workers

logger = logging.getLogger(__name__)

APP = "synchub"
CHECK_TIMEOUT = 2             # seconds each dependency check may take
CACHE_SECONDS = 15
TOKEN_ENV = "STATUS_TOKEN"
_STARTED = time.monotonic()
_RANK = {"ok": 0, "degraded": 1, "down": 2}
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="status")
_cache = {"at": None, "report": None}
_lock = threading.Lock()


def token():
    return (os.environ.get(TOKEN_ENV) or "").strip()


def token_matches(header):
    """True when the Authorization header carries STATUS_TOKEN (compared in constant time)."""
    given = header[7:].strip() if (header or "")[:7].lower() == "bearer " else ""
    expected = token()
    return bool(expected) and bool(given) and hmac.compare_digest(given.encode(), expected.encode())


def _iso(stamp):
    """A stored 'YYYY-MM-DD HH:MM:SS' (UTC) as '...Z', or None."""
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _timed(function):
    """(result, milliseconds) of a call, or (None, ms) with the exception's kind when it failed."""
    start = time.monotonic()
    try:
        return function(), round((time.monotonic() - start) * 1000)
    except Exception as exc:
        logger.debug(f"Status check failed: {type(exc).__name__}")
        return exc, round((time.monotonic() - start) * 1000)


def _database_and_metrics():
    """One short read-only look at the database: the counts for the metrics."""
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connection(timeout=CHECK_TIMEOUT) as conn:
        conn.execute("SELECT 1").fetchone()
        count = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0   # noqa: E731
        queued = count("SELECT COUNT(*) FROM job WHERE status = 'queued'")
        running = count("SELECT COUNT(*) FROM job WHERE status = 'running'")
        failed = count("SELECT COUNT(*) FROM job WHERE status = 'failed' AND finished_at >= ?", day_ago)
        finished = conn.execute("SELECT MAX(finished_at) FROM job WHERE status = 'done'").fetchone()[0]
        seen = [r[0] for r in conn.execute("SELECT last_seen FROM worker WHERE enabled = 1 AND revoked_at IS NULL")]
        jellyfin_url = (settings.get("jellyfin_url", conn=conn) or "").strip()
    online = sum(1 for s in seen if workers.is_online(s))
    return {"jobs_queued": queued, "jobs_running": running, "jobs_failed_24h": failed, "workers_online": online, "workers_total": len(seen),
            "last_job_finished_at": _iso(finished)}, jellyfin_url


def _jellyfin(url):
    """Jellyfin's public ping: no key is sent and nothing is stored."""
    reply = requests.get(url.rstrip("/") + "/System/Info/Public", timeout=CHECK_TIMEOUT)
    return reply.status_code


def _build():
    checks, metrics = {}, {"jobs_queued": None, "jobs_running": None, "jobs_failed_24h": None, "workers_online": None, "workers_total": None, "last_job_finished_at": None}
    future = _pool.submit(_timed, _database_and_metrics)
    try:
        outcome, ms = future.result(timeout=CHECK_TIMEOUT + 0.5)
    except FutureTimeout:
        outcome, ms = TimeoutError(), CHECK_TIMEOUT * 1000
    jellyfin_url = ""
    if isinstance(outcome, Exception):
        checks["database"] = {"status": "down", "latency_ms": ms, "detail": "timeout" if isinstance(outcome, TimeoutError) else "unreachable"}
    else:
        (found, jellyfin_url), checks["database"] = outcome, {"status": "ok", "latency_ms": ms}
        metrics.update(found)
        if found["workers_total"] and not found["workers_online"]:
            checks["workers"] = {"status": "degraded", "detail": "no worker online"}
        else:
            checks["workers"] = {"status": "ok"}
    if jellyfin_url.lower().startswith(("http://", "https://")):
        future = _pool.submit(_timed, lambda: _jellyfin(jellyfin_url))
        try:
            outcome, ms = future.result(timeout=CHECK_TIMEOUT + 0.5)
        except FutureTimeout:
            outcome, ms = TimeoutError(), CHECK_TIMEOUT * 1000
        if isinstance(outcome, int) and outcome < 500:
            checks["jellyfin"] = {"status": "ok", "latency_ms": ms}
        else:
            checks["jellyfin"] = {"status": "degraded", "latency_ms": ms, "detail": "timeout" if isinstance(outcome, (TimeoutError, requests.Timeout)) else "unreachable"}
    worst = max((c["status"] for c in checks.values()), key=_RANK.get, default="ok")
    build = buildinfo.get()
    return {"app": APP, "version": build["label"] or __version__, "status": worst, "checks": checks, "metrics": metrics}


def report():
    """The status report, at most 15 seconds old. `checked_at` and `uptime_seconds` are those of the moment it was computed."""
    with _lock:
        if _cache["report"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["report"]
        body = _build()
        body["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body["uptime_seconds"] = int(time.monotonic() - _STARTED)
        ordered = {k: body[k] for k in ("app", "version", "status", "checked_at", "uptime_seconds", "checks", "metrics")}
        _cache["at"], _cache["report"] = time.monotonic(), ordered
        return ordered


def clear_cache():
    with _lock:
        _cache["at"], _cache["report"] = None, None
