# sonarr_client.py
import requests
from database import get_setting

TIMEOUT = 15

def _conn():
    url = get_setting("sonarr_url", "").rstrip("/")
    key = get_setting("sonarr_api_key", "")
    if not url or not key:
        raise RuntimeError("Sonarr URL or API key missing.")
    return url, {"X-Api-Key": key, "Content-Type": "application/json"}

def get_library_tvdb_ids() -> dict:
    url, headers = _conn()
    r = requests.get(f"{url}/api/v3/series", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return {str(s["tvdbId"]): str(s["id"]) for s in r.json() if s.get("tvdbId")}

def add_series(tvdb_id, season_number=None, dry_run=False) -> dict:
    url, headers = _conn()
    r = requests.get(f"{url}/api/v3/series/lookup", headers=headers,
                     params={"term": f"tvdb:{tvdb_id}"}, timeout=TIMEOUT)
    r.raise_for_status()
    results = r.json()
    if not results:
        return {"status": "not_found", "message": f"TVDB {tvdb_id} not found in Sonarr lookup."}

    show = results[0]
    if dry_run:
        return {"status": "dry_run", "message": f"Would add '{show['title']}'."}

    seasons = show.get("seasons", [])
    if season_number:  # monitor only the requested season, keep the rest in the list
        for s in seasons:
            s["monitored"] = (s["seasonNumber"] == int(season_number))

    payload = {
        "title": show["title"],
        "titleSlug": show["titleSlug"],
        "tvdbId": show["tvdbId"],
        "year": show.get("year"),
        "images": show.get("images", []),
        "seasons": seasons,
        "qualityProfileId": int(get_setting("sonarr_quality_profile_id", "1")),
        "rootFolderPath": get_setting("sonarr_root_folder", "/tv"),
        "monitored": True,
        "addOptions": {"searchForMissingEpisodes": True, "ignoreEpisodesWithFiles": True},
    }
    res = requests.post(f"{url}/api/v3/series", json=payload, headers=headers, timeout=TIMEOUT)
    if res.status_code in (200, 201):
        return {"status": "added", "sonarr_id": str(res.json().get("id")), "message": f"Added '{show['title']}'."}
    return {"status": "error", "message": f"Sonarr HTTP {res.status_code}: {res.text[:200]}"}