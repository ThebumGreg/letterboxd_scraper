from datetime import datetime, timezone
from config import TARGET_LIST_TYPES, TARGET_USERNAMES
from database import init_db, sync_scraped_records
from logger import logger
from scraper import LetterboxdScraper

def run():
    logger.info("=== Starting Letterboxd Daily Scrape Job ===")
    init_db()
    
    scraper = LetterboxdScraper()
    run_timestamp = datetime.now(timezone.utc).isoformat()

    for username in TARGET_USERNAMES:
        for list_type in TARGET_LIST_TYPES:
            try:
                records = scraper.scrape_user_list(username, list_type)
                upserted, tombstoned, active = sync_scraped_records(
                    username, list_type, records, run_timestamp
                )
                logger.info(
                    f"Sync Summary [{username} - {list_type}]: "
                    f"Processed {upserted} records | Tombstoned (Removed): {tombstoned} | Current Total Active: {active}"
                )
            except Exception as e:
                logger.error(f"Unhandled error scraping [{username}] {list_type}: {e}", exc_info=True)

    logger.info("=== Letterboxd Daily Scrape Job Finished ===")

if __name__ == "__main__":
    run()