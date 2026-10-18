import logging
import json
from datetime import datetime, timezone
from database import (get_aggregated_active_films, get_external_library_cache,
                      get_setting, update_external_cache, update_job_status, prune_external_cache )
import radarr_client, sonarr_client

logger = logging.getLogger(__name__)

def sync_radarr_library():
    """Mirrors every Radarr movie into the cache (runs regardless of the automation toggle).
    Returns ({tmdb_id: radarr_id}, removed_count)."""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    movies = radarr_client.get_library()

    for m in movies:
        title = f"{m['title']} ({m['year']})" if m.get("year") else m["title"]
        update_external_cache(
            "radarr",
            tmdb_id=m["tmdb_id"],
            imdb_id=m["imdb_id"],
            status="downloaded" if m["has_file"] else "monitored",
            in_library=1,
            external_item_id=m["radarr_id"],
            details_json=json.dumps({"title": title, "has_file": m["has_file"]}),
        )

    # Only reached if the fetch succeeded, so movies deleted from Radarr drop out of the cache
    removed = prune_external_cache("radarr", started)
    return {m["tmdb_id"]: m["radarr_id"] for m in movies}, removed


def run_radarr_automation_worker() -> None:
    update_job_status("radarr", "in_progress", "Fetching Radarr library...")
    try:
        in_radarr, removed = sync_radarr_library()
        summary = f"Synced {len(in_radarr)} movie(s) from Radarr"
        summary += f" ({removed} stale removed)." if removed else "."

        if get_setting("enable_radarr_automation", "0") != "1":
            update_job_status("radarr", "completed", summary + " Automation is off, nothing added.")
            return

        update_job_status("radarr", "in_progress", summary + " Evaluating watchlist...")
        min_users = int(get_setting("radarr_min_user_count", "1"))
        dry_run = get_setting("radarr_dry_run", "0") == "1"
        in_jellyfin = {str(i["tmdb_id"]) for i in get_external_library_cache("jellyfin") if i.get("tmdb_id")}

        added = 0
        for film in get_aggregated_active_films(list_type="Watchlist", media_type="movie"):
            tmdb = str(film.get("tmdb_id") or "")
            if (not tmdb or film.get("user_count", 0) < min_users
                    or tmdb in in_jellyfin or tmdb in in_radarr):
                continue

            result = radarr_client.add_movie(tmdb, dry_run=dry_run)
            if result["status"] == "added":
                added += 1
                in_radarr[tmdb] = result["radarr_id"]
                update_external_cache("radarr", tmdb_id=tmdb, status="monitored", in_library=1,
                                      external_item_id=result["radarr_id"],
                                      details_json=json.dumps({"title": film.get("full_display_name")}))
            elif result["status"] == "error":
                logger.warning(f"Radarr add failed for {tmdb}: {result['message']}")

        update_job_status("radarr", "completed",
                          f"{summary} {'Dry run' if dry_run else 'Done'}. Added {added} new movie(s).")
    except Exception as e:
        logger.error(f"Radarr automation error: {e}")
        update_job_status("radarr", "failed", str(e))
        raise


def run_sonarr_automation_worker() -> None:
    if get_setting("enable_sonarr_automation", "0") != "1":
        update_job_status("sonarr", "idle", "Sonarr automation disabled in settings.")
        return

    update_job_status("sonarr", "in_progress", "Evaluating missing TV shows...")
    try:
        min_users = int(get_setting("sonarr_min_user_count", "1"))
        dry_run = get_setting("sonarr_dry_run", "0") == "1"

        in_sonarr = sonarr_client.get_library_tvdb_ids()
        for tvdb, sid in in_sonarr.items():
            update_external_cache("sonarr", tvdb_id=tvdb, status="in_library",
                                  in_library=1, external_item_id=sid)

        in_jellyfin = {str(i["tvdb_id"]) for i in get_external_library_cache("jellyfin") if i.get("tvdb_id")}

        added = 0
        for show in get_aggregated_active_films():
            tvdb = str(show.get("tvdb_id") or "")
            if (show.get("media_type") != "tv" or not tvdb
                    or show.get("user_count", 0) < min_users
                    or tvdb in in_jellyfin or tvdb in in_sonarr):
                continue

            result = sonarr_client.add_series(
                tvdb, season_number=show.get("season_number"), dry_run=dry_run
            )
            if result["status"] == "added":
                added += 1
                in_sonarr[tvdb] = result["sonarr_id"]
                update_external_cache("sonarr", tvdb_id=tvdb, status="added",
                                      in_library=1, external_item_id=result["sonarr_id"])
            elif result["status"] == "error":
                logger.warning(f"Sonarr add failed for {tvdb}: {result['message']}")

        update_job_status("sonarr", "completed",
                          f"{'Dry run' if dry_run else 'Done'}. Added {added} new show(s).")
    except Exception as e:
        logger.error(f"Sonarr automation error: {e}")
        update_job_status("sonarr", "failed", str(e))