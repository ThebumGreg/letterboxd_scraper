from synchub import http, settings
from synchub.clients import ApiError, check, require


class _Arr:
    service = ""
    label = ""

    def __init__(self, url=None, api_key=None):
        self.url, self.key = require(url or settings.get(f"{self.service}_url"), api_key or settings.get(f"{self.service}_api_key"), self.label)

    @property
    def headers(self):
        return {"X-Api-Key": self.key, "Content-Type": "application/json"}

    def _get(self, event, path, **kwargs):
        res = http.request(self.service, event, "GET", f"{self.url}{path}", headers=self.headers, **kwargs)
        return check(res, self.label, event).json()

    def _send(self, event, method, path, body=None, ok=(200, 201, 202)):
        res = http.request(self.service, event, method, f"{self.url}{path}", headers=self.headers, json=body)
        if res.status_code not in ok:
            raise ApiError(f"{self.label} {event} failed: HTTP {res.status_code} {res.text[:200]}")
        try:
            return res.json()
        except ValueError:
            return {}

    def queue(self, page_size=1000):
        """Every record of the download queue."""
        records, page = [], 1
        while True:
            body = self._get("GetQueue", "/api/v3/queue", params={"page": page, "pageSize": page_size})
            batch = body.get("records") or []
            records.extend(batch)
            if not batch or len(records) >= (body.get("totalRecords") or 0):
                return records
            page += 1

    def quality_profiles(self):
        return self._get("GetQualityProfiles", "/api/v3/qualityprofile")

    def root_folders(self):
        return self._get("GetRootFolders", "/api/v3/rootfolder")

    def status(self):
        return self._get("SystemStatus", "/api/v3/system/status", timeout=8)


class RadarrClient(_Arr):
    service, label = "radarr", "Radarr"

    def movies(self):
        return self._get("GetLibrary", "/api/v3/movie", timeout=60)

    def lookup(self, tmdb_id):
        results = self._get("LookupMovie", "/api/v3/movie/lookup", params={"term": f"tmdb:{tmdb_id}"})
        return results[0] if results else None

    def add_movie(self, movie, quality_profile_id, root_folder):
        payload = {"title": movie["title"], "titleSlug": movie["titleSlug"], "tmdbId": movie["tmdbId"], "year": movie.get("year"),
                   "images": movie.get("images", []), "qualityProfileId": int(quality_profile_id), "rootFolderPath": root_folder,
                   "monitored": True, "addOptions": {"searchForMovie": True}}
        return self._send("AddMovie", "POST", "/api/v3/movie", payload)


class SonarrClient(_Arr):
    service, label = "sonarr", "Sonarr"

    def series(self):
        return self._get("GetLibrary", "/api/v3/series", timeout=60)

    def episodes(self, series_id):
        return self._get("GetEpisodes", "/api/v3/episode", params={"seriesId": series_id}, timeout=60)

    def get_series(self, sonarr_id):
        return self._get("GetSeries", f"/api/v3/series/{sonarr_id}")

    def lookup(self, tvdb_id):
        results = self._get("LookupSeries", "/api/v3/series/lookup", params={"term": f"tvdb:{tvdb_id}"})
        return results[0] if results else None

    def add_series(self, show, quality_profile_id, root_folder, monitored_seasons=None, search=True):
        """monitored_seasons: TVDB season numbers to monitor; None leaves Sonarr's default (everything)."""
        seasons = show.get("seasons", [])
        if monitored_seasons is not None:
            wanted = {int(s) for s in monitored_seasons}
            for s in seasons:
                s["monitored"] = s.get("seasonNumber") in wanted
        payload = {"title": show["title"], "titleSlug": show["titleSlug"], "tvdbId": show["tvdbId"], "year": show.get("year"),
                   "images": show.get("images", []), "seasons": seasons, "qualityProfileId": int(quality_profile_id),
                   "rootFolderPath": root_folder, "monitored": True,
                   "addOptions": {"searchForMissingEpisodes": bool(search), "ignoreEpisodesWithFiles": True}}
        return self._send("AddSeries", "POST", "/api/v3/series", payload)

    def update_series(self, sonarr_id, payload):
        return self._send("UpdateSeries", "PUT", f"/api/v3/series/{sonarr_id}", payload)

    def monitor_episodes(self, episode_ids, monitored=True):
        return self._send("MonitorEpisodes", "PUT", "/api/v3/episode/monitor", {"episodeIds": [int(i) for i in episode_ids], "monitored": bool(monitored)})

    def search_episodes(self, episode_ids):
        return self._send("SearchEpisodes", "POST", "/api/v3/command", {"name": "EpisodeSearch", "episodeIds": [int(i) for i in episode_ids]})

    def search_season(self, sonarr_id, season_number):
        return self._send("SearchSeason", "POST", "/api/v3/command", {"name": "SeasonSearch", "seriesId": int(sonarr_id), "seasonNumber": int(season_number)})
