"""A scriptable stand-in for TmdbClient."""


class FakeTmdb:
    def __init__(self, movies=None, shows=None, seasons=None, find=None, search=None, fail=False):
        self.movies, self.shows, self.season_data = movies or {}, shows or {}, seasons or {}
        self.find_data, self.search_data, self.fail = find or {}, search or {}, fail
        self.calls = []

    def _check(self, name, *args):
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
