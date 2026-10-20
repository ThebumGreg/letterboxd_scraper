# jellyfin_integration.py
import logging
import os
import requests
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from config import POSTER_DIR, HTTP_TIMEOUT
from database import get_setting, update_service_cache, prune_service_cache, reconcile_master_catalog, logged_request

logger = logging.getLogger(__name__)

def _get_auth_headers(jf_key):
    """Returns standardized Jellyfin Authorization headers."""
    return {
        "Accept": "application/json",
        "Authorization": f'MediaBrowser Client="PythonApp", Device="Script", DeviceId="python-script-01", Version="1.0.0", Token="{jf_key}"',
    }

def get_jellyfin_item_link(item_id):
    jf_url = get_setting("jellyfin_url", "").rstrip("/")
    return {
        "primary_url": f"{jf_url}/web/index.html#/details?id={item_id}" if jf_url else "#",
        "app_link": f"jellyfin://details?id={item_id}"
    }

def get_jellyfin_users():
    if get_setting("disable_jellyfin_connection", "0") == "1":
        return []

    jf_url = get_setting("jellyfin_url", "").rstrip("/")
    jf_key = get_setting("jellyfin_api_key", "")
    if not jf_url or not jf_key:
        return []
    
    try:
        res = logged_request(
            "jellyfin", "GetUsers", "GET", 
            f"{jf_url}/Users", 
            headers=_get_auth_headers(jf_key), 
            timeout=10
        )
        if res.status_code == 200:
            return [{"id": u.get("Id"), "name": u.get("Name")} for u in res.json()]
    except Exception as e:
        logger.error(f"Error fetching Jellyfin users: {e}")
    return []

def fetch_and_cache_poster(slug, jellyfin_id=None, tmdb_id=None, media_type="movie"):
    target_path = POSTER_DIR / f"{slug}.jpg"
    neg_cache_path = POSTER_DIR / f"{slug}.missing"

    if target_path.exists() and target_path.stat().st_size > 3000:
        return True
    if neg_cache_path.exists():
        return False

    jf_url, jf_key = get_setting("jellyfin_url", "").rstrip("/"), get_setting("jellyfin_api_key", "")
    if not (get_setting("disable_jellyfin_connection", "0") == "1") and jellyfin_id and jf_url and jf_key:
        try:
            headers = _get_auth_headers(jf_key)
            headers["Accept"] = "image/*,application/json"
            res = requests.get(
                f"{jf_url}/Items/{jellyfin_id}/Images/Primary", 
                headers=headers, 
                timeout=HTTP_TIMEOUT
            )
            if res.status_code == 200 and len(res.content) > 3000:
                with open(target_path, "wb") as f:
                    f.write(res.content)
                return True
        except Exception:
            pass

    tmdb_key = get_setting("tmdb_api_key", "")
    if tmdb_id and tmdb_key:
        try:
            endpoint_type = "tv" if media_type == "tv" else "movie"
            headers, params = {}, {}
            if tmdb_key.startswith("eyJ"):
                headers["Authorization"] = f"Bearer {tmdb_key}"
            else:
                params["api_key"] = tmdb_key

            meta_res = requests.get(
                f"https://api.themoviedb.org/3/{endpoint_type}/{tmdb_id}", 
                headers=headers, 
                params=params, 
                timeout=HTTP_TIMEOUT
            )
            if meta_res.status_code == 200 and meta_res.json().get("poster_path"):
                dl_res = requests.get(
                    f"https://image.tmdb.org/t/p/w500{meta_res.json()['poster_path']}", 
                    timeout=HTTP_TIMEOUT
                )
                if dl_res.status_code == 200 and len(dl_res.content) > 3000:
                    with open(target_path, "wb") as f:
                        f.write(dl_res.content)
                    return True
        except Exception:
            pass

    neg_cache_path.touch()
    return False

def sync_jellyfin_library(media_type="all"):
    if get_setting("disable_jellyfin_connection", "0") == "1":
        logger.info("Jellyfin connection is disabled; skipping sync.")
        return True

    jf_url = get_setting("jellyfin_url", "").rstrip("/")
    jf_key = get_setting("jellyfin_api_key", "")
    if not jf_url or not jf_key:
        logger.warning("Jellyfin URL or API Key unconfigured.")
        return False

    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    headers = _get_auth_headers(jf_key)

    include_types = "Movie" if media_type == "movie" else ("Series,Episode" if media_type == "tv" else "Movie,Series,Episode")

    try:
        series_tvdb_map = {}

        # Pre-fetch parent series TVDB IDs if syncing TV shows/episodes to avoid pagination race conditions
        if media_type in ("tv", "all"):
            try:
                series_res = logged_request(
                    "jellyfin", "FetchSeriesMap", "GET",
                    f"{jf_url}/Items?IncludeItemTypes=Series&Recursive=true&Fields=ProviderIds",
                    headers=headers, timeout=20
                )
                if series_res.status_code == 200:
                    for s_item in series_res.json().get("Items", []):
                        s_id = s_item.get("Id")
                        s_tvdb = str(s_item.get("ProviderIds", {}).get("Tvdb")) if s_item.get("ProviderIds", {}).get("Tvdb") else None
                        if s_id and s_tvdb:
                            series_tvdb_map[s_id] = s_tvdb
            except Exception as e:
                logger.warning(f"Failed pre-fetching Jellyfin series map: {e}")

        start_index = 0
        limit = 500
        matched_count = 0

        while True:
            endpoint = f"{jf_url}/Items?IncludeItemTypes={include_types}&Recursive=true&Fields=ProviderIds,ProductionYear,ParentIndexNumber,IndexNumber,SeriesName&StartIndex={start_index}&Limit={limit}"
            response = logged_request("jellyfin", "SyncLibrary", "GET", endpoint, headers=headers, timeout=30)
            if response.status_code != 200:
                logger.error(f"Jellyfin API error: HTTP {response.status_code}")
                return False
                
            items = response.json().get("Items", [])
            if not items:
                break
            
            for item in items:
                if item.get("Type") == "Series":
                    s_id = item.get("Id")
                    s_tvdb = str(item.get("ProviderIds", {}).get("Tvdb")) if item.get("ProviderIds", {}).get("Tvdb") else None
                    if s_id and s_tvdb:
                        series_tvdb_map[s_id] = s_tvdb

            for item in items:
                item_id = item.get("Id")
                item_name = item.get("Name", "Unknown Title")
                series_name, series_id = item.get("SeriesName"), item.get("SeriesId")
                season_num, episode_num = item.get("ParentIndexNumber"), item.get("IndexNumber")
                prod_year, item_type = item.get("ProductionYear"), item.get("Type")

                providers = item.get("ProviderIds", {})
                tmdb_id = str(providers.get("Tmdb")) if providers.get("Tmdb") else None
                imdb_id = providers.get("Imdb")
                tvdb_id = str(providers.get("Tvdb")) if providers.get("Tvdb") else None

                if item_type == "Movie":
                    media_type_val, display_title = "movie", item_name
                elif item_type == "Series":
                    media_type_val, display_title = "tv", item_name
                elif item_type == "Episode":
                    media_type_val = "episode"
                    tvdb_id = tvdb_id or series_tvdb_map.get(series_id)
                    display_title = f"{series_name} - S{season_num:02d}E{episode_num:02d} ({item_name})" if (series_name and season_num is not None and episode_num is not None) else item_name
                else:
                    continue

                update_service_cache(
                    "jellyfin", tmdb_id=tmdb_id, imdb_id=imdb_id, tvdb_id=tvdb_id, 
                    title=display_title, year=prod_year, media_type=media_type_val, 
                    season_number=season_num, episode_number=episode_num, status="in_library", 
                    in_library=1, external_item_id=item_id
                )
                matched_count += 1
            
            start_index += limit

        prune_service_cache("jellyfin", started)
        logger.info(f"Jellyfin ({media_type}) sync completed: Cached {matched_count} item(s).")
        return True
    except Exception as e:
        logger.error(f"Error syncing Jellyfin library: {e}")
        return False
    finally:
        reconcile_master_catalog()

def sync_jellyfin_playlist(playlist_name, item_ids):
    if get_setting("disable_jellyfin_connection", "0") == "1":
        return False, "Jellyfin connection is disabled in Settings."
    jf_url, jf_key = get_setting("jellyfin_url", "").rstrip("/"), get_setting("jellyfin_api_key", "")
    if not jf_url or not jf_key or not item_ids:
        return False, "Jellyfin unconfigured or no items to sync."

    headers = _get_auth_headers(jf_key)
    encoded_name = quote(playlist_name)
    chunk_size = 50
    
    try:
        res = logged_request("jellyfin", "SearchPlaylist", "GET", f"{jf_url}/Items?IncludeItemTypes=Playlist&SearchTerm={encoded_name}&Recursive=true", headers=headers, timeout=HTTP_TIMEOUT)
        playlists = res.json().get("Items", []) if res.status_code == 200 else []

        if playlists:
            playlist_id = playlists[0]["Id"]
            for i in range(0, len(item_ids), chunk_size):
                logged_request("jellyfin", "AddPlaylistItems", "POST", f"{jf_url}/Playlists/{playlist_id}/Items?Ids={','.join(item_ids[i:i + chunk_size])}", headers=headers, timeout=HTTP_TIMEOUT)
            return True, f"Updated playlist '{playlist_name}' with {len(item_ids)} items."
        else:
            res_create = logged_request("jellyfin", "CreatePlaylist", "POST", f"{jf_url}/Playlists?Name={encoded_name}&Ids={','.join(item_ids[:chunk_size])}", headers=headers, timeout=HTTP_TIMEOUT)
            if res_create.status_code in (200, 201):
                playlist_id = res_create.json().get("Id")
                for i in range(chunk_size, len(item_ids), chunk_size):
                    logged_request("jellyfin", "AddPlaylistItems", "POST", f"{jf_url}/Playlists/{playlist_id}/Items?Ids={','.join(item_ids[i:i + chunk_size])}", headers=headers, timeout=HTTP_TIMEOUT)
                return True, f"Created playlist '{playlist_name}' with {len(item_ids)} items."
            return False, f"Failed creating playlist: HTTP {res_create.status_code}"
    except Exception as e:
        return False, f"Playlist sync error: {e}"

def handle_playback_reporting(payload: dict):
    logger.info(f"Jellyfin playback event received: {payload.get('NotificationType')}")