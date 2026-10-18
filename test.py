# test_radarr.py
import requests
from database import get_setting

url = get_setting("radarr_url", "").rstrip("/")
key = get_setting("radarr_api_key", "")
print("URL:", repr(url), "| key set:", bool(key), f"({len(key)} chars)")

try:
    r = requests.get(f"{url}/api/v3/movie", headers={"X-Api-Key": key}, timeout=15)
    print("HTTP", r.status_code)
    if r.ok:
        movies = r.json()
        print("Movies in Radarr:", len(movies))
        print("With tmdbId:", sum(1 for m in movies if m.get("tmdbId")))
        print("Sample:", [(m["title"], m.get("tmdbId")) for m in movies[:3]])
    else:
        print(r.text[:300])
except Exception as e:
    print("Connection failed:", type(e).__name__, e)