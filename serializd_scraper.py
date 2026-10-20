# serializd_scraper.py
import requests
from config import DEFAULT_HEADERS, HTTP_TIMEOUT
from database import sync_scraped_records, get_active_profiles
from logger import logger

def scrape_serializd_watchlist(profile: dict) -> None:
    username = profile.get("serializd_username")
    profile_id = profile.get("id")
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
            sync_scraped_records(username, profile_id, "Watchlist", records)
            logger.info(f"Serializd: Scraped {len(records)} TV show(s) for user {username}")
        elif res.status_code in [403, 429]:
            logger.warning(f"Serializd scraper aborted for {username}: HTTP {res.status_code} (Rate limit/Blocked)")
        else:
            logger.warning(f"Serializd request failed for {username}: HTTP {res.status_code}")
    except Exception as e:
        logger.error(f"Serializd scraping error for {username}: {e}")

def run_serializd_scraper_task():
    active_profiles = get_active_profiles()
    targets = [p for p in active_profiles if p.get('serializd_username')]
    for profile in targets:
        scrape_serializd_watchlist(profile)

if __name__ == "__main__":
    run_serializd_scraper_task()