"""Request timing: how long each page takes, so slow pages are found by measuring instead of guessing.

Every page request (not static files, posters or the workers' and players' own polling) is timed. The browser's developer tools show the figures in the
`Server-Timing` header, a request slower than SLOW_MS is written to the log, and the last RECENT requests are kept in memory for the Tools card
"Page timings". Nothing is stored in the database and nothing is sent anywhere. Database time counts the `execute` calls made through SyncHub's tracked
connections: rows fetched afterwards are not in it, so it is a lower bound; the query count is exact."""
import sys
import threading
import time
import traceback
from collections import defaultdict, deque

from logger import logger

SLOW_MS = 1000
RECENT = 300
SKIP_PREFIXES = ("static", "poster", "worker_api_", "player_api_")

WATCHDOG_AFTER = 20          # seconds a page may take before the watchdog writes what every thread is doing
WATCHDOG_EVERY = 120         # and then stays quiet this long

_lock = threading.Lock()
_recent = deque(maxlen=RECENT)
_local = threading.local()
_inflight = {}               # thread id -> (started, method, path)
_watchdog = {"thread": None, "last": 0.0}


def note_query(seconds):
    """Called by the tracked connection for every execute; counts toward the request running on this thread, if any."""
    state = getattr(_local, "state", None)
    if state is not None:
        state["queries"] += 1
        state["db"] += seconds


def begin(endpoint, method, path):
    if endpoint is None or endpoint.startswith(SKIP_PREFIXES):
        _local.state = None
        return
    _local.state = {"endpoint": endpoint, "method": method, "path": path, "start": time.perf_counter(), "queries": 0, "db": 0.0}
    with _lock:
        _inflight[threading.get_ident()] = (time.monotonic(), method, path)


def finish(response):
    """Adds the Server-Timing header and keeps the figures. Returns the response."""
    state = getattr(_local, "state", None)
    _local.state = None
    with _lock:
        _inflight.pop(threading.get_ident(), None)
    if state is None:
        return response
    total = (time.perf_counter() - state["start"]) * 1000
    db_ms = state["db"] * 1000
    response.headers["Server-Timing"] = f'total;dur={total:.1f}, db;dur={db_ms:.1f};desc="{state["queries"]} queries"'
    entry = {"at": time.strftime("%H:%M:%S"), "endpoint": state["endpoint"], "method": state["method"], "path": state["path"][:120], "status": response.status_code,
             "ms": round(total, 1), "db_ms": round(db_ms, 1), "queries": state["queries"], "bytes": (response.calculate_content_length() or 0) if response.is_sequence else 0}
    with _lock:
        _recent.append(entry)
    if total >= SLOW_MS:
        logger.warning(f"Slow page: {state['method']} {state['path']} took {total:.0f} ms ({state['queries']} queries, {db_ms:.0f} ms in execute)")
    return response


def _percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def report():
    """{'pages': [per endpoint: count, average, 95th percentile, slowest, queries], 'slowest': [the slowest single requests], 'count': n}"""
    with _lock:
        rows = list(_recent)
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["endpoint"]].append(r)
    pages = []
    for endpoint, items in grouped.items():
        times = [i["ms"] for i in items]
        pages.append({"endpoint": endpoint, "path": items[-1]["path"].split("?")[0], "count": len(items), "avg_ms": round(sum(times) / len(times), 1), "p95_ms": _percentile(times, 0.95),
                      "max_ms": max(times), "avg_queries": round(sum(i["queries"] for i in items) / len(items), 1), "avg_kb": round(sum(i["bytes"] for i in items) / len(items) / 1024, 1)})
    pages.sort(key=lambda p: -p["max_ms"])
    return {"count": len(rows), "slow_ms": SLOW_MS, "pages": pages[:25], "slowest": sorted(rows, key=lambda r: -r["ms"])[:15]}


def reset():
    with _lock:
        _recent.clear()


def thread_dump(limit=14):
    """What every thread of the app is doing right now, as text: the innermost `limit` calls of each, so a slow page can be traced to the code holding it up."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        stack = traceback.format_stack(frame)[-limit:]
        parts.append(f"--- {names.get(ident, ident)}\n" + "".join(stack).rstrip())
    return "\n".join(parts)


def _watch():
    while True:
        time.sleep(5)
        now = time.monotonic()
        with _lock:
            slow = [(now - began, method, path) for began, method, path in _inflight.values() if now - began > WATCHDOG_AFTER]
        if slow and now - _watchdog["last"] > WATCHDOG_EVERY:
            _watchdog["last"] = now
            slow.sort(reverse=True)
            logger.warning(f"Slow request watchdog: {len(slow)} request(s) have run over {WATCHDOG_AFTER} s (longest: {slow[0][1]} {slow[0][2]}, {slow[0][0]:.0f} s). "
                           f"What every thread is doing:\n{thread_dump()}")


def start_watchdog():
    """Starts the watchdog once: when a page has been running for over 20 seconds it writes every thread's stack to the log (at most every two minutes)."""
    with _lock:
        if _watchdog["thread"] is not None:
            return
        _watchdog["thread"] = threading.Thread(target=_watch, name="slow-request-watchdog", daemon=True)
        _watchdog["thread"].start()
