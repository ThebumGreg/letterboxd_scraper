# scheduler.py
import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from database import get_setting, update_task_status, reconcile_master_catalog
from ddns_updater import run_ddns_update

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()

def run_letterboxd_scraper_task():
    update_task_status("scraper_letterboxd", "in_progress", "Scraping Letterboxd watchlists...")
    try:
        from letterboxd_scraper import scrape_letterboxd_page
        scrape_letterboxd_page()
        update_task_status("scraper_letterboxd", "completed", "Letterboxd scrape finished successfully.")
    except Exception as e:
        logger.error(f"Letterboxd scraper job failed: {e}")
        update_task_status("scraper_letterboxd", "failed", str(e))
    finally:
        reconcile_master_catalog()

def run_serializd_scraper_task():
    update_task_status("scraper_serializd", "in_progress", "Scraping Serializd watchlists...")
    try:
        from serializd_scraper import scrape_serializd_watchlist
        scrape_serializd_watchlist()
        update_task_status("scraper_serializd", "completed", "Serializd scrape finished successfully.")
    except Exception as e:
        logger.error(f"Serializd scraper job failed: {e}")
        update_task_status("scraper_serializd", "failed", str(e))
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

def run_jellyfin_movies_task():
    update_task_status("jellyfin_movies", "in_progress", "Syncing Jellyfin movies...")
    try:
        from jellyfin_integration import sync_jellyfin_library
        sync_jellyfin_library(media_type="movie")
        update_task_status("jellyfin_movies", "completed", "Jellyfin movies sync finished.")
    except Exception as e:
        logger.error(f"Jellyfin movies job failed: {e}")
        update_task_status("jellyfin_movies", "failed", str(e))

def run_jellyfin_tv_task():
    update_task_status("jellyfin_tv", "in_progress", "Syncing Jellyfin TV shows...")
    try:
        from jellyfin_integration import sync_jellyfin_library
        sync_jellyfin_library(media_type="tv")
        update_task_status("jellyfin_tv", "completed", "Jellyfin TV sync finished.")
    except Exception as e:
        logger.error(f"Jellyfin TV job failed: {e}")
        update_task_status("jellyfin_tv", "failed", str(e))

def run_jellyfin_task():
    run_jellyfin_movies_task()
    run_jellyfin_tv_task()

job_sync_jellyfin = run_jellyfin_task

def run_all_sequential_pipeline():
    update_task_status("all", "in_progress", "Sequential pipeline started.")
    update_task_status("scraper_letterboxd", "pending", "Waiting in queue...")
    update_task_status("scraper_serializd", "pending", "Waiting in queue...")
    update_task_status("radarr", "pending", "Waiting in queue...")
    update_task_status("sonarr", "pending", "Waiting in queue...")
    update_task_status("jellyfin_movies", "pending", "Waiting in queue...")
    update_task_status("jellyfin_tv", "pending", "Waiting in queue...")
    
    run_jellyfin_movies_task()
    run_jellyfin_tv_task()
    run_radarr_task()
    run_sonarr_task()
    run_letterboxd_scraper_task()
    run_serializd_scraper_task()
    
    update_task_status("all", "completed", "All operations completed sequentially.")

def start_async_task(task_fn):
    thread = threading.Thread(target=task_fn)
    thread.daemon = True
    thread.start()

trigger_async_task = start_async_task

def reconfigure_scheduler():
    scheduler.remove_all_jobs()
    
    # DDNS Job runs constantly regardless of user settings
    scheduler.add_job(run_ddns_update, 'interval', minutes=5, id='job_ddns_updater')
    
    scraper_hours = int(get_setting("scraper_interval_hours", "24"))
    radarr_hours = int(get_setting("radarr_interval_hours", "6"))
    sonarr_hours = int(get_setting("sonarr_interval_hours", "6"))
    jellyfin_hours = int(get_setting("jellyfin_interval_hours", "6"))
    
    if scraper_hours > 0:
        scheduler.add_job(run_letterboxd_scraper_task, 'interval', hours=scraper_hours, id='job_lb_scraper')
        scheduler.add_job(run_serializd_scraper_task, 'interval', hours=scraper_hours, id='job_sz_scraper')
        
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