# config.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Run from a checkout, everything lives beside the code (data/, logs/, static/posters/). With SYNCHUB_DATA_DIR set (the Docker image sets it to /data)
# the database, the logs, the poster cache and the backups all live under that one folder, so one volume holds everything worth keeping.
_data_root = os.getenv("SYNCHUB_DATA_DIR")
DATA_DIR = Path(_data_root) if _data_root else BASE_DIR / "data"
LOGS_DIR = DATA_DIR / "logs" if _data_root else BASE_DIR / "logs"
POSTER_DIR = DATA_DIR / "posters" if _data_root else BASE_DIR / "static" / "posters"
BACKUP_DIR = DATA_DIR / "backups"

# Create required runtime directories on startup
for path in [DATA_DIR, LOGS_DIR, POSTER_DIR, BACKUP_DIR]:
    path.mkdir(parents=True, exist_ok=True)

LOG_FILE_PATH = LOGS_DIR / "app.log"

# Pauses between page loads and between users when scraping
DEFAULT_REQUEST_DELAY_RANGE = (2.5, 4.5)
DEFAULT_USER_DELAY_RANGE = (10.0, 20.0)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

HTTP_TIMEOUT = 10