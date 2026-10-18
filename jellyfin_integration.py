import os
import requests
from typing import Dict, List, Optional, Tuple
from database import get_setting, update_external_library_cache
from logger import logger

MIN_POSTER_SIZE_BYTES = 5120


def get_jellyfin_headers() -> Dict[str, str]:
    return {
        "X-Emby-Token": get_setting("jellyfin_api_key"),
        "Accept": "application/json",
    }


def fetch_jellyfin_library() -> List[Dict]:
    """Retrieves all Movies and TV Series from Jellyfin into external_library_cache."""
    if get_setting("disable_jellyfin_connection", "0") == "1":
        logger.info("Jellyfin integration is disabled in settings.")
        return []

    url = get_setting("jellyfin_url").rstrip("/")
    api_key = get_setting("jellyfin_api_key")

    if not url or not api_key:
        return []

    try:
        endpoint = f"{url}/Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Series",
            "Fields": "ProviderIds,Path,Overview",
        }
        resp = requests.get(endpoint, headers=get_jellyfin_headers(), params=params, timeout=15)
        if resp.status_code == 200:
            raw_items = resp.json().get("Items", [])
            parsed_items = []
            for item in raw_items:
                provider_ids = item.get("ProviderIds", {})
                item_info = {
                    "item_identifier": item.get("Id"),
                    "title": item.get("Name"),
                    "normalized_title": item.get("Name", "").lower().strip(),
                    "media_type": "movie" if item.get("Type") == "Movie" else "tv",
                    "tmdb_id": provider_ids.get("Tmdb"),
                    "imdb_id": provider_ids.get("Imdb"),
                    "tvdb_id": provider_ids.get("Tvdb"),
                    "external_item_id": item.get("Id"),
                    "details": item,
                }
                parsed_items.append(item_info)

            update_external_library_cache("jellyfin", parsed_items)
            return parsed_items
    except Exception as e:
        logger.error(f"Failed to fetch complete Jellyfin library: {e}")

    return []


def get_jellyfin_item_link(item_id: str, user_agent: str = "") -> Dict[str, str]:
    base_url = get_setting("jellyfin_url").rstrip("/")
    web_url = f"{base_url}/#/details?id={item_id}"
    ios_deeplink = f"jellyfin://details?id={item_id}"
    is_ios = any(device in user_agent for device in ["iPhone", "iPad", "iPod"])

    return {
        "web_url": web_url,
        "ios_deeplink": ios_deeplink,
        "primary_url": ios_deeplink if is_ios else web_url,
        "is_ios": is_ios,
    }


def fetch_tmdb_poster(tmdb_id: Optional[str], save_path: str) -> bool:
    tmdb_key = get_setting("tmdb_api_key")
    if not tmdb_key or not tmdb_id:
        return False

    try:
        url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_key}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            poster_path = res.json().get("poster_path")
            if poster_path:
                img_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
                img_res = requests.get(img_url, timeout=10)
                if img_res.status_code == 200 and len(img_res.content) > MIN_POSTER_SIZE_BYTES:
                    with open(save_path, "wb") as f:
                        f.write(img_res.content)
                    return True
    except Exception as e:
        logger.error(f"TMDB poster fetch failed for TMDB {tmdb_id}: {e}")

    return False


def fetch_and_cache_poster(film_slug: str, jellyfin_id: Optional[str], tmdb_id: Optional[str]) -> Optional[str]:
    os.makedirs("static/posters", exist_ok=True)
    target_file = f"static/posters/{film_slug}.jpg"

    if os.path.exists(target_file):
        if os.path.getsize(target_file) > MIN_POSTER_SIZE_BYTES:
            return f"/static/posters/{film_slug}.jpg"
        os.remove(target_file)

    if jellyfin_id and get_setting("disable_jellyfin_connection", "0") != "1":
        jf_base = get_setting("jellyfin_url").rstrip("/")
        img_url = f"{jf_base}/Items/{jellyfin_id}/Images/Primary"
        try:
            res = requests.get(img_url, headers=get_jellyfin_headers(), timeout=10)
            if res.status_code == 200 and len(res.content) > MIN_POSTER_SIZE_BYTES:
                with open(target_file, "wb") as f:
                    f.write(res.content)
                return f"/static/posters/{film_slug}.jpg"
        except Exception as e:
            logger.warning(f"Failed to fetch Jellyfin poster for {jellyfin_id}: {e}")

    if fetch_tmdb_poster(tmdb_id, target_file):
        return f"/static/posters/{film_slug}.jpg"

    return None


def sync_jellyfin_playlist(playlist_name: str, item_ids: List[str]) -> Tuple[bool, str]:
    if get_setting("disable_jellyfin_connection", "0") == "1":
        return False, "Jellyfin connection is disabled in settings."

    jf_base = get_setting("jellyfin_url").rstrip("/")
    if not jf_base or not get_setting("jellyfin_api_key"):
        return False, "Jellyfin API URL or Key not configured."

    headers = get_jellyfin_headers()
    try:
        search_res = requests.get(
            f"{jf_base}/Items",
            headers=headers,
            params={"IncludeItemTypes": "Playlist", "SearchTerm": playlist_name},
            timeout=10,
        )
        existing_id = None
        if search_res.status_code == 200:
            for item in search_res.json().get("Items", []):
                if item.get("Name") == playlist_name:
                    existing_id = item.get("Id")
                    break

        if existing_id:
            requests.delete(f"{jf_base}/Playlists/{existing_id}/Items", headers=headers, timeout=10)
            if item_ids:
                requests.post(
                    f"{jf_base}/Playlists/{existing_id}/Items",
                    headers=headers,
                    params={"ids": ",".join(item_ids)},
                    timeout=10,
                )
            return True, f"Updated existing playlist '{playlist_name}' ({len(item_ids)} items)."
        else:
            payload = {"Name": playlist_name, "Ids": item_ids}
            res = requests.post(f"{jf_base}/Playlists", headers=headers, json=payload, timeout=10)
            if res.status_code in [200, 204]:
                return True, f"Created new playlist '{playlist_name}' with {len(item_ids)} items."
            return False, f"Failed to create playlist. Status: {res.status_code}"
    except Exception as e:
        logger.error(f"Jellyfin playlist sync error: {e}")
        return False, str(e)