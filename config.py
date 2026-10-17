import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
EXPORTS_DIR = BASE_DIR / "exports"
POSTER_DIR = BASE_DIR / "static" / "posters"

# Create required runtime directories on startup
for path in [DATA_DIR, LOGS_DIR, EXPORTS_DIR, POSTER_DIR]:
    path.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "letterboxd.db"
LOG_FILE_PATH = LOGS_DIR / "app.log"

# Application Default Configurations
DEFAULT_USERNAMES = ["tristanisepic"]
DEFAULT_LIST_TYPES = ["watchlist", "films"]

# Delay and Scraper Ranges
DEFAULT_REQUEST_DELAY_RANGE = (2.5, 4.5)
DEFAULT_USER_DELAY_RANGE = (10.0, 20.0)
DEFAULT_SCRAPE_INTERVAL_HOURS = 24

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

HTTP_TIMEOUT = 10