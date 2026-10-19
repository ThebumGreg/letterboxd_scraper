# jellyfin_integration.py
import logging
import os
import requests
from database import get_setting, update_service_cache, reconcile_master_catalog

logger = logging.getLogger(__name__)

def get_jellyfin_item_link(item_id, user_agent=""):
    jf_url = get_setting("jellyfin_url", "").rstrip("/")
    primary_url = f"{jf_url}/web/index.html#/details?id={item_id}" if jf_url else "#"
    app_link = f"jellyfin://details?id={item_id}"
    return {"primary_url": primary_url, "app_link": app_link}

def fetch_and_cache_poster(slug, jellyfin_id=None, tmdb_id=None):
    posters_dir = os.path.join("static", "posters")
    os.makedirs(posters_dir, exist_ok=True)
    target_path = os.path.join(posters_dir, f"{slug}.jpg")

    if os.path.exists(target_path) and os.path.getsize(target_path) > 5000:
        return True

    jf_url = get_setting("jellyfin_url", "").rstrip("/")
    jf_key = get_setting("jellyfin_api_key", "")

    if jellyfin_id and jf_url and jf_key:
        img_url = f"{jf_url}/Items/{jellyfin_id}/Images/Primary?api_key={jf_key}"
        try:
            res = requests.get(img_url, timeout=10)
            if res.status_code == 200 and len(res.content) > 5000:
                with open(target_path, "wb") as f:
                    f.write(res.content)
                return True
        except Exception as e:
            logger.warning(f"Failed fetching poster from Jellyfin for {slug}: {e}")

    tmdb_key = get_setting("tmdb_api_key", "")
    if tmdb_id and tmdb_key:
        try:
            meta_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_key}"
            meta_res = requests.get(meta_url, timeout=10)
            if meta_res.status_code == 200:
                poster_path = meta_res.json().get("poster_path")
                if poster_path:
                    dl_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
                    dl_res = requests.get(dl_url, timeout=10)
                    if dl_res.status_code == 200 and len(dl_res.content) > 5000:
                        with open(target_path, "wb") as f:
                            f.write(dl_res.content)
                        return True
        except Exception as e:
            logger.warning(f"Failed fetching TMDB poster for {slug}: {e}")
    return False

def sync_jellyfin_library(media_type="all"):
    jf_url = get_setting("jellyfin_url", "").rstrip("/")
    jf_key = get_setting("jellyfin_api_key", "")
    if not jf_url or not jf_key:
        logger.warning("Jellyfin URL or API Key unconfigured.")
        return False

    headers = {"X-Emby-Token": jf_key}
    include_types = "Movie,Series,Episode"
    if media_type == "movie":
        include_types = "Movie"
    elif media_type == "tv":
        include_types = "Series,Episode"

    endpoint = f"{jf_url}/Items?IncludeItemTypes={include_types}&Recursive=true&Fields=ProviderIds,ProductionYear,ParentIndexNumber,IndexNumber,SeriesName"

    try:
        response = requests.get(endpoint, headers=headers, timeout=20)
        if response.status_code != 200:
            logger.error(f"Jellyfin API error: HTTP {response.status_code}")
            return False
            
        items = response.json().get("Items", [])
        matched_count = 0
        
        for item in items:
            item_id = item.get("Id")
            item_name = item.get("Name", "Unknown Title")
            series_name = item.get("SeriesName")
            season_num = item.get("ParentIndexNumber")
            episode_num = item.get("IndexNumber")
            prod_year = item.get("ProductionYear")
            item_type = item.get("Type")

            if item_type == "Movie":
                media_type_val = "movie"
                display_title = item_name
            elif item_type == "Series":
                media_type_val = "tv"
                display_title = item_name
            elif item_type == "Episode":
                media_type_val = "episode"
                if series_name and season_num is not None and episode_num is not None:
                    display_title = f"{series_name} - S{season_num:02d}E{episode_num:02d} ({item_name})"
                else:
                    display_title = item_name
            else:
                media_type_val = "unknown"
                display_title = item_name

            providers = item.get("ProviderIds", {})
            tmdb_id = str(providers.get("Tmdb")) if providers.get("Tmdb") else None
            imdb_id = providers.get("Imdb")
            tvdb_id = str(providers.get("Tvdb")) if providers.get("Tvdb") else None

            if tmdb_id or tvdb_id or imdb_id or display_title:
                update_service_cache(
                    service="jellyfin",
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    tvdb_id=tvdb_id,
                    title=display_title,
                    year=prod_year,
                    media_type=media_type_val,
                    season_number=season_num,
                    episode_number=episode_num,
                    status="in_library",
                    in_library=1,
                    external_item_id=item_id
                )
                matched_count += 1
                
        logger.info(f"Jellyfin ({media_type}) sync completed: Cached {matched_count} titles in database.")
        return True
    except Exception as e:
        logger.error(f"Error syncing Jellyfin library: {e}")
        return False
    finally:
        reconcile_master_catalog()

def sync_jellyfin_playlist(playlist_name, item_ids):
    jf_url = get_setting("jellyfin_url", "").rstrip("/")
    jf_key = get_setting("jellyfin_api_key", "")
    if not jf_url or not jf_key:
        return False, "Jellyfin credentials not configured."

    headers = {"X-Emby-Token": jf_key}
    try:
        search_url = f"{jf_url}/Items?IncludeItemTypes=Playlist&SearchTerm={playlist_name}&Recursive=true"
        res = requests.get(search_url, headers=headers, timeout=10)
        playlists = res.json().get("Items", []) if res.status_code == 200 else []

        if playlists:
            playlist_id = playlists[0]["Id"]
            add_url = f"{jf_url}/Playlists/{playlist_id}/Items?Ids={','.join(item_ids)}"
            requests.post(add_url, headers=headers, timeout=10)
            return True, f"Updated existing playlist '{playlist_name}' with {len(item_ids)} items."
        else:
            create_url = f"{jf_url}/Playlists?Name={playlist_name}&Ids={','.join(item_ids)}"
            res_create = requests.post(create_url, headers=headers, timeout=10)
            if res_create.status_code in (200, 201):
                return True, f"Created new playlist '{playlist_name}' with {len(item_ids)} items."
            return False, f"Failed creating playlist: HTTP {res_create.status_code}"
    except Exception as e:
        return False, f"Playlist sync error: {e}"

def handle_playback_reporting(payload: dict):
    logger.info(f"Jellyfin playback event received: {payload.get('NotificationType')}")

def evaluate_media_quality(external_item_id: str, current_quality: str, target_quality: str):
    logger.info(f"Evaluating quality for {external_item_id}: {current_quality} vs {target_quality}")
    pass