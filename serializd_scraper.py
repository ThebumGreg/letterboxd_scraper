# serializd_scraper.py
import json
import random
import time
import requests
from config import DEFAULT_HEADERS, DEFAULT_REQUEST_DELAY_RANGE, DEFAULT_USER_DELAY_RANGE, HTTP_TIMEOUT
from database import get_setting, sync_scraped_records
from logger import logger

def get_target_serializd_usernames():
    raw = get_setting("target_serializd_usernames", "") or ""
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
        names = [str(n).strip() for n in parsed] if isinstance(parsed, list) else []
    except ValueError:
        names = [n.strip() for n in raw.replace("\n", ",").split(",")]
    return list(dict.fromkeys(n for n in names if n))

def scrape_serializd_watchlist(username: str) -> None:
    if not username: return
    url = f"https://www.serializd.com/api/user/{username}/watchlist"
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT)
        if res.status_code == 200:
            shows = res.json().get("shows", [])
            records = []
            for show in shows:
                show_id = show.get("id")
                records.append({
                    "movie_slug": f"serializd-{show_id}",
                    "film_title": show.get("name"),
                    "full_display_name": show.get("name"),
                    "film_identifier": str(show_id),
                    "media_type": "tv",
                    "tvdb_id": str(show.get("tvdbId")) if show.get("tvdbId") else None,
                    "tmdb_id": str(show.get("tmdbId")) if show.get("tmdbId") else None,
                    "imdb_id": show.get("imdbId"),
                    "season_number": show.get("season_number") or show.get("season"),
                    "episode_number": show.get("episode_number") or show.get("episode"),
                })
            sync_scraped_records(username, "Watchlist", records)
            logger.info(f"Serializd: Scraped {len(records)} TV show(s) for user {username}")
        else:
            logger.warning(f"Serializd request failed for {username}: HTTP {res.status_code}")
    except Exception as e:
        logger.error(f"Serializd scraping error for {username}: {e}")