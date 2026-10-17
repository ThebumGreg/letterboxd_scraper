import json
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from curl_cffi import requests

from config import DEFAULT_HEADERS, HTTP_TIMEOUT
from database import get_setting, init_db, set_setting, sync_scraped_records
from logger import logger
from parser import parse_movie_posters, parse_next_page_url
from radarr_client import sync_missing_to_radarr

class LetterboxdScraper:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def fetch_page(self, url: str) -> Optional[str]:
        req_min = float(get_setting("request_delay_min", "2.5"))
        req_max = float(get_setting("request_delay_max", "4.5"))
        
        time.sleep(random.uniform(req_min, req_max))
        try:
            response = self.session.get(url, impersonate="chrome120", timeout=HTTP_TIMEOUT)
            return response.text if response.status_code == 200 else None
        except Exception as e:
            logger.error(f"Error requesting {url}: {e}")
            return None

    def scrape_user_list(self, username: str, list_type: str) -> List[Dict[str, str]]:
        records: List[Dict[str, str]] = []
        current_url = (
            f"https://letterboxd.com/{username}/watchlist/"
            if list_type == "watchlist"
            else f"https://letterboxd.com/{username}/films/"
        )

        logger.info(f"Scraping [{username}] {list_type}...")
        while current_url:
            html = self.fetch_page(current_url)
            if not html:
                break

            records.extend(parse_movie_posters(html, username))
            next_url = parse_next_page_url(html, current_url)
            if next_url == current_url:
                break
            current_url = next_url

        logger.info(f"Finished [{username}] {list_type}: extracted {len(records)} records.")
        return records

def run_scraper_job():
    if get_setting("is_scraping_active", "0") == "1":
        logger.warning("Scraper job requested, but another scrape is currently active. Skipping.")
        return

    set_setting("is_scraping_active", "1")
    logger.info("=== Starting Letterboxd Multi-User Daily Scrape Job ===")
    init_db()

    scraper = LetterboxdScraper()
    run_timestamp = datetime.now(timezone.utc).isoformat()

    try:
        target_usernames = json.loads(get_setting("target_usernames", '["tristanisepic"]'))
        target_list_types = json.loads(get_setting("target_list_types", '["watchlist", "films"]'))
        user_min = float(get_setting("user_delay_min", "10.0"))
        user_max = float(get_setting("user_delay_max", "20.0"))

        for u_idx, username in enumerate(target_usernames):
            if u_idx > 0:
                user_delay = random.uniform(user_min, user_max)
                logger.info(f"Batch Spacing: waiting {user_delay:.2f}s before scraping {username}...")
                time.sleep(user_delay)

            for list_type in target_list_types:
                try:
                    records = scraper.scrape_user_list(username, list_type)
                    upserted, tombstoned, active = sync_scraped_records(
                        username, list_type, records, run_timestamp
                    )
                    logger.info(
                        f"Sync [{username} - {list_type}]: Processed {upserted} | "
                        f"Tombstoned {tombstoned} | Active {active}"
                    )
                except Exception as e:
                    logger.error(f"Error scraping [{username}] {list_type}: {e}", exc_info=True)

        set_setting("last_run_timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # Automated Direct Radarr Sync Check
        if get_setting("auto_sync_radarr", "0") == "1":
            logger.info("Auto-Sync enabled: Pushing missing movies directly to Radarr API...")
            sync_missing_to_radarr()

        logger.info("=== Letterboxd Daily Scrape Job Finished ===")
    finally:
        set_setting("is_scraping_active", "0")

if __name__ == "__main__":
    run_scraper_job()