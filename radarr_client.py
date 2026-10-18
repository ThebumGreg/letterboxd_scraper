# radarr_client.py
import requests
from database import get_setting

TIMEOUT = 15

def _conn():
    url = get_setting("radarr_url", "").rstrip("/")
    key = get_setting("radarr_api_key", "")
    if not url or not key:
        raise RuntimeError("Radarr URL or API key missing.")
    return url, {"X-Api-Key": key, "Content-Type": "application/json"}

def get_library_tmdb_ids() -> dict:
    """Returns {tmdb_id: radarr_id}. Raises on any failure."""
    url, headers = _conn()
    r = requests.get(f"{url}/api/v3/movie", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return {str(m["tmdbId"]): str(m["id"]) for m in r.json() if m.get("tmdbId")}

def add_movie(tmdb_id, dry_run=False) -> dict:
    url, headers = _conn()
    r = requests.get(f"{url}/api/v3/movie/lookup", headers=headers,
                     params={"term": f"tmdb:{tmdb_id}"}, timeout=TIMEOUT)
    r.raise_for_status()
    results = r.json()
    if not results:
        return {"status": "not_found", "message": f"TMDB {tmdb_id} not found in Radarr lookup."}

    movie = results[0]
    if dry_run:
        return {"status": "dry_run", "message": f"Would add '{movie['title']}'."}

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
    res = requests.post(f"{url}/api/v3/movie", json=payload, headers=headers, timeout=TIMEOUT)
    if res.status_code in (200, 201):
        return {"status": "added", "radarr_id": str(res.json().get("id")), "message": f"Added '{movie['title']}'."}
    return {"status": "error", "message": f"Radarr HTTP {res.status_code}: {res.text[:200]}"}

def get_library() -> list:
    """Every movie in Radarr. Raises on any failure."""
    url, headers = _conn()
    r = requests.get(f"{url}/api/v3/movie", headers=headers, timeout=60)
    r.raise_for_status()
    movies = []
    for m in r.json():
        if not m.get("tmdbId"):
            continue
        movies.append({
            "radarr_id": str(m["id"]),
            "tmdb_id": str(m["tmdbId"]),
            "imdb_id": m.get("imdbId"),
            "title": m.get("title") or "Unknown",
            "year": m.get("year"),
            "has_file": bool(m.get("hasFile")),
        })
    return movies

def get_library_tmdb_ids() -> dict:
    return {m["tmdb_id"]: m["radarr_id"] for m in get_library()}