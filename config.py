import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Usernames to scrape
TARGET_USERNAMES = [
    "tristanisepic",
    # Add additional usernames here
]

# Supported page types per user
TARGET_LIST_TYPES = ["watchlist", "films"]

# Database path
DB_PATH = BASE_DIR / "letterboxd_data.db"

# Scraper timing & requests
REQUEST_DELAY_RANGE = (1.5, 3.0)  # Random sleep interval between requests (seconds)
HTTP_TIMEOUT = 15  # Request timeout (seconds)
MAX_RETRIES = 3

# HTTP Request Headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://letterboxd.com/",
}