import re
from typing import Dict, Set
from curl_cffi import requests

from database import get_setting
from logger import logger

def normalize_title(title: str) -> str:
    title = re.sub(r"\s*\(\d{4}\)$", "", title)
    title = re.sub(r"[^\w\s]", "", title)
    return title.lower().strip()

def fetch_sonarr_library_titles() -> Set[str]:
    url = get_setting("sonarr_url", "http://localhost:8989").rstrip("/")
    api_key = get_setting("sonarr_api_key", "")

    if not api_key:
        return set()

    endpoint = f"{url}/api/v3/series"
    headers = {"X-Api-Key": api_key}

    try:
        response = requests.get(endpoint, headers=headers, timeout=10)
        if response.status_code == 200:
            series_list = response.json()
            owned_titles = set()
            for series in series_list:
                raw_title = series.get("title", "")
                owned_titles.add(normalize_title(raw_title))
                if series.get("year"):
                    owned_titles.add(normalize_title(f"{raw_title} {series.get('year')}"))
            return owned_titles
        return set()
    except Exception as e:
        logger.error(f"Failed to fetch Sonarr library: {e}")
        return set()

def add_series_to_sonarr(title: str) -> Dict[str, str]:
    url = get_setting("sonarr_url", "http://localhost:8989").rstrip("/")
    api_key = get_setting("sonarr_api_key", "")
    auto_push = get_setting("auto_sync_sonarr", "0") == "1"

    if not api_key:
        return {"status": "error", "message": "Sonarr API key missing in settings."}

    headers = {"X-Api-Key": api_key}

    try:
        search_url = f"{url}/api/v3/series/lookup?term={requests.utils.quote(title)}"
        search_res = requests.get(search_url, headers=headers, timeout=3)
        if search_res.status_code != 200 or not search_res.json():
            return {"status": "error", "message": f"Could not find TV show '{title}' in Sonarr lookup."}

        show_data = search_res.json()[0]
    except Exception as e:
        return {"status": "error", "message": f"Sonarr lookup failed: {e}"}

    # Safeguard: Test Mode vs Live Addition
    if not auto_push:
        logger.info(f"[TEST MODE] Blocked addition to Sonarr for series: '{show_data['title']}'")
        return {
            "status": "success",
            "message": f"[TEST MODE] Read-only verification successful for '{show_data['title']}'. (Enable Live Push in Settings to import)."
        }

    # Live Sonarr Import Execution
    payload = {
        "title": show_data["title"],
        "qualityProfileId": int(get_setting("sonarr_quality_profile_id", "1")),
        "titleSlug": show_data["titleSlug"],
        "tvdbId": show_data["tvdbId"],
        "year": show_data["year"],
        "rootFolderPath": get_setting("sonarr_root_folder", "/tv"),
        "monitored": True,
        "addOptions": {"searchForMissingEpisodes": True}
    }

    try:
        add_res = requests.post(f"{url}/api/v3/series", json=payload, headers=headers, timeout=3)
        if add_res.status_code in (200, 201):
            logger.info(f"Successfully added '{title}' to Sonarr.")
            return {"status": "success", "message": f"Added '{show_data['title']}' to Sonarr!"}
        return {"status": "error", "message": f"Sonarr error: HTTP {add_res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to post series to Sonarr: {e}"}