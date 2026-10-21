# scheduler.py
import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from database import get_setting, update_task_status, reconcile_master_catalog
from ddns_updater import run_ddns_update

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()
_PIPELINE_LOCK = threading.Lock()

def run_letterboxd_scraper_task():
    update_task_status("scraper_letterboxd", "in_progress", "Scraping Letterboxd watchlists...")
    try:
        from letterboxd_scraper import run_all_scrapers
        run_all_scrapers()
        update_task_status("scraper_letterboxd", "completed", "Letterboxd scrape finished.")
    except Exception as e:
        logger.error(f"Letterboxd scraper job failed: {e}")
        update_task_status("scraper_letterboxd", "failed", str(e))
    finally:
        reconcile_master_catalog()

def run_serializd_scraper_task():
    update_task_status("scraper_serializd", "in_progress", "Scraping Serializd watchlists...")
    try:
        from serializd_scraper import run_serializd_scraper_task as scrape_all_serializd
        scrape_all_serializd()
        update_task_status("scraper_serializd", "completed", "Serializd scrape finished.")
    except Exception as e:
        logger.error(f"Serializd scraper job failed: {e}")
        update_task_status("scraper_serializd", "failed", str(e))
    finally:
        reconcile_master_catalog()

def run_id_resolver_task():
    try:
        from id_resolver import resolve_missing_ids
        resolve_missing_ids()
    except Exception as e:
        logger.error(f"ID resolver job failed: {e}")
        update_task_status("id_resolver", "failed", str(e))
    finally:
        reconcile_master_catalog()

def run_radarr_task():
    update_task_status("radarr", "in_progress", "Syncing with Radarr...")
    try:
        from automation import run_radarr_automation_worker
        run_radarr_automation_worker()
        update_task_status("radarr", "completed", "Radarr sync finished.")
    except Exception as e:
        logger.error(f"Radarr job failed: {e}")
        update_task_status("radarr", "failed", str(e))

def run_sonarr_task():
    update_task_status("sonarr", "in_progress", "Syncing with Sonarr...")
    try:
        from automation import run_sonarr_automation_worker
        run_sonarr_automation_worker()
        update_task_status("sonarr", "completed", "Sonarr sync finished.")
    except Exception as e:
        logger.error(f"Sonarr job failed: {e}")
        update_task_status("sonarr", "failed", str(e))

def _run_jellyfin_sync(task_name, media_type, label):
    update_task_status(task_name, "in_progress", f"Syncing Jellyfin {label}...")
    try:
        from jellyfin_integration import sync_jellyfin_library
        if sync_jellyfin_library(media_type=media_type) is False:
            update_task_status(task_name, "failed", f"Jellyfin {label} sync failed or Jellyfin is not configured. Check the logs.")
        else:
            update_task_status(task_name, "completed", f"Jellyfin {label} sync finished.")
    except Exception as e:
        logger.error(f"Jellyfin {label} job failed: {e}")
        update_task_status(task_name, "failed", str(e))

def run_jellyfin_movies_task():
    _run_jellyfin_sync("jellyfin_movies", "movie", "movies")

def run_jellyfin_tv_task():
    _run_jellyfin_sync("jellyfin_tv", "tv", "TV shows")

def run_disk_scan_task():
    update_task_status("disk_scanner", "in_progress", "Scanning local storage...")
    try:
        from disk_scanner import scan_local_disk_directory
        scan_local_disk_directory()
        update_task_status("disk_scanner", "completed", "Disk scan finished successfully.")
    except Exception as e:
        logger.error(f"Disk scan job failed: {e}")
        update_task_status("disk_scanner", "failed", str(e))

def run_all_sequential_pipeline():
    if not _PIPELINE_LOCK.acquire(blocking=False):
        logger.info("Sequential pipeline already active; skipping execution.")
        return

    try:
        update_task_status("all", "in_progress", "Sequential pipeline started.")
        update_task_status("jellyfin_movies", "pending", "Queued...")
        update_task_status("jellyfin_tv", "pending", "Queued...")
        update_task_status("scraper_letterboxd", "pending", "Queued...")
        update_task_status("scraper_serializd", "pending", "Queued...")
        update_task_status("id_resolver", "pending", "Queued...")
        update_task_status("radarr", "pending", "Queued...")
        update_task_status("sonarr", "pending", "Queued...")
        
        run_jellyfin_movies_task()
        run_jellyfin_tv_task()
        run_letterboxd_scraper_task()
        run_serializd_scraper_task()
        run_id_resolver_task()
        run_radarr_task()
        run_sonarr_task()
        
        update_task_status("all", "completed", "All pipeline operations completed sequentially.")
    finally:
        _PIPELINE_LOCK.release()

def start_async_task(task_fn):
    thread = threading.Thread(target=task_fn)
    thread.daemon = True
    thread.start()

def _interval_hours(key, default):
    """Reads an interval setting; a missing, non-numeric or negative stored value falls back to the default."""
    try:
        hours = int(float(get_setting(key, str(default))))
    except (TypeError, ValueError):
        logger.warning(f"Invalid value for {key}; using {default}h.")
        return default
    if hours < 0:
        logger.warning(f"Negative value for {key}; using {default}h.")
        return default
    return hours

def reconfigure_scheduler():
    scheduler.remove_all_jobs()
    scheduler.add_job(run_ddns_update, 'interval', minutes=5, id='job_ddns_updater')
    
    scraper_hours = _interval_hours("scraper_interval_hours", 24)
    radarr_hours = _interval_hours("radarr_interval_hours", 6)
    sonarr_hours = _interval_hours("sonarr_interval_hours", 6)
    jellyfin_hours = _interval_hours("jellyfin_interval_hours", 6)
    
    if scraper_hours > 0:
        scheduler.add_job(run_letterboxd_scraper_task, 'interval', hours=scraper_hours, id='job_lb_scraper')
        scheduler.add_job(run_serializd_scraper_task, 'interval', hours=scraper_hours, id='job_sz_scraper')
        scheduler.add_job(run_id_resolver_task, 'interval', hours=scraper_hours, id='job_id_resolver')
        
    if radarr_hours > 0:
        scheduler.add_job(run_radarr_task, 'interval', hours=radarr_hours, id='job_radarr')
        
    if sonarr_hours > 0:
        scheduler.add_job(run_sonarr_task, 'interval', hours=sonarr_hours, id='job_sonarr')
        
    if jellyfin_hours > 0:
        scheduler.add_job(run_jellyfin_movies_task, 'interval', hours=jellyfin_hours, id='job_jf_movies')
        scheduler.add_job(run_jellyfin_tv_task, 'interval', hours=jellyfin_hours, id='job_jf_tv')

def init_scheduler():
    reconfigure_scheduler()
    if not scheduler.running:
        scheduler.start()