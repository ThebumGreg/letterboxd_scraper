import re
from typing import Dict, Set
from curl_cffi import requests

from database import get_setting
from logger import logger


def normalize_title(title: str) -> str:
    title = re.sub(r"\s*\(\d{4}\)$", "", title)
    title = re.sub(r"[^\w\s]", "", title)
    return title.lower().strip()

def fetch_radarr_library_titles() -> Set[str]:
    url = get_setting("radarr_url", "http://localhost:7878").rstrip("/")
    api_key = get_setting("radarr_api_key", "")

    if not api_key:
        return set()

    endpoint = f"{url}/api/v3/movie"
    headers = {"X-Api-Key": api_key}

    try:
        response = requests.get(endpoint, headers=headers, timeout=3)
        if response.status_code == 200:
            movies = response.json()
            owned_titles = set()
            for movie in movies:
                raw_title = movie.get("title", "")
                owned_titles.add(normalize_title(raw_title))
                if movie.get("year"):
                    owned_titles.add(normalize_title(f"{raw_title} {movie.get('year')}"))
            return owned_titles
        return set()
    except Exception as e:
        logger.error(f"Failed to fetch Radarr library: {e}")
        return set()

def add_movie_to_radarr(title: str) -> Dict[str, str]:
    url = get_setting("radarr_url", "http://localhost:7878").rstrip("/")
    api_key = get_setting("radarr_api_key", "")
    auto_push = get_setting("auto_sync_radarr", "0") == "1"

    if not api_key:
        return {"status": "error", "message": "Radarr API key missing in settings."}

    headers = {"X-Api-Key": api_key}

    try:
        search_url = f"{url}/api/v3/movie/lookup?term={requests.utils.quote(title)}"
        search_res = requests.get(search_url, headers=headers, timeout=10)
        if search_res.status_code != 200 or not search_res.json():
            return {"status": "error", "message": f"Could not find movie '{title}' in Radarr lookup."}

        movie_data = search_res.json()[0]
    except Exception as e:
        return {"status": "error", "message": f"Radarr lookup failed: {e}"}

    # Safeguard: Test Mode vs Live Addition
    if not auto_push:
        logger.info(f"[TEST MODE] Blocked auto-add to Radarr for movie: '{movie_data['title']}'")
        return {
            "status": "success",
            "message": f"[TEST MODE] Read-only verification successful for '{movie_data['title']}'. (Enable Live Push in Settings to import)."
        }

    # Live Radarr Import Execution
    payload = {
        "title": movie_data["title"],
        "qualityProfileId": int(get_setting("radarr_quality_profile_id", "1")),
        "titleSlug": movie_data["titleSlug"],
        "images": movie_data.get("images", []),
        "tmdbId": movie_data["tmdbId"],
        "year": movie_data["year"],
        "rootFolderPath": get_setting("radarr_root_folder", "/movies"),
        "monitored": True,
        "addOptions": {"searchForMovie": True}
    }

    try:
        add_res = requests.post(f"{url}/api/v3/movie", json=payload, headers=headers, timeout=10)
        if add_res.status_code in (200, 201):
            logger.info(f"Successfully added '{title}' to Radarr.")
            return {"status": "success", "message": f"Added '{movie_data['title']}' to Radarr!"}
        return {"status": "error", "message": f"Radarr error: HTTP {add_res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to post movie to Radarr: {e}"}

def sync_missing_to_radarr():
    logger.info("Executing Radarr sync check...")