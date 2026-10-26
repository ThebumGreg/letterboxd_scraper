from synchub import http, settings
from synchub.clients import ApiError, ConfigError

BASE = "https://api.themoviedb.org/3"
APPEND_LIMIT = 20   # TMDB allows at most 20 appended sub-requests per call


class TmdbClient:
    def __init__(self, api_key=None):
        self.key = (api_key or settings.get("tmdb_api_key") or "").strip()
        if not self.key:
            raise ConfigError("TMDB API key is not configured.")

    def _get(self, path, **params):
        headers = {}
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        res = http.request("tmdb", path.split("/")[1] if "/" in path else path, "GET", f"{BASE}{path}", headers=headers, params=params)
        if res.status_code == 404:
            return None
        if res.status_code >= 400:
            raise ApiError(f"TMDB {path} failed: HTTP {res.status_code}")
        return res.json()

    def configuration(self):
        return self._get("/configuration")

    def movie(self, tmdb_id):
        return self._get(f"/movie/{tmdb_id}", append_to_response="release_dates")

    def show(self, tmdb_id):
        return self._get(f"/tv/{tmdb_id}", append_to_response="external_ids,content_ratings")

    def seasons(self, tmdb_id, numbers):
        """{season_number: season_json} for the given season numbers (Season 0 = Specials), in batched calls."""
        numbers = sorted(set(numbers))
        found = {}
        for i in range(0, len(numbers), APPEND_LIMIT):
            chunk = numbers[i:i + APPEND_LIMIT]
            body = self._get(f"/tv/{tmdb_id}", append_to_response=",".join(f"season/{n}" for n in chunk))
            for n in chunk:
                if body and body.get(f"season/{n}"):
                    found[n] = body[f"season/{n}"]
        return found

    def find(self, external_id, source):
        """source: 'tvdb_id' or 'imdb_id'. Returns TMDB's find result ({'movie_results': [...], 'tv_results': [...]})."""
        return self._get(f"/find/{external_id}", external_source=source) or {}

    def search_movie(self, title, year=None):
        params = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = year
        return (self._get("/search/movie", **params) or {}).get("results", [])

    def search_tv(self, title, year=None):
        params = {"query": title, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        return (self._get("/search/tv", **params) or {}).get("results", [])

    # ---- lists for the Discover page (each is one page of 20 results, newest data TMDB has)
    def trending(self, kind="all", window="day"):
        """kind: 'all', 'movie' or 'tv'; window: 'day' or 'week'. 'all' mixes movies and shows (and people, which the caller drops)."""
        return (self._get(f"/trending/{kind}/{window}") or {}).get("results", [])

    def popular_movies(self):
        return (self._get("/movie/popular") or {}).get("results", [])

    def popular_tv(self):
        return (self._get("/tv/popular") or {}).get("results", [])

    def upcoming_movies(self):
        return (self._get("/movie/upcoming") or {}).get("results", [])

    def airing_this_week(self):
        return (self._get("/tv/on_the_air") or {}).get("results", [])
