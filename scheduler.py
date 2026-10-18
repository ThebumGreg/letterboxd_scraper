import os
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from automation import run_radarr_automation_worker, run_sonarr_automation_worker
from database import get_setting, update_job_status
from jellyfin_integration import fetch_jellyfin_library
from logger import logger

scheduler = BackgroundScheduler()


def job_sync_jellyfin() -> None:
    update_job_status("jellyfin_sync", "running", "Fetching Jellyfin library items...")
    try:
        items = fetch_jellyfin_library()
        update_job_status(
            "jellyfin_sync",
            "success",
            f"Cached {len(items)} items from Jellyfin.",
        )
    except Exception as e:
        logger.error(f"Background Jellyfin sync failed: {e}")
        update_job_status("jellyfin_sync", "failed", str(e))


def job_full_pipeline() -> None:
    """Executes background pipeline: Jellyfin library refresh -> Radarr -> Sonarr."""
    job_sync_jellyfin()
    run_radarr_automation_worker()
    run_sonarr_automation_worker()


def trigger_async_task(target_func, *args) -> None:
    thread = threading.Thread(target=target_func, args=args)
    thread.daemon = True
    thread.start()


def init_scheduler() -> None:
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not scheduler.running:
        scrape_hours = int(get_setting("scrape_interval_hours", "6"))
        scheduler.add_job(
            job_full_pipeline,
            "interval",
            hours=scrape_hours,
            id="full_pipeline_job",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("APScheduler background engine started.")