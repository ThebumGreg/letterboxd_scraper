import logging
import json
from datetime import datetime, timezone

from database import (get_aggregated_active_films, get_external_library_cache,
                      get_setting, update_external_cache, update_job_status, prune_external_cache
)
import radarr_client, sonarr_client

logger = logging.getLogger(__name__)

def sync_radarr_library():
    """Mirrors every Radarr movie into the cache (runs if library sync is enabled)."""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    movies = radarr_client.get_library()
    for m in movies:
        title = f"{m['title']} ({m['year']})" if m.get("year") else m["title"]
        update_external_cache(
            "radarr",
            tmdb_id=m["tmdb_id"],
            imdb_id=m["imdb_id"],
            title=title,
            year=m.get("year"),
            media_type="movie",
            status="downloaded" if m["has_file"] else "monitored",
            in_library=1,
            external_item_id=m["radarr_id"],
            details_json=json.dumps({"has_file": m["has_file"]}),
        )
    removed = prune_external_cache("radarr", started)
    return {m["tmdb_id"]: m["radarr_id"] for m in movies}, removed

def sync_sonarr_library():
    """Mirrors every Sonarr show into the cache (runs if library sync is enabled)."""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    shows = sonarr_client.get_library()
    for s in shows:
        title = f"{s['title']} ({s['year']})" if s.get("year") else s["title"]
        update_external_cache(
            "sonarr",
            tmdb_id=None,
            imdb_id=s.get("imdb_id"),
            tvdb_id=s["tvdb_id"],
            title=title,
            year=s.get("year"),
            media_type="tv",
            status="downloaded" if s["has_file"] else "monitored",
            in_library=1,
            external_item_id=s["sonarr_id"],
            details_json=json.dumps({"has_file": s["has_file"]}),
        )
    removed = prune_external_cache("sonarr", started)
    return {s["tvdb_id"]: s["sonarr_id"] for s in shows}, removed


def run_radarr_automation_worker() -> None:
    update_job_status("radarr", "in_progress", "Starting Radarr automation...")
    try:
        sync_enabled = get_setting("radarr_enable_library_sync", "1") == "1"
        add_enabled = get_setting("radarr_enable_queue_addition", "0") == "1"

        in_radarr = {}
        if sync_enabled:
            update_job_status("radarr", "in_progress", "Fetching Radarr library...")
            in_radarr, removed = sync_radarr_library()
            summary = f"Synced {len(in_radarr)} movie(s) from Radarr"
            summary += f" ({removed} stale removed)." if removed else "."
        else:
            cached = get_external_library_cache("radarr")
            in_radarr = {c["tmdb_id"]: c["external_item_id"] for c in cached if c.get("tmdb_id")}
            summary = "Library sync disabled, using cached Radarr data."

        if not add_enabled:
            update_job_status("radarr", "completed", summary + " Queue addition is disabled.")
            return

        update_job_status("radarr", "in_progress", summary + " Evaluating watchlist...")
        min_users = int(get_setting("radarr_min_user_count", "1"))
        dry_run = get_setting("radarr_dry_run", "0") == "1"
        in_jellyfin = {str(i["tmdb_id"]) for i in get_external_library_cache("jellyfin") if i.get("tmdb_id")}
        
        added = 0
        for film in get_aggregated_active_films(list_type="Watchlist", media_type="movie"):
            tmdb = str(film.get("tmdb_id") or "")
            if (not tmdb or film.get("user_count", 0) < min_users
                    or tmdb in in_jellyfin or tmdb in in_radarr
                    or film.get("id_source") == "tmdb-search"):
                continue
            
            result = radarr_client.add_movie(tmdb, dry_run=dry_run)
            if result["status"] == "added":
                added += 1
                in_radarr[tmdb] = result["radarr_id"]
                update_external_cache("radarr", tmdb_id=tmdb, title=film.get("full_display_name"), media_type="movie", status="monitored", in_library=1, external_item_id=result["radarr_id"])
            elif result["status"] == "error":
                logger.warning(f"Radarr add failed for {tmdb}: {result['message']}")
        
        update_job_status("radarr", "completed",
                          f"{summary} {'Dry run' if dry_run else 'Done'}. Added {added} new movie(s).")
    except Exception as e:
        logger.error(f"Radarr automation error: {e}")
        update_job_status("radarr", "failed", str(e))
        raise

def run_sonarr_automation_worker() -> None:
    update_job_status("sonarr", "in_progress", "Starting Sonarr automation...")
    try:
        sync_enabled = get_setting("sonarr_enable_library_sync", "1") == "1"
        add_enabled = get_setting("sonarr_enable_queue_addition", "0") == "1"

        in_sonarr = {}
        if sync_enabled:
            update_job_status("sonarr", "in_progress", "Fetching Sonarr library...")
            in_sonarr, removed = sync_sonarr_library()
            summary = f"Synced {len(in_sonarr)} show(s) from Sonarr"
            summary += f" ({removed} stale removed)." if removed else "."
        else:
            cached = get_external_library_cache("sonarr")
            in_sonarr = {c["tvdb_id"]: c["external_item_id"] for c in cached if c.get("tvdb_id")}
            summary = "Library sync disabled, using cached Sonarr data."

        if not add_enabled:
            update_job_status("sonarr", "completed", summary + " Queue addition is disabled.")
            return

        update_job_status("sonarr", "in_progress", summary + " Evaluating missing TV shows...")
        min_users = int(get_setting("sonarr_min_user_count", "1"))
        dry_run = get_setting("sonarr_dry_run", "0") == "1"

        in_jellyfin = {str(i["tvdb_id"]) for i in get_external_library_cache("jellyfin") if i.get("tvdb_id")}
        added = 0
        
        for show in get_aggregated_active_films(list_type="Watchlist", media_type="tv"):
            tvdb = str(show.get("tvdb_id") or "")
            if (not tvdb or show.get("user_count", 0) < min_users
                    or tvdb in in_jellyfin or tvdb in in_sonarr):
                continue

            result = sonarr_client.add_series(
                tvdb, season_number=show.get("season_number"), dry_run=dry_run
            )
            if result["status"] == "added":
                added += 1
                in_sonarr[tvdb] = result["sonarr_id"]
                update_external_cache("sonarr", tvdb_id=tvdb, title=show.get("full_display_name"), media_type="tv", status="added", in_library=1, external_item_id=result["sonarr_id"])
            elif result["status"] == "error":
                logger.warning(f"Sonarr add failed for {tvdb}: {result['message']}")
        
        update_job_status("sonarr", "completed",
                          f"{summary} {'Dry run' if dry_run else 'Done'}. Added {added} new show(s).")
    except Exception as e:
        logger.error(f"Sonarr automation error: {e}")
        update_job_status("sonarr", "failed", str(e))
        raise