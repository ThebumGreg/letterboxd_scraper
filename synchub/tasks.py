"""Runnable jobs. Each records progress in task_status, logs what it is doing and reports failures instead of hiding them."""
import inspect
import logging
import threading
import time

from synchub import automation, db, episodes, matching, metadata, settings, states
from synchub.clients import ConfigError
from synchub.clients.arr import RadarrClient, SonarrClient
from synchub.clients.jellyfin import JellyfinClient
from synchub.clients.tmdb import TmdbClient
from synchub.ingest import jellyfin as jf_ingest
from synchub.ingest import letterboxd as lb_ingest
from synchub.ingest import letterboxd_ids
from synchub.ingest import radarr as radarr_ingest
from synchub.ingest import serializd as sz_ingest
from synchub.ingest import sonarr as sonarr_ingest

logger = logging.getLogger(__name__)
_LOCKS = {}
_STARTED = {}


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


def update_message(task_name, message):
    """Changes only the message shown for a running task (does not touch its start time or status)."""
    with db.connection() as conn:
        conn.execute("UPDATE task_status SET message = ? WHERE task_name = ?", (message, task_name))


class Reporter:
    """Progress for one task: every report goes to the log, and (at most every `interval` seconds) to the Tools page."""

    def __init__(self, task_name, interval=2.0):
        self.task_name, self.interval, self._last = task_name, interval, 0.0

    def __call__(self, message, force=False):
        logger.info(f"[{self.task_name}] {message}")
        if force or time.monotonic() - self._last >= self.interval:
            self._last = time.monotonic()
            try:
                update_message(self.task_name, message)
            except Exception as exc:      # progress is a nicety; never fail the task over it
                logger.debug(f"Could not store progress for {self.task_name}: {exc}")


def run(task_name, label, fn):
    """Runs fn() (or fn(report), if it takes one argument) as a tracked job and returns its summary string.

    Logs the start, the duration and, on failure, the full traceback. Only one run of a task at a time.
    """
    lock = _LOCKS.setdefault(task_name, threading.Lock())
    if not lock.acquire(blocking=False):
        running_for = time.monotonic() - _STARTED.get(task_name, time.monotonic())
        logger.info(f"[{task_name}] is already running (for {running_for:.0f}s); this request was skipped.")
        return None
    _STARTED[task_name] = started = time.monotonic()
    report = Reporter(task_name)
    logger.info(f"[{task_name}] {label}: started")
    try:
        set_status(task_name, "in_progress", f"{label}...")
        summary = fn(report) if len(inspect.signature(fn).parameters) else fn()
        logger.info(f"[{task_name}] finished in {time.monotonic() - started:.1f}s: {summary or label}")
        set_status(task_name, "completed", summary or f"{label} finished.")
        return summary
    except Exception as exc:
        logger.error(f"[{task_name}] failed after {time.monotonic() - started:.1f}s: {exc}", exc_info=True)
        try:
            set_status(task_name, "failed", str(exc))
        except Exception as status_exc:
            logger.error(f"[{task_name}] could not record the failure: {status_exc}")
        return None
    finally:
        _STARTED.pop(task_name, None)
        lock.release()


def _jellyfin(scope, label, report):
    if settings.get("disable_jellyfin_connection") == "1":
        return "Jellyfin integration is disabled in Settings."
    client = JellyfinClient()
    report("Reading items from Jellyfin...", force=True)
    with db.connection() as conn:
        result = jf_ingest.sync(conn, client, scope, progress=report)
    return f"{label}: {result['seen']} item(s) synced, {result['removed']} no longer reported."


def sync_jellyfin_movies():
    return run("jellyfin_movies", "Syncing Jellyfin movies", lambda report: _jellyfin("movies", "Jellyfin movies", report))


def sync_jellyfin_tv():
    return run("jellyfin_tv", "Syncing Jellyfin TV", lambda report: _jellyfin("tv", "Jellyfin TV", report))


def _link_now():
    """Links newly synced records so the auto-add rules see an up-to-date picture (no network)."""
    with db.connection() as conn:
        matching.Matcher(conn, None).resolve_all()


def _summarise(result, noun):
    parts = []
    if result.get("added"):
        parts.append(f"added {len(result['added'])} {noun}")
    if result.get("would_add"):
        parts.append(f"dry run: would add {len(result['would_add'])}")
    if result.get("errors"):
        parts.append(f"{len(result['errors'])} could not be added")
    if result.get("skipped_no_tvdb"):
        parts.append(f"{len(result['skipped_no_tvdb'])} skipped (no TVDB ID)")
    return (" Auto-add: " + ", ".join(parts) + ".") if parts else ""


def _radarr_pull(client, report):
    """Library and queue, each in its own short transaction (never a network call with writes pending)."""
    report("Reading the Radarr library...", force=True)
    with db.connection() as conn:
        lib = radarr_ingest.sync_library(conn, client)
    report(f"Radarr library: {lib['seen']} movie(s). Reading the queue...", force=True)
    with db.connection() as conn:
        queue = radarr_ingest.sync_queue(conn, client)
    return lib, queue


def _sonarr_pull(client, report):
    report("Reading the Sonarr library and episodes...", force=True)
    with db.connection() as conn:
        lib = sonarr_ingest.sync_library(conn, client, progress=report)
    report(f"Sonarr library: {lib['series']} series, {lib['episodes']} episode(s). Reading the queue...", force=True)
    with db.connection() as conn:
        queue = sonarr_ingest.sync_queue(conn, client)
    return lib, queue


def sync_radarr():
    def work(report):
        client = RadarrClient()
        lib, queue = _radarr_pull(client, report)
        note = ""
        if settings.get("radarr_enable_queue_addition") == "1":
            report("Checking which wanted movies should be added...", force=True)
            _link_now()
            with db.connection() as conn:
                result = automation.run_radarr_additions(conn, client, dry_run=settings.get("radarr_dry_run") == "1")
            if result["added"]:
                _radarr_pull(client, report)
            note = _summarise(result, "movie(s)")
        return f"Radarr: {lib['seen']} movie(s), {queue['seen']} in queue.{note}"
    return run("radarr", "Syncing Radarr", work)


def sync_sonarr():
    def work(report):
        client = SonarrClient()
        lib, queue = _sonarr_pull(client, report)
        errors = f" ({len(lib['episode_fetch_errors'])} series had episode errors)" if lib["episode_fetch_errors"] else ""
        note = ""
        if settings.get("sonarr_enable_queue_addition") == "1":
            report("Checking which wanted shows should be added...", force=True)
            _link_now()
            with db.connection() as conn:
                result = automation.run_sonarr_additions(conn, client, dry_run=settings.get("sonarr_dry_run") == "1")
            if result["added"]:
                _sonarr_pull(client, report)
            note = _summarise(result, "show(s)")
        return f"Sonarr: {lib['series']} series, {lib['episodes']} episode(s), {queue['seen']} in queue{errors}.{note}"
    return run("sonarr", "Syncing Sonarr", work)


def resync_sonarr():
    """Used right after adding a series: pull Sonarr's library and map its episodes (no TMDB needed)."""
    client = SonarrClient()
    with db.connection() as conn:
        sonarr_ingest.sync_library(conn, client)
    _link_now()
    with db.connection() as conn:
        episodes.map_sonarr_episodes(conn)


def scrape_letterboxd():
    def work(report):
        summary = lb_ingest.sync_all(progress=report)
        if not summary:
            return "No active profile has a Letterboxd username."
        incomplete = [f"{u}/{l}" for u, lists in summary.items() for l, r in lists.items() if not r["complete"]]
        note = f" Incomplete: {', '.join(incomplete)}." if incomplete else ""
        return f"Letterboxd: {sum(r['seen'] for lists in summary.values() for r in lists.values())} entries scraped.{note}"
    return run("scraper_letterboxd", "Scraping Letterboxd", work)


def scrape_serializd():
    def work(report):
        summary = sz_ingest.sync_all(progress=report)
        if not summary:
            return "No active profile has a Serializd username."
        failed = [u for u, r in summary.items() if "error" in r]
        note = f" Failed: {', '.join(failed)}." if failed else ""
        return f"Serializd: {sum(r.get('seen', 0) for r in summary.values())} shows scraped.{note}"
    return run("scraper_serializd", "Scraping Serializd", work)


def resolve_letterboxd_ids():
    def work(report):
        import random
        low, high = float(settings.get("request_delay_min") or 2.5), float(settings.get("request_delay_max") or 4.5)
        with db.connection() as conn:
            stats = letterboxd_ids.resolve(conn, pause=lambda: time.sleep(random.uniform(min(low, high), max(low, high))), progress=report)
        note = " Letterboxd blocked further requests." if stats["blocked"] else ""
        return f"Checked {stats['checked']} film(s): {stats['found']} IDs found, {stats['no_id']} without an ID, {stats['errors']} errors.{note}"
    return run("id_resolver", "Resolving Letterboxd IDs", work)


def _stage(name, report, fn):
    started = time.monotonic()
    report(f"{name}...", force=True)
    result = fn()
    logger.info(f"[reconcile] {name} done in {time.monotonic() - started:.1f}s: {result}")
    return result


def reconcile_now(tmdb=None, report=lambda message, force=False: None):
    """Links sources to media, refreshes TMDB metadata, links episodes, then recomputes states. Returns per-stage stats.

    Each stage commits on its own (and the matcher/metadata stages commit as they go), so slow network calls
    never happen while the database write lock is held.
    """
    result = {}

    def matching_stage():
        with db.connection() as conn:
            return matching.Matcher(conn, tmdb).resolve_all(progress=report)

    def metadata_stage():
        with db.connection() as conn:
            return metadata.refresh_pending(conn, tmdb, progress=report)

    def link_stage():
        with db.connection() as conn:
            return {"jellyfin_episodes": episodes.link_jellyfin_episodes(conn), "sonarr_episodes": episodes.map_sonarr_episodes(conn)}

    def state_stage():
        with db.connection() as conn:
            return {"queue_events": states.sync_queue_events(conn), "states": states.compute(conn)}

    result["matching"] = _stage("Matching titles to movies and shows", report, matching_stage)
    if tmdb is not None:
        result["metadata"] = _stage("Fetching TMDB details and episode lists", report, metadata_stage)
    linked = _stage("Linking episodes", report, link_stage)
    result.update(linked)
    result.update(_stage("Working out statuses", report, state_stage))
    return result


def reconcile():
    def work(report):
        try:
            tmdb = TmdbClient()
        except ConfigError:
            tmdb = None
        r = reconcile_now(tmdb, report)
        note = "" if tmdb else " TMDB is not configured: shows without a TMDB ID and episode lists are waiting for it."
        m = r["matching"]
        return (f"Linked {m.get('linked', 0) + m.get('relinked', 0)} source record(s), {m.get('unresolved', 0)} unresolved, "
                f"{m.get('review_added', 0)} new review item(s).{note}")
    return run("reconcile", "Reconciling", work)


def with_reconcile(*steps):
    """A job that runs the given steps and then reconciles, so the dashboard reflects the new data."""
    def job():
        for step in steps:
            step()
        reconcile()
    job.__name__ = "_then_".join(step.__name__ for step in steps) + "_then_reconcile"
    return job


def run_all():
    """The full sequence, one step after another. A failing step is recorded and the rest still run."""
    def work(report):
        # Reconcile before Radarr/Sonarr so their auto-add rules see wanted titles already linked, and again afterwards.
        steps = (sync_jellyfin_movies, sync_jellyfin_tv, scrape_letterboxd, scrape_serializd, resolve_letterboxd_ids, reconcile,
                 sync_radarr, sync_sonarr, reconcile)
        for number, step in enumerate(steps, 1):
            report(f"Step {number} of {len(steps)}: {step.__name__.replace('_', ' ')}", force=True)
            step()
        with db.connection() as conn:
            failed = [r["task_name"] for r in conn.execute("SELECT task_name FROM task_status WHERE status = 'failed' AND task_name <> 'all'")]
        return "All steps finished." + (f" Failed: {', '.join(failed)}." if failed else "")
    return run("all", "Running everything", work)


# ---------------------------------------------------------------- helpers for the web layer
TASK_NAMES = ("jellyfin_movies", "jellyfin_tv", "radarr", "sonarr", "scraper_letterboxd", "scraper_serializd", "id_resolver", "reconcile", "all")


def all_statuses():
    """Every known task, idle unless it has run."""
    statuses = {name: {"task_name": name, "status": "idle", "started_at": None, "last_run": None, "message": "No active logs"} for name in TASK_NAMES}
    with db.connection() as conn:
        for row in conn.execute("SELECT * FROM task_status"):
            statuses[row["task_name"]] = dict(row)
    return statuses


def start_async(fn):
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


_REFRESH_LOCK = threading.Lock()
_REFRESH_RUNNING, _REFRESH_PENDING = set(), set()


def refresh_soon(service, job, spawn=start_async):
    """Runs job in the background, coalescing a burst of webhook events: at most one run at a time per service,
    plus one more run if events arrived while it was running."""
    with _REFRESH_LOCK:
        if service in _REFRESH_RUNNING:
            _REFRESH_PENDING.add(service)
            return False
        _REFRESH_RUNNING.add(service)

    def loop():
        try:
            while True:
                try:
                    job()
                except Exception as exc:
                    logger.error(f"Refresh after {service} event failed: {exc}")
                with _REFRESH_LOCK:
                    if service in _REFRESH_PENDING:
                        _REFRESH_PENDING.discard(service)
                        continue
                    _REFRESH_RUNNING.discard(service)
                    return
        except BaseException:
            with _REFRESH_LOCK:
                _REFRESH_RUNNING.discard(service)
            raise

    spawn(loop)
    return True


def refresh_media(media_id, tmdb=None):
    """Re-reads one movie/show from TMDB right now, then relinks its episodes and recomputes states. Returns a short message."""
    tmdb = tmdb or TmdbClient()
    with db.connection() as conn:
        row = conn.execute("SELECT kind FROM media WHERE id = ?", (media_id,)).fetchone()
        if not row:
            raise LookupError("Unknown media.")
        if row["kind"] == "movie":
            found = metadata.refresh_movie(conn, tmdb, media_id)
            message = "Movie details refreshed." if found else "TMDB does not know this movie."
        else:
            result = metadata.refresh_show(conn, tmdb, media_id)
            message = f"Refreshed {result['episodes']} episode(s) from TMDB." if result["found"] else "TMDB does not know this show."
    with db.connection() as conn:
        episodes.link_jellyfin_episodes(conn)
        episodes.map_sonarr_episodes(conn)
        states.compute(conn)
    return message
