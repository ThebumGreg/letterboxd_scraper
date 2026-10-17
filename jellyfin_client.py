import re
from typing import Any, Dict, List, Optional
import requests
from database import get_setting
from logger import logger


def get_jellyfin_config():
    url = get_setting("jellyfin_url", "http://localhost:8096").rstrip("/")
    api_key = get_setting("jellyfin_api_key", "")
    return url, api_key


def clean_movie_title(title: str) -> str:
    """Removes year brackets, extra whitespace, and special characters for Jellyfin searching."""
    if not title:
        return ""
    # Strip year in parentheses, e.g., "Fight Club (1999)" -> "Fight Club"
    cleaned = re.sub(r"\s*\(\d{4}\)", "", title)
    # Strip quotes or brackets
    cleaned = re.sub(r'["\'\[\]]', "", cleaned)
    return cleaned.strip()


def fetch_jellyfin_catalog(item_type: str = "Movie") -> List[Dict[str, Any]]:
    """Fetch all catalog items from Jellyfin library to match against active films."""
    url, api_key = get_jellyfin_config()
    if not api_key:
        return []

    endpoint = f"{url}/Items"
    params = {
        "IncludeItemTypes": item_type,
        "Recursive": "true",
        "Fields": "Path,ProviderIds",
        "api_key": api_key,
    }

    try:
        res = requests.get(endpoint, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            items = data.get("Items", [])
            catalog = []
            for item in items:
                title = item.get("Name", "")
                item_id = item.get("Id", "")
                web_url = (
                    f"{url}/web/index.html#!/details?id={item_id}"
                    if item_id
                    else ""
                )
                catalog.append(
                    {
                        "id": item_id,
                        "title": title,
                        "normalized_title": title.lower().strip(),
                        "web_url": web_url,
                    }
                )
            return catalog
        else:
            logger.error(f"Jellyfin catalog fetch failed: HTTP {res.status_code}")
            return []
    except Exception as e:
        logger.error(f"Error connecting to Jellyfin catalog: {e}")
        return []


def fetch_jellyfin_poster_bytes(movie_title: str) -> Optional[bytes]:
    """Fetch primary poster binary data directly from Jellyfin for a given movie title."""
    url, api_key = get_jellyfin_config()
    if not api_key:
        logger.warning(
            f"Jellyfin poster fetch skipped for '{movie_title}': API key is missing in settings."
        )
        return None

    cleaned_title = clean_movie_title(movie_title)
    endpoint = f"{url}/Items"

    search_queries = [cleaned_title]
    if ":" in cleaned_title:
        search_queries.append(cleaned_title.split(":")[0].strip())

    for query in search_queries:
        params = {
            "searchTerm": query,
            "includeItemTypes": "Movie",
            "recursive": "true",
            "limit": 10,
            "api_key": api_key,
        }

        try:
            logger.info(
                f"Querying Jellyfin API for title: '{query}' (original: '{movie_title}')"
            )
            res = requests.get(endpoint, params=params, timeout=5)

            if res.status_code != 200:
                logger.error(
                    f"Jellyfin search returned HTTP {res.status_code} for query '{query}'"
                )
                continue

            items = res.json().get("Items", [])
            if not items:
                logger.warning(
                    f"Jellyfin returned 0 search results for query: '{query}'"
                )
                continue

            matched_item = None
            target_norm = cleaned_title.lower()

            for item in items:
                item_name = item.get("Name", "").lower()
                original_name = item.get("OriginalTitle", "").lower()
                if (
                    item_name == target_norm
                    or original_name == target_norm
                    or target_norm in item_name
                ):
                    matched_item = item
                    break

            if not matched_item:
                matched_item = items[0]

            item_id = matched_item.get("Id")
            item_name = matched_item.get("Name")
            logger.info(f"Jellyfin match found: '{item_name}' (ID: {item_id})")

            image_url = f"{url}/Items/{item_id}/Images/Primary"
            img_res = requests.get(
                image_url, params={"api_key": api_key}, timeout=5
            )

            if img_res.status_code == 200 and len(img_res.content) > 500:
                logger.info(
                    f"Successfully downloaded {len(img_res.content)} bytes from Jellyfin for '{movie_title}'"
                )
                return img_res.content
            else:
                logger.warning(
                    f"Jellyfin image endpoint returned HTTP {img_res.status_code} ({len(img_res.content)} bytes)"
                )

        except Exception as e:
            logger.error(
                f"Error fetching Jellyfin poster for query '{query}': {e}",
                exc_info=True,
            )

    return None