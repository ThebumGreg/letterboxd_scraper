"""Background jobs, scheduled from the interval settings (0 hours switches a job off)."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from synchub import ddns, settings, tasks

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def interval_hours(key, default):
    """A missing, non-numeric or negative stored value falls back to the default instead of breaking startup."""
    try:
        hours = int(float(settings.get(key, str(default))))
    except (TypeError, ValueError):
        logger.warning(f"Invalid value for {key}; using {default}.")
        return default
    return hours if hours >= 0 else default


def queue_poll_seconds():
    """How often download progress is checked (0 = never). Anything between 1 and 9 is raised to 10 so a typo cannot hammer the services."""
    seconds = interval_hours("queue_poll_seconds", 30)
    return 0 if seconds == 0 else max(10, seconds)


REAPER_SECONDS = 30


def _reap_jobs():
    """Takes back jobs whose worker has gone silent."""
    from synchub import db, jobs
    with db.connection() as conn:
        jobs.reap(conn)


def _prune_jellyfin_events():
    from synchub import jellyfin_events
    removed = jellyfin_events.prune()
    if removed:
        logger.info(f"Removed {removed} Jellyfin event(s) older than the retention setting.")


def _scrape_job():
    for step in (tasks.scrape_letterboxd, tasks.scrape_serializd, tasks.resolve_letterboxd_ids, tasks.reconcile):
        step()


JOBS = (
    ("job_scrape", "scraper_interval_hours", 24, _scrape_job),
    ("job_jellyfin", "jellyfin_interval_hours", 6, tasks.with_reconcile(tasks.sync_jellyfin_movies, tasks.sync_jellyfin_tv)),
    ("job_radarr", "radarr_interval_hours", 6, tasks.with_reconcile(tasks.sync_radarr)),
    ("job_sonarr", "sonarr_interval_hours", 6, tasks.with_reconcile(tasks.sync_sonarr)),
)


def configure():
    scheduler.remove_all_jobs()
    scheduler.add_job(ddns.run_update, "interval", minutes=ddns.interval_minutes(), id="job_ddns", max_instances=1, coalesce=True)
    for job_id, key, default, fn in JOBS:
        hours = interval_hours(key, default)
        if hours > 0:
            scheduler.add_job(fn, "interval", hours=hours, id=job_id, max_instances=1, coalesce=True)
    scheduler.add_job(_reap_jobs, "interval", seconds=REAPER_SECONDS, id="job_reaper", max_instances=1, coalesce=True)
    scheduler.add_job(_prune_jellyfin_events, "interval", hours=24, id="job_prune_jellyfin_events", max_instances=1, coalesce=True)
    seconds = queue_poll_seconds()
    if seconds > 0:
        scheduler.add_job(tasks.poll_queues, "interval", seconds=seconds, id="job_queue_poll", max_instances=1, coalesce=True)


def start():
    configure()
    if not scheduler.running:
        scheduler.start()
