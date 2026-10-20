# radarr_client.py
from database import get_setting, logged_request

TIMEOUT = 15

def _conn():
    url = get_setting("radarr_url", "").rstrip("/")
    key = get_setting("radarr_api_key", "")
    if not url or not key:
        raise RuntimeError("Radarr URL or API key missing.")
    return url, {"X-Api-Key": key, "Content-Type": "application/json"}

def get_quality_profiles(override_url=None, override_key=None) -> list:
    url = (override_url or get_setting("radarr_url", "")).rstrip("/")
    key = override_key or get_setting("radarr_api_key", "")
    if not url or not key: return []
    r = logged_request("radarr", "GetQualityProfiles", "GET", f"{url}/api/v3/qualityprofile", headers={"X-Api-Key": key}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_root_folders(override_url=None, override_key=None) -> list:
    url = (override_url or get_setting("radarr_url", "")).rstrip("/")
    key = override_key or get_setting("radarr_api_key", "")
    if not url or not key: return []
    r = logged_request("radarr", "GetRootFolders", "GET", f"{url}/api/v3/rootfolder", headers={"X-Api-Key": key}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_library_tmdb_ids() -> dict:
    url, headers = _conn()
    r = logged_request("radarr", "GetLibraryTMDB", "GET", f"{url}/api/v3/movie", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return {str(m["tmdbId"]): str(m["id"]) for m in r.json() if m.get("tmdbId")}

def add_movie(tmdb_id, dry_run=False) -> dict:
    url, headers = _conn()
    r = logged_request("radarr", "LookupMovie", "GET", f"{url}/api/v3/movie/lookup", headers=headers, params={"term": f"tmdb:{tmdb_id}"}, timeout=TIMEOUT)
    r.raise_for_status()
    results = r.json()
    if not results:
        return {"status": "not_found", "message": f"TMDB {tmdb_id} not found in Radarr lookup."}

    movie = results[0]
    if dry_run: return {"status": "dry_run", "message": f"Would add '{movie['title']}'."}

    payload = {
        "title": movie["title"],
        "titleSlug": movie["titleSlug"],
        "tmdbId": movie["tmdbId"],
        "year": movie.get("year"),
        "images": movie.get("images", []),
        "qualityProfileId": int(get_setting("radarr_quality_profile_id", "1")),
        "rootFolderPath": get_setting("radarr_root_folder", "/movies"),
        "monitored": True,
        "addOptions": {"searchForMovie": True},
    }
    res = logged_request("radarr", "AddMovie", "POST", f"{url}/api/v3/movie", json=payload, headers=headers, timeout=TIMEOUT)
    if res.status_code in (200, 201): return {"status": "added", "radarr_id": str(res.json().get("id")), "message": f"Added '{movie['title']}'."}
    return {"status": "error", "message": f"Radarr HTTP {res.status_code}: {res.text[:200]}"}

def get_library() -> list:
    url, headers = _conn()
    r = logged_request("radarr", "GetLibrary", "GET", f"{url}/api/v3/movie", headers=headers, timeout=60)
    r.raise_for_status()
    return [{ "radarr_id": str(m["id"]), "tmdb_id": str(m["tmdbId"]), "imdb_id": m.get("imdbId"), "title": m.get("title") or "Unknown", "year": m.get("year"), "has_file": bool(m.get("hasFile")) } for m in r.json() if m.get("tmdbId")]