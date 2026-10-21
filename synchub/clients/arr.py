from synchub import http, settings
from synchub.clients import check, require


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


class RadarrClient(_Arr):
    service, label = "radarr", "Radarr"

    def movies(self):
        return self._get("GetLibrary", "/api/v3/movie", timeout=60)


class SonarrClient(_Arr):
    service, label = "sonarr", "Sonarr"

    def series(self):
        return self._get("GetLibrary", "/api/v3/series", timeout=60)

    def episodes(self, series_id):
        return self._get("GetEpisodes", "/api/v3/episode", params={"seriesId": series_id}, timeout=60)
