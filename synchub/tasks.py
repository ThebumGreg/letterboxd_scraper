"""Runnable jobs. Each records progress in task_status, logs what it is doing and reports failures instead of hiding them."""
import inspect
import logging
import threading
import time

from synchub import automation, db, episodes, matching, metadata, posters, ratings, settings, states, watchlist
from synchub.clients import ConfigError
from synchub.clients.arr import RadarrClient, SonarrClient
from synchub.clients.jellyfin import JellyfinClient
from synchub.clients.tmdb import TmdbClient
from synchub.ingest import jellyfin as jf_ingest
from synchub.ingest import jellyfin_userdata as userdata_ingest
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


def _remember_server_id(client):
    """Links that open the Jellyfin app need the server's ID. It is read here, once per sync, never while a page renders."""
    try:
        server_id = (client.system_info() or {}).get("Id")
    except Exception as exc:
        logger.warning(f"Could not read the Jellyfin server ID (links that open the Jellyfin app need it): {exc}")
        return
    if server_id and server_id != settings.get("jellyfin_server_id"):
        settings.set_value("jellyfin_server_id", server_id)


def _jellyfin(scope, label, report):
    if settings.get("disable_jellyfin_connection") == "1":
        return "Jellyfin integration is disabled in Settings."
    client = JellyfinClient()
    _remember_server_id(client)
    report("Reading items from Jellyfin...", force=True)
    with db.connection() as conn:
        result = jf_ingest.sync(conn, client, scope, progress=report)
    return f"{label}: {result['seen']} item(s) synced, {result['removed']} no longer reported."


def backup_database():
    """A consistent copy of the database in the backups folder, keeping the newest few (Settings, Automation, 'Database backups')."""
    def work():
        from synchub import backup
        keep = backup.keep_count()
        if not keep:
            return "Backups are turned off in Settings."
        path = backup.create()
        removed = backup.prune(keep)
        return f"Saved {path.name} ({path.stat().st_size / 1048576:.1f} MB)." + (f" Removed {len(removed)} older {'copy' if len(removed) == 1 else 'copies'}." if removed else "")
    return run("backup", "Backing up the database", work)


def sync_jellyfin_movies():
    return run("jellyfin_movies", "Syncing Jellyfin movies", lambda report: _jellyfin("movies", "Jellyfin movies", report))


def sync_jellyfin_tv():
    return run("jellyfin_tv", "Syncing Jellyfin TV", lambda report: _jellyfin("tv", "Jellyfin TV", report))


def sync_watch_state():
    """Copies what each active profile's Jellyfin user has watched, and where they stopped, so the title pages can show it."""
    def work(report):
        if settings.get("disable_jellyfin_connection") == "1":
            return "Jellyfin integration is disabled in Settings."
        client = JellyfinClient()
        with db.connection() as conn:
            people = [(r["name"], r["jellyfin_user_id"]) for r in conn.execute("SELECT name, jellyfin_user_id FROM profile WHERE is_active = 1 AND jellyfin_user_id IS NOT NULL ORDER BY name")]
        if not people:
            return "No active profile has a Jellyfin user."
        seen, problems = 0, []
        for number, (name, user_id) in enumerate(people, 1):
            report(f"Reading what {name} has watched ({number} of {len(people)})...", force=True)
            try:
                with db.connection() as conn:
                    seen += userdata_ingest.sync_user(conn, client, user_id)["seen"]
            except Exception as exc:
                logger.warning(f"Could not read {name}'s watch history from Jellyfin: {exc}")
                problems.append(f"{name}: {exc}")
        if problems and len(problems) == len(people):
            raise RuntimeError("Could not read anyone's watch history. " + "; ".join(problems))
        with db.connection() as conn:
            watchlist.drop_watched(conn)                     # a title just finished leaves the lists of the people who asked for that
        return f"Watch history: {seen} item(s) for {len(people) - len(problems)} profile(s)." + (f" Failed: {'; '.join(problems)}." if problems else "")
    return run("watch_state", "Reading watch history", work)


WATCH_SETTLE_SECONDS = 2.0        # Jellyfin saves where someone stopped a moment around the time it announces they stopped
WATCH_EVENT_MIN_SECONDS = 20      # a user's data is saved on every progress report; one look at it per item in this long is enough
WATCH_PROGRESS_MIN_SECONDS = 90   # a webhook that only sends progress (no stop or saved-data events) still keeps the record fresh, at this pace
_WATCH_LAST = {}


def refresh_watch_item(jellyfin_user_id, item_id, settle=None):
    """Re-reads one item for one user (right after they stopped playing it)."""
    time.sleep(WATCH_SETTLE_SECONDS if settle is None else settle)
    try:
        client = JellyfinClient()
        with db.connection() as conn:
            userdata_ingest.refresh_item(conn, client, jellyfin_user_id, item_id)
    except ConfigError:
        return
    except Exception as exc:
        logger.warning(f"Could not refresh watch data for an item of Jellyfin user {jellyfin_user_id}: {exc}")


def watch_event(event, payload, now=None):
    """A Jellyfin webhook event. When someone with a profile stopped playing a movie or an episode (or their data for it changed), their
    record of it is read again, in the background. Returns True if a refresh was started."""
    if event not in ("PlaybackStop", "UserDataSaved", "PlaybackProgress"):
        return False
    from synchub import jellyfin_events
    fields = jellyfin_events.extract(payload)
    user, item = userdata_ingest.norm(fields.get("user_id")), userdata_ingest.norm(fields.get("item_id"))
    if not user or not item or (fields.get("item_type") or "").lower() not in ("movie", "episode"):
        return False
    with db.connection() as conn:
        known = conn.execute("SELECT 1 FROM profile WHERE is_active = 1 AND lower(replace(jellyfin_user_id, '-', '')) = ?", (user,)).fetchone()
    if not known:
        return False
    if event in ("UserDataSaved", "PlaybackProgress"):    # sent on every progress report: do not read it again each time
        gap = WATCH_EVENT_MIN_SECONDS if event == "UserDataSaved" else WATCH_PROGRESS_MIN_SECONDS
        now = time.monotonic() if now is None else now
        if now - _WATCH_LAST.get((user, item), -gap) < gap:
            return False
        _WATCH_LAST[(user, item)] = now
    return refresh_soon(f"watch:{user}:{item}", lambda: refresh_watch_item(user, item))


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
    if result.get("blocked"):
        parts.append(f"BLOCKED, {result['waiting']} waiting: {result['blocked']}")
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


# ---------------------------------------------------------------- change notifications
state_listeners = []      # callables taking a set of media IDs whose state or progress just changed (the web layer registers one)
MAX_ANNOUNCED = 100       # a bigger change (a first sync) is not worth announcing row by row


def notify_states_changed(media_ids):
    if not media_ids or len(media_ids) > MAX_ANNOUNCED:
        return
    for listener in list(state_listeners):
        try:
            listener(set(media_ids))
        except Exception as exc:
            logger.warning(f"A state-change listener failed: {exc}")


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
            stats = metadata.refresh_pending(conn, tmdb, progress=report)
        if tmdb:
            with db.connection() as conn:
                stats["ratings"] = ratings.fill_missing(conn, tmdb, progress=report)     # age ratings for titles that have none yet
        return stats

    def link_stage():
        with db.connection() as conn:
            return {"jellyfin_episodes": episodes.link_jellyfin_episodes(conn), "sonarr_episodes": episodes.map_sonarr_episodes(conn)}

    def watchlist_stage():
        with db.connection() as conn:
            return watchlist.refresh(conn)

    def state_stage():
        touched, changed = set(), set()
        with db.connection() as conn:
            stats = {"queue_events": states.sync_queue_events(conn, touched=touched), "states": states.compute(conn, changed=changed)}
            media_ids = states.media_ids_for(conn, touched | changed)
        notify_states_changed(media_ids)
        return stats

    result["matching"] = _stage("Matching titles to movies and shows", report, matching_stage)
    if tmdb is not None:
        result["metadata"] = _stage("Fetching TMDB details and episode lists", report, metadata_stage)
    linked = _stage("Linking episodes", report, link_stage)
    result.update(linked)
    result["watchlists"] = _stage("Updating watchlists", report, watchlist_stage)
    result.update(_stage("Working out statuses", report, state_stage))
    return result


# ---------------------------------------------------------------- frequent, cheap download-progress poll
QUEUE_IDLE_SECONDS = 300          # nothing downloading: look only this often (a webhook makes even that rare)
_LAST_POLL, _POLL_FAILURES, _UNLINKED = {}, {}, {}


def _queue_active(service):
    with db.connection() as conn:
        return conn.execute("SELECT 1 FROM src_arr_queue WHERE service = ? AND removed_at IS NULL LIMIT 1", (service,)).fetchone() is not None


def poll_queue(service, client=None, force=False):
    """One cheap look at a service's download queue: updates progress and states, tells dashboards, and starts a full
    sync when something finished or an unknown download appeared. Returns None when skipped.

    While nothing is downloading it only looks every QUEUE_IDLE_SECONDS (force=True looks now)."""
    lock = _LOCKS.get(service)
    if lock is not None and lock.locked():
        return None                                   # a full sync of this service is running and reads the same queue
    now = time.monotonic()
    if not force and not _queue_active(service) and now - _LAST_POLL.get(service, -QUEUE_IDLE_SECONDS) < QUEUE_IDLE_SECONDS:
        return None
    _LAST_POLL[service] = now
    ingest, client_class, sync = (radarr_ingest, RadarrClient, sync_radarr) if service == "radarr" else (sonarr_ingest, SonarrClient, sync_sonarr)
    try:
        client = client or client_class()
        touched, changed = set(), set()
        with db.connection() as conn:
            queue = ingest.sync_queue(conn, client)
            events = states.sync_queue_events(conn, touched=touched)
            states.compute(conn, only=touched, changed=changed)
            media_ids = states.media_ids_for(conn, touched | changed)
    except ConfigError:
        return None
    except Exception as exc:
        failures = _POLL_FAILURES[service] = _POLL_FAILURES.get(service, 0) + 1
        if failures == 1 or failures % 20 == 0:
            logger.warning(f"[queue] could not read the {service} queue ({failures} failure(s) in a row): {exc}")
        return None
    if _POLL_FAILURES.pop(service, 0):
        logger.info(f"[queue] the {service} queue can be read again")
    if events["opened"] or events["closed"]:
        logger.info(f"[queue] {service}: {queue['seen']} in queue, {events['opened']} started, {events['closed']} finished or removed")
    else:
        logger.debug(f"[queue] {service}: {queue['seen']} in queue, {len(media_ids)} title(s) changed")
    notify_states_changed(media_ids)
    # A download leaving the queue usually means a file arrived (the library must be read to know), and a download for
    # something we have not linked yet means the library is out of date.
    if events["closed"] or events["unlinked"] > _UNLINKED.get(service, 0):
        refresh_soon(service, with_reconcile(sync))
    _UNLINKED[service] = events["unlinked"]
    return {"service": service, "in_queue": queue["seen"], "events": events, "media": media_ids}


def poll_queues(force=False):
    """The scheduled job: Radarr then Sonarr. A service that is not configured, or has nothing downloading, costs nothing."""
    return {service: poll_queue(service, force=force) for service in ("radarr", "sonarr")}


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


def refresh_posters():
    """Retries every poster: forgets earlier failures, asks TMDB for the poster path of titles lacking one, then caches every missing image."""
    def work(report):
        cleared = posters.clear_failures()
        report(f"Cleared {cleared} remembered poster failure(s).", force=True)
        note = ""
        try:
            tmdb = TmdbClient()
        except ConfigError:
            tmdb, note = None, " TMDB is not configured, so titles without a poster path could only use Jellyfin."
        if tmdb:
            with db.connection() as conn:
                found = metadata.fill_missing_poster_paths(conn, tmdb, progress=report)
            note = f" TMDB poster paths: {found['found']} found, {found['none']} have none" + (f", {found['errors']} failed (see the log)" if found["errors"] else "") + "."
        with db.connection() as conn:
            stats = posters.refresh_all(conn, progress=report)
        if stats["none_titles"]:
            logger.info(f"[posters] no poster anywhere for (first {len(stats['none_titles'])}): {'; '.join(stats['none_titles'])}")
        return (f"Posters: {stats['downloaded']} downloaded, {stats['cached']} already cached, {stats['none']} not available from TMDB or Jellyfin.{note}")
    return run("posters", "Refreshing posters", work)


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
        steps = (sync_jellyfin_movies, sync_jellyfin_tv, sync_watch_state, scrape_letterboxd, scrape_serializd, resolve_letterboxd_ids, reconcile,
                 sync_radarr, sync_sonarr, reconcile)
        for number, step in enumerate(steps, 1):
            report(f"Step {number} of {len(steps)}: {step.__name__.replace('_', ' ')}", force=True)
            step()
        with db.connection() as conn:
            failed = [r["task_name"] for r in conn.execute("SELECT task_name FROM task_status WHERE status = 'failed' AND task_name <> 'all'")]
        return "All steps finished." + (f" Failed: {', '.join(failed)}." if failed else "")
    return run("all", "Running everything", work)


# ---------------------------------------------------------------- helpers for the web layer
TASK_NAMES = ("jellyfin_movies", "jellyfin_tv", "watch_state", "radarr", "sonarr", "scraper_letterboxd", "scraper_serializd", "id_resolver", "reconcile", "posters", "all")


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
