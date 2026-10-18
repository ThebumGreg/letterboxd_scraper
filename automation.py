import logging
import requests
from database import (
    get_aggregated_active_films,
    get_external_library_cache,
    get_setting,
    update_external_cache,
    update_job_status,
)

logger = logging.getLogger(__name__)


def run_radarr_automation_worker() -> None:
    """Evaluates active watchlists and sends missing movies to Radarr if enabled."""
    enabled = get_setting("enable_radarr_automation", "0") == "1"
    if not enabled:
        update_job_status("radarr", "idle", "Radarr automation disabled in settings.")
        return

    radarr_url = get_setting("radarr_url").rstrip("/")
    api_key = get_setting("radarr_api_key")
    min_users = int(get_setting("radarr_min_user_count", "1"))
    root_folder = get_setting("radarr_root_folder", "/movies")
    quality_profile = int(get_setting("radarr_quality_profile_id", "1"))

    if not radarr_url or not api_key:
        update_job_status("radarr", "failed", "Radarr URL or API key missing.")
        return

    update_job_status("radarr", "in_progress", "Evaluating missing movies...")

    try:
        active_films = [f for f in get_aggregated_active_films() if f.get("media_type", "movie") == "movie"]
        jf_cache = get_external_library_cache("jellyfin")
        existing_tmdb_ids = {str(item["tmdb_id"]) for item in jf_cache if item.get("tmdb_id")}

        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        radarr_resp = requests.get(f"{radarr_url}/api/v3/movie", headers=headers, timeout=10)
        radarr_tmdb_ids = set()
        
        if radarr_resp.status_code == 200:
            for m in radarr_resp.json():
                tmdb = m.get("tmdbId")
                if tmdb:
                    radarr_tmdb_ids.add(str(tmdb))
                    update_external_cache("radarr", tmdb_id=str(tmdb), status="in_library", in_library=1, external_item_id=str(m.get("id")))

        added_count = 0
        for film in active_films:
            tmdb_id = film.get("tmdb_id")
            user_count = film.get("user_count", 0)

            if not tmdb_id:
                continue

            tmdb_str = str(tmdb_id)

            if (
                user_count >= min_users
                and tmdb_str not in existing_tmdb_ids
                and tmdb_str not in radarr_tmdb_ids
            ):
                payload = {
                    "title": film.get("movie_title"),
                    "qualityProfileId": quality_profile,
                    "titleSlug": film.get("movie_slug"),
                    "tmdbId": int(tmdb_id),
                    "rootFolderPath": root_folder,
                    "monitored": True,
                    "addOptions": {"searchForMovie": True},
                }
                res = requests.post(f"{radarr_url}/api/v3/movie", json=payload, headers=headers, timeout=10)
                if res.status_code in [200, 201]:
                    added_count += 1
                    radarr_tmdb_ids.add(tmdb_str)
                    res_data = res.json()
                    update_external_cache(
                        "radarr",
                        tmdb_id=tmdb_str,
                        status="added",
                        in_library=1,
                        external_item_id=str(res_data.get("id"))
                    )

        update_job_status(
            "radarr",
            "completed",
            f"Automated evaluation complete. Added {added_count} new movie(s) to Radarr.",
        )
    except Exception as e:
        logger.error(f"Radarr automation error: {e}")
        update_job_status("radarr", "failed", str(e))


def run_sonarr_automation_worker() -> None:
    """Evaluates active watchlists and sends missing TV shows to Sonarr if enabled."""
    enabled = get_setting("enable_sonarr_automation", "0") == "1"
    if not enabled:
        update_job_status("sonarr", "idle", "Sonarr automation disabled in settings.")
        return

    sonarr_url = get_setting("sonarr_url").rstrip("/")
    api_key = get_setting("sonarr_api_key")
    min_users = int(get_setting("sonarr_min_user_count", "1"))
    root_folder = get_setting("sonarr_root_folder", "/tv")
    quality_profile = int(get_setting("sonarr_quality_profile_id", "1"))

    if not sonarr_url or not api_key:
        update_job_status("sonarr", "failed", "Sonarr URL or API key missing.")
        return

    update_job_status("sonarr", "in_progress", "Evaluating missing TV shows...")

    try:
        active_shows = [f for f in get_aggregated_active_films() if f.get("media_type") == "tv"]
        jf_cache = get_external_library_cache("jellyfin")
        existing_tvdb_ids = {str(item["tvdb_id"]) for item in jf_cache if item.get("tvdb_id")}

        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        sonarr_resp = requests.get(f"{sonarr_url}/api/v3/series", headers=headers, timeout=10)
        sonarr_tvdb_ids = set()

        if sonarr_resp.status_code == 200:
            for s in sonarr_resp.json():
                tvdb = s.get("tvdbId")
                if tvdb:
                    sonarr_tvdb_ids.add(str(tvdb))
                    update_external_cache("sonarr", tvdb_id=str(tvdb), status="in_library", in_library=1, external_item_id=str(s.get("id")))

        added_count = 0
        for show in active_shows:
            tvdb_id = show.get("tvdb_id")
            user_count = show.get("user_count", 0)
            season_num = show.get("season_number")

            if not tvdb_id:
                continue

            tvdb_str = str(tvdb_id)

            if (
                user_count >= min_users
                and tvdb_str not in existing_tvdb_ids
                and tvdb_str not in sonarr_tvdb_ids
            ):
                payload = {
                    "title": show.get("movie_title"),
                    "qualityProfileId": quality_profile,
                    "titleSlug": show.get("movie_slug"),
                    "tvdbId": int(tvdb_id),
                    "rootFolderPath": root_folder,
                    "monitored": True,
                    "addOptions": {
                        "searchForMissingEpisodes": True,
                        "ignoreEpisodesWithFiles": True,
                        "searchForCutoffUnmetEpisodes": False
                    },
                }

                # If a specific season was requested from Serializd/Letterboxd, set season monitoring
                if season_num:
                    payload["seasons"] = [{"seasonNumber": int(season_num), "monitored": True}]

                res = requests.post(f"{sonarr_url}/api/v3/series", json=payload, headers=headers, timeout=10)
                if res.status_code in [200, 201]:
                    added_count += 1
                    sonarr_tvdb_ids.add(tvdb_str)
                    res_data = res.json()
                    update_external_cache(
                        "sonarr",
                        tvdb_id=tvdb_str,
                        status="added",
                        in_library=1,
                        external_item_id=str(res_data.get("id"))
                    )

        update_job_status(
            "sonarr",
            "completed",
            f"Automated evaluation complete. Added {added_count} new show(s) to Sonarr.",
        )
    except Exception as e:
        logger.error(f"Sonarr automation error: {e}")
        update_job_status("sonarr", "failed", str(e))