# sonarr_client.py
from database import get_setting, logged_request

TIMEOUT = 15

def _conn():
    url = get_setting("sonarr_url", "").rstrip("/")
    key = get_setting("sonarr_api_key", "")
    if not url or not key:
        raise RuntimeError("Sonarr URL or API key missing.")
    return url, {"X-Api-Key": key, "Content-Type": "application/json"}

def get_quality_profiles(override_url=None, override_key=None) -> list:
    url = (override_url or get_setting("sonarr_url", "")).rstrip("/")
    key = override_key or get_setting("sonarr_api_key", "")
    if not url or not key: return []
    r = logged_request("sonarr", "GetQualityProfiles", "GET", f"{url}/api/v3/qualityprofile", headers={"X-Api-Key": key}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_root_folders(override_url=None, override_key=None) -> list:
    url = (override_url or get_setting("sonarr_url", "")).rstrip("/")
    key = override_key or get_setting("sonarr_api_key", "")
    if not url or not key: return []
    r = logged_request("sonarr", "GetRootFolders", "GET", f"{url}/api/v3/rootfolder", headers={"X-Api-Key": key}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_library() -> list:
    url, headers = _conn()
    r = logged_request("sonarr", "GetLibrary", "GET", f"{url}/api/v3/series", headers=headers, timeout=60)
    r.raise_for_status()
    return [{ "sonarr_id": str(s["id"]), "tvdb_id": str(s["tvdbId"]), "imdb_id": s.get("imdbId"), "title": s.get("title") or "Unknown", "year": s.get("year"), "has_file": s.get("statistics", {}).get("episodeFileCount", 0) > 0 } for s in r.json() if s.get("tvdbId")]

def get_library_tvdb_ids() -> dict:
    return {s["tvdb_id"]: s["sonarr_id"] for s in get_library()}

def get_series_by_tvdb(tvdb_id: str) -> dict:
    url, headers = _conn()
    r = logged_request("sonarr", "GetSeriesByTvdb", "GET", f"{url}/api/v3/series", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    for series in r.json():
        if str(series.get("tvdbId")) == str(tvdb_id): return series
    return None

def get_episodes_for_series(sonarr_id: str) -> list:
    url, headers = _conn()
    r = logged_request("sonarr", "GetEpisodesForSeries", "GET", f"{url}/api/v3/episode", headers=headers, params={"seriesId": sonarr_id}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_episodes_by_tvdb(tvdb_id: str) -> list:
    series = get_series_by_tvdb(tvdb_id)
    if not series:
        return []
    return get_episodes_for_series(str(series["id"]))

def monitor_episodes(episode_ids: list, monitored: bool = True) -> dict:
    if not episode_ids:
        return {"status": "ok", "count": 0}
    url, headers = _conn()
    payload = {"episodeIds": [int(eid) for eid in episode_ids], "monitored": bool(monitored)}
    r = logged_request("sonarr", "MonitorEpisodes", "PUT", f"{url}/api/v3/episode/monitor", json=payload, headers=headers, timeout=TIMEOUT)
    if r.status_code in (200, 202):
        return {"status": "success", "count": len(episode_ids)}
    return {"status": "error", "message": f"Sonarr HTTP {r.status_code}: {r.text[:200]}"}

def search_episodes(episode_ids: list) -> dict:
    if not episode_ids:
        return {"status": "ok", "count": 0}
    url, headers = _conn()
    payload = {"name": "EpisodeSearch", "episodeIds": [int(eid) for eid in episode_ids]}
    r = logged_request("sonarr", "SearchEpisodes", "POST", f"{url}/api/v3/command", json=payload, headers=headers, timeout=TIMEOUT)
    if r.status_code in (200, 201):
        return {"status": "success", "command_id": r.json().get("id")}
    return {"status": "error", "message": f"Sonarr HTTP {r.status_code}: {r.text[:200]}"}

def update_series(sonarr_id: str, series_payload: dict) -> dict:
    url, headers = _conn()
    res = logged_request("sonarr", "UpdateSeries", "PUT", f"{url}/api/v3/series/{sonarr_id}", json=series_payload, headers=headers, timeout=TIMEOUT)
    res.raise_for_status()
    return res.json()

def add_series(tvdb_id, season_number=None, monitored_seasons=None, dry_run=False) -> dict:
    url, headers = _conn()
    r = logged_request("sonarr", "LookupSeries", "GET", f"{url}/api/v3/series/lookup", headers=headers, params={"term": f"tvdb:{tvdb_id}"}, timeout=TIMEOUT)
    r.raise_for_status()
    results = r.json()
    if not results: return {"status": "not_found", "message": f"TVDB {tvdb_id} not found in Sonarr lookup."}
    
    show = results[0]
    if dry_run: return {"status": "dry_run", "message": f"Would add '{show['title']}'."}
    
    seasons = show.get("seasons", [])
    target_seasons = monitored_seasons or (season_number if isinstance(season_number, list) else ([int(season_number)] if season_number is not None else None))
    
    if target_seasons is not None:
        target_seasons = [int(s) for s in target_seasons]
        for s in seasons: s["monitored"] = (s.get("seasonNumber") in target_seasons)
            
    payload = {
        "title": show["title"], "titleSlug": show["titleSlug"], "tvdbId": show["tvdbId"], "year": show.get("year"), "images": show.get("images", []), "seasons": seasons,
        "qualityProfileId": int(get_setting("sonarr_quality_profile_id", "1")), "rootFolderPath": get_setting("sonarr_root_folder", "/tv"), "monitored": True, "addOptions": {"searchForMissingEpisodes": True, "ignoreEpisodesWithFiles": True},
    }
    
    res = logged_request("sonarr", "AddSeries", "POST", f"{url}/api/v3/series", json=payload, headers=headers, timeout=TIMEOUT)
    if res.status_code in (200, 201): return {"status": "added", "sonarr_id": str(res.json().get("id")), "message": f"Added '{show['title']}'."}
    return {"status": "error", "message": f"Sonarr HTTP {res.status_code}: {res.text[:200]}"}