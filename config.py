from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Directory Setup
LOGS_DIR = BASE_DIR / "logs"
EXPORTS_DIR = BASE_DIR / "exports"
LOGS_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)

# File Paths
DB_PATH = BASE_DIR / "letterboxd_data.db"
LOG_FILE_PATH = LOGS_DIR / "scraper.log"

# Default Fallback Configurations
DEFAULT_USERNAMES = ["tristanisepic"]
DEFAULT_LIST_TYPES = ["watchlist", "films"]
DEFAULT_REQUEST_DELAY_RANGE = (2.5, 4.5)
DEFAULT_USER_DELAY_RANGE = (10.0, 20.0)
DEFAULT_SCRAPE_INTERVAL_HOURS = 24
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