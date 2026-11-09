"""Request timing: how long each page takes, so slow pages are found by measuring instead of guessing.

Every page request (not static files, posters or the workers' and players' own polling) is timed. The browser's developer tools show the figures in the
`Server-Timing` header, a request slower than SLOW_MS is written to the log, and the last RECENT requests are kept in memory for the Tools card
"Page timings". Nothing is stored in the database and nothing is sent anywhere. Database time counts the `execute` calls made through SyncHub's tracked
connections: rows fetched afterwards are not in it, so it is a lower bound; the query count is exact."""
import threading
import time
from collections import defaultdict, deque

from logger import logger

SLOW_MS = 1000
RECENT = 300
SKIP_PREFIXES = ("static", "poster", "worker_api_", "player_api_")

_lock = threading.Lock()
_recent = deque(maxlen=RECENT)
_local = threading.local()


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


def finish(response):
    """Adds the Server-Timing header and keeps the figures. Returns the response."""
    state = getattr(_local, "state", None)
    _local.state = None
    if state is None:
        return response
    total = (time.perf_counter() - state["start"]) * 1000
    db_ms = state["db"] * 1000
    response.headers["Server-Timing"] = f'total;dur={total:.1f}, db;dur={db_ms:.1f};desc="{state["queries"]} queries"'
    entry = {"at": time.strftime("%H:%M:%S"), "endpoint": state["endpoint"], "method": state["method"], "path": state["path"][:120], "status": response.status_code,
             "ms": round(total, 1), "db_ms": round(db_ms, 1), "queries": state["queries"], "bytes": response.calculate_content_length() or 0}
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
