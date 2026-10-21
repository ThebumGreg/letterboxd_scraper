"""Runnable jobs. Each records progress in task_status and reports failures instead of hiding them."""
import logging
import threading

from synchub import db, settings
from synchub.clients.arr import RadarrClient, SonarrClient
from synchub.clients.jellyfin import JellyfinClient
from synchub.ingest import jellyfin as jf_ingest
from synchub.ingest import letterboxd as lb_ingest
from synchub.ingest import radarr as radarr_ingest
from synchub.ingest import serializd as sz_ingest
from synchub.ingest import sonarr as sonarr_ingest

logger = logging.getLogger(__name__)
_LOCKS = {}


def set_status(task_name, status, message=""):
    now = db.now()
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO task_status (task_name, status, started_at, last_run, message) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(task_name) DO UPDATE SET
                 status = excluded.status,
                 started_at = CASE WHEN excluded.status = 'in_progress' THEN excluded.started_at ELSE task_status.started_at END,
                 last_run = CASE WHEN excluded.status IN ('completed', 'failed') THEN excluded.last_run ELSE task_status.last_run END,
                 message = excluded.message""",
            (task_name, status, now if status == "in_progress" else None, now, message),
        )


def reset_stale():
    """Call once at process start: jobs left 'in_progress' by a previous run can never finish."""
    with db.connection() as conn:
        conn.execute("UPDATE task_status SET status = 'failed', message = 'Interrupted by a restart.' WHERE status IN ('in_progress', 'pending')")


def run(task_name, label, fn):
    """Runs fn() as a tracked job; fn returns a short summary string. Only one run of a task at a time."""
    lock = _LOCKS.setdefault(task_name, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.info(f"{task_name} is already running; skipped.")
        return None
    set_status(task_name, "in_progress", f"{label}...")
    try:
        summary = fn()
        set_status(task_name, "completed", summary or f"{label} finished.")
        return summary
    except Exception as exc:
        logger.error(f"{task_name} failed: {exc}")
        set_status(task_name, "failed", str(exc))
        return None
    finally:
        lock.release()


def _jellyfin(scope, label):
    if settings.get("disable_jellyfin_connection") == "1":
        return "Jellyfin integration is disabled in Settings."
    client = JellyfinClient()
    with db.connection() as conn:
        result = jf_ingest.sync(conn, client, scope)
    return f"{label}: {result['seen']} item(s) synced, {result['removed']} no longer reported."


def sync_jellyfin_movies():
    return run("jellyfin_movies", "Syncing Jellyfin movies", lambda: _jellyfin("movies", "Jellyfin movies"))


def sync_jellyfin_tv():
    return run("jellyfin_tv", "Syncing Jellyfin TV", lambda: _jellyfin("tv", "Jellyfin TV"))


def sync_radarr():
    def work():
        client = RadarrClient()
        with db.connection() as conn:
            lib = radarr_ingest.sync_library(conn, client)
            queue = radarr_ingest.sync_queue(conn, client)
        return f"Radarr: {lib['seen']} movie(s), {queue['seen']} in queue."
    return run("radarr", "Syncing Radarr", work)


def sync_sonarr():
    def work():
        client = SonarrClient()
        with db.connection() as conn:
            lib = sonarr_ingest.sync_library(conn, client)
            queue = sonarr_ingest.sync_queue(conn, client)
        note = f" ({len(lib['episode_fetch_errors'])} series had episode errors)" if lib["episode_fetch_errors"] else ""
        return f"Sonarr: {lib['series']} series, {lib['episodes']} episode(s), {queue['seen']} in queue{note}."
    return run("sonarr", "Syncing Sonarr", work)


def scrape_letterboxd():
    def work():
        summary = lb_ingest.sync_all()
        if not summary:
            return "No active profile has a Letterboxd username."
        incomplete = [f"{u}/{l}" for u, lists in summary.items() for l, r in lists.items() if not r["complete"]]
        note = f" Incomplete: {', '.join(incomplete)}." if incomplete else ""
        return f"Letterboxd: {sum(r['seen'] for lists in summary.values() for r in lists.values())} entries scraped.{note}"
    return run("scraper_letterboxd", "Scraping Letterboxd", work)


def scrape_serializd():
    def work():
        summary = sz_ingest.sync_all()
        if not summary:
            return "No active profile has a Serializd username."
        failed = [u for u, r in summary.items() if "error" in r]
        note = f" Failed: {', '.join(failed)}." if failed else ""
        return f"Serializd: {sum(r.get('seen', 0) for r in summary.values())} shows scraped.{note}"
    return run("scraper_serializd", "Scraping Serializd", work)
