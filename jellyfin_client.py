import re
from typing import Dict, List, Set
from curl_cffi import requests

from database import get_setting
from logger import logger

def normalize_title(title: str) -> str:
    title = re.sub(r"\s*\(\d{4}\)$", "", title)
    title = re.sub(r"[^\w\s]", "", title)
    return title.lower().strip()

def fetch_jellyfin_catalog(item_type: str = "Movie") -> List[Dict]:
    url = get_setting("jellyfin_url", "http://localhost:8096").rstrip("/")
    api_key = get_setting("jellyfin_api_key", "")

    if not api_key:
        return []

    endpoint = f"{url}/Items"
    headers = {"X-Emby-Token": api_key}
    params = {
        "IncludeItemTypes": item_type,
        "Recursive": "true",
        "Fields": "Name,ProductionYear,Path,Overview,ProviderIds"
    }

    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=3)
        if response.status_code == 200:
            items = response.json().get("Items", [])
            catalog = []
            for item in items:
                catalog.append({
                    "id": item.get("Id"),
                    "title": item.get("Name"),
                    "year": item.get("ProductionYear"),
                    "path": item.get("Path"),
                    "overview": item.get("Overview", ""),
                    "normalized_title": normalize_title(item.get("Name", "")),
                    "web_url": f"{url}/web/index.html#!/details?id={item.get('Id')}"
                })
            return catalog
        return []
    except Exception as e:
        logger.error(f"Error connecting to Jellyfin API: {e}")
        return []

def fetch_jellyfin_library_titles(item_type: str = "Movie") -> Set[str]:
    catalog = fetch_jellyfin_catalog(item_type=item_type)
    owned = set()
    for item in catalog:
        owned.add(item["normalized_title"])
        if item["year"]:
            owned.add(normalize_title(f"{item['title']} {item['year']}"))
    return owned