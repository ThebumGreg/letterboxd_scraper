import datetime
import requests
from database import get_setting, sync_scraped_records
from logger import logger

def scrape_serializd_watchlist(username: str) -> None:
    """Scrapes public show watchlists from Serializd API/web endpoints."""
    url = f"https://www.serializd.com/api/user/{username}/watchlist"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            shows = res.json().get("shows", [])
            run_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            records = []
            for show in shows:
                records.append({
                    "movie_slug": f"serializd-{show.get('id')}",
                    "movie_title": show.get("name"),
                    "movie_url": f"https://www.serializd.com/show/{show.get('id')}",
                    "full_display_name": show.get("name"),
                    "film_identifier": str(show.get("id")),
                    "media_type": "tv",
                    "tmdb_id": str(show.get("tmdbId")) if show.get("tmdbId") else None,
                    "imdb_id": show.get("imdbId"),
                    "season_number": show.get("season_number") or show.get("season"),
                    "episode_number": show.get("episode_number") or show.get("episode"),
                })
            sync_scraped_records(username, "watchlist_tv", records, run_ts)
    except Exception as e:
        logger.error(f"Serializd scraping failed for user {username}: {e}")