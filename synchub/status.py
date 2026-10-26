"""One picture of whether SyncHub is healthy: connections, workers, the work queue, syncs and disk space.

Nothing here calls out to another service. Connections are judged from their settings and from how their last sync went, so the dashboard
can show a single dot without waiting on the network."""
import shutil
from datetime import datetime, timezone
from pathlib import Path

from synchub import db, settings, workers

OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"
SEVERITY = {OK: 0, OFF: 0, WARN: 1, BAD: 2}
STALE_SYNC_HOURS = 36                       # the syncs run every few hours; a day and a half without one means something is stuck
LOW_DISK_GB = 10

# connection -> (settings that must be filled in, the tasks that read from it)
CONNECTIONS = (
    ("Jellyfin", ("jellyfin_url", "jellyfin_api_key"), ("jellyfin_movies", "jellyfin_tv", "watch_state")),
    ("Radarr", ("radarr_url", "radarr_api_key"), ("radarr",)),
    ("Sonarr", ("sonarr_url", "sonarr_api_key"), ("sonarr",)),
    ("TMDB", ("tmdb_api_key",), ()),
)


def _hours_since(text, now):
    try:
        when = datetime.strptime((text or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - when).total_seconds() / 3600


def _connection(conn, name, keys, task_names, now):
    if name == "Jellyfin" and settings.get("disable_jellyfin_connection", conn=conn) == "1":
        return {"label": name, "state": OFF, "detail": "Switched off in Settings", "href": "/settings"}
    if not all(settings.get(k, conn=conn) for k in keys):
        return {"label": name, "state": OFF, "detail": "Not set up", "href": "/settings"}
    failed = []
    latest = None
    for task in task_names:
        row = conn.execute("SELECT status, last_run, message FROM task_status WHERE task_name = ?", (task,)).fetchone()
        if not row:
            continue
        if row["status"] == "failed":
            failed.append(row["message"] or task)
        if row["last_run"] and (latest is None or row["last_run"] > latest):
            latest = row["last_run"]
    if failed:
        return {"label": name, "state": BAD, "detail": f"Last sync failed: {failed[0][:140]}", "href": "/tools"}
    if task_names and latest is None:
        return {"label": name, "state": WARN, "detail": "Set up, but it has not synced yet", "href": "/tools"}
    age = _hours_since(latest, now) if latest else None
    if age is not None and age > STALE_SYNC_HOURS:
        return {"label": name, "state": WARN, "detail": f"Last synced {int(age // 24)} days ago" if age >= 48 else f"Last synced {int(age)} hours ago", "href": "/tools"}
    return {"label": name, "state": OK, "detail": f"Last synced {latest} UTC" if latest else "Set up", "href": "/tools"}


def _worker_items(conn):
    items = []
    fleet = [w for w in workers.list_all(conn) if not w["revoked"]]
    enabled = [w for w in fleet if w["enabled"]]
    if not enabled:
        return [{"label": "Workers", "state": OFF, "detail": "No workers added", "href": "/workers"}]
    online = [w for w in enabled if w["online"]]
    state = OK if len(online) == len(enabled) else WARN if online else BAD
    names = ", ".join(w["name"] for w in enabled if not w["online"])
    items.append({"label": "Workers", "state": state, "detail": f"{len(online)} of {len(enabled)} online" + (f" (offline: {names})" if names else ""), "href": "/workers"})
    return items


def _queue_items(conn):
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM job GROUP BY status")}
    queued, running, review, failed = (counts.get(k, 0) for k in ("queued", "running", "needs_review", "failed"))
    recent_failed = conn.execute("SELECT COUNT(*) AS n FROM job WHERE status = 'failed' AND COALESCE(finished_at, created_at) >= datetime('now', '-1 day')").fetchone()["n"] if failed else 0
    out = [{"label": "Work queue", "state": OK, "detail": f"{running} running, {queued} waiting", "href": "/library"}]
    out.append({"label": "Waiting for you", "state": WARN if review else OK, "detail": f"{review} job{' needs' if review == 1 else 's need'} a decision" if review else "Nothing to approve", "href": "/library"})
    if recent_failed:
        out.append({"label": "Failed jobs", "state": BAD, "detail": f"{recent_failed} failed in the last day", "href": "/library"})
    return out


def _disk_items():
    try:
        usage = shutil.disk_usage(Path(db.DB_FILE).parent)
    except OSError:
        return []
    free_gb = usage.free / 1e9
    return [{"label": "SyncHub disk", "state": BAD if free_gb < 1 else WARN if free_gb < LOW_DISK_GB else OK,
             "detail": f"{free_gb:.0f} GB free of {usage.total / 1e9:.0f} GB (where the database lives)", "href": None}]


def snapshot(conn, now=None):
    """{'items': [{'label', 'state', 'detail', 'href'}], 'state': worst state, 'attention': how many items need a look, 'summary': one sentence}."""
    now = now or datetime.now(timezone.utc)
    items = [_connection(conn, n, k, t, now) for n, k, t in CONNECTIONS]
    items += _worker_items(conn) + _queue_items(conn) + _disk_items()
    worst = max((i["state"] for i in items), key=lambda s: SEVERITY[s], default=OK)
    attention = sum(1 for i in items if SEVERITY[i["state"]] > 0)
    summary = "All systems OK" if not attention else f"{attention} thing{'' if attention == 1 else 's'} need attention"
    return {"items": items, "state": OK if SEVERITY[worst] == 0 else worst, "attention": attention, "summary": summary}
