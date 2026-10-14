import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Directory Setup for Logs and Exports
LOGS_DIR = BASE_DIR / "logs"
EXPORTS_DIR = BASE_DIR / "exports"

LOGS_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)

# Usernames to scrape
TARGET_USERNAMES = [
    "gsocal",
]

TARGET_LIST_TYPES = ["watchlist", "films"]

# Database & File Paths
DB_PATH = BASE_DIR / "letterboxd_data.db"
LOG_FILE_PATH = LOGS_DIR / "scraper.log"

REQUEST_DELAY_RANGE = (2.5, 4.5)
HTTP_TIMEOUT = 15

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://letterboxd.com/",
}