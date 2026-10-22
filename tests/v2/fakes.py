"""Scriptable stand-ins for the services SyncHub talks to."""


class ServarrSetup:
    """What a real Radarr/Sonarr answers about its own setup. The defaults satisfy SyncHub's default settings."""
    service, label = "radarr", "Radarr"
    profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
    folders = [{"path": "/movies", "accessible": True, "freeSpace": 10 ** 11}, {"path": "/films", "accessible": True, "freeSpace": 10 ** 11}]

    def quality_profiles(self):
        return self.profiles

    def root_folders(self):
        return self.folders


class SonarrSetup(ServarrSetup):
    service, label = "sonarr", "Sonarr"
    folders = [{"path": "/tv", "accessible": True, "freeSpace": 10 ** 11}, {"path": "/shows", "accessible": True, "freeSpace": 10 ** 11}]


# ---------------------------------------------------------------- TMDB
class FakeTmdb:
    def __init__(self, movies=None, shows=None, seasons=None, find=None, search=None, fail=False):
        self.movies, self.shows, self.season_data = movies or {}, shows or {}, seasons or {}
        self.find_data, self.search_data, self.fail = find or {}, search or {}, fail
        self.calls = []

    def _check(self, name, *args):
        from synchub import db
        db.assert_not_in_write_transaction(f"fake TMDB {name}")
        self.calls.append((name, *args))
        if self.fail:
            raise RuntimeError("TMDB down")

    def movie(self, tmdb_id):
        self._check("movie", tmdb_id)
        return self.movies.get(str(tmdb_id))

    def show(self, tmdb_id):
        self._check("show", tmdb_id)
        return self.shows.get(str(tmdb_id))

    def seasons(self, tmdb_id, numbers):
        self._check("seasons", tmdb_id, tuple(numbers))
        data = self.season_data.get(str(tmdb_id), {})
        return {n: data[n] for n in numbers if n in data}

    def find(self, external_id, source):
        self._check("find", external_id, source)
        return self.find_data.get((str(external_id), source), {})

    def search_movie(self, title, year=None):
        self._check("search_movie", title, year)
        return self.search_data.get(("movie", title), [])

    def search_tv(self, title, year=None):
        self._check("search_tv", title, year)
        return self.search_data.get(("tv", title), [])
