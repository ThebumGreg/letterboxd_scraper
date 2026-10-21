import pytest

from synchub.ingest import radarr, sonarr


class FakeRadarr:
    def __init__(self, movies, queue=()):
        self._movies, self._queue = movies, list(queue)

    def movies(self): return self._movies
    def queue(self): return self._queue


class FakeSonarr:
    def __init__(self, series, episodes, queue=(), fail=()):
        self._series, self._episodes, self._queue, self.fail = series, episodes, list(queue), set(fail)

    def series(self): return self._series
    def queue(self): return self._queue

    def episodes(self, series_id):
        if series_id in self.fail:
            raise RuntimeError("HTTP 500")
        return self._episodes.get(series_id, [])


def q(conn, table, key):
    return {r[key]: dict(r) for r in conn.execute(f"SELECT * FROM {table}")}


def test_radarr_library_upsert_and_removal(conn):
    movies = [{"id": 1, "tmdbId": 949, "imdbId": "tt0113277", "title": "Heat", "year": 1995, "monitored": True, "hasFile": True},
              {"id": 2, "tmdbId": 0, "title": "No id", "year": 0, "monitored": False, "hasFile": False}]
    radarr.sync_library(conn, FakeRadarr(movies), now="2026-01-01 00:00:00")
    got = q(conn, "src_radarr_movie", "radarr_id")
    assert (got["1"]["tmdb_id"], got["1"]["has_file"], got["1"]["monitored"]) == ("949", 1, 1)
    assert got["2"]["tmdb_id"] is None and got["2"]["year"] is None

    movies[0]["hasFile"] = False
    result = radarr.sync_library(conn, FakeRadarr(movies[:1]), now="2026-01-02 00:00:00")
    got = q(conn, "src_radarr_movie", "radarr_id")
    assert result["removed"] == 1 and got["2"]["removed_at"] and got["1"]["has_file"] == 0 and got["1"]["removed_at"] is None


def test_sonarr_library_with_episodes(conn):
    series = [{"id": 10, "tvdbId": 269533, "tmdbId": 60802, "title": "The Last Ship", "year": 2014, "monitored": True}]
    eps = {10: [{"id": 100, "seasonNumber": 3, "episodeNumber": 10, "tvdbId": 5648401, "title": "Scuttle", "airDate": "2016-08-14", "monitored": True, "hasFile": True},
                {"id": 101, "seasonNumber": 0, "episodeNumber": 1, "title": "Special", "monitored": False, "hasFile": False}]}
    result = sonarr.sync_library(conn, FakeSonarr(series, eps), now="2026-01-01 00:00:00")
    assert result["series"] == 1 and result["episodes"] == 2
    ep = q(conn, "src_sonarr_episode", "sonarr_episode_id")
    assert (ep["100"]["sonarr_id"], ep["100"]["tvdb_episode_id"], ep["100"]["has_file"], ep["100"]["air_date"]) == ("10", "5648401", 1, "2016-08-14")
    assert ep["101"]["season_number"] == 0 and ep["101"]["tvdb_episode_id"] is None
    assert q(conn, "src_sonarr_series", "sonarr_id")["10"]["tmdb_id"] == "60802"


def test_sonarr_episode_removed_when_it_disappears_from_a_series(conn):
    series = [{"id": 10, "tvdbId": 1, "title": "S"}]
    full = {10: [{"id": 1, "seasonNumber": 1, "episodeNumber": 1}, {"id": 2, "seasonNumber": 1, "episodeNumber": 2}]}
    sonarr.sync_library(conn, FakeSonarr(series, full), now="2026-01-01 00:00:00")
    sonarr.sync_library(conn, FakeSonarr(series, {10: full[10][:1]}), now="2026-01-02 00:00:00")
    ep = q(conn, "src_sonarr_episode", "sonarr_episode_id")
    assert ep["1"]["removed_at"] is None and ep["2"]["removed_at"] == "2026-01-02 00:00:00"


def test_sonarr_episode_fetch_failure_keeps_old_episodes_and_is_reported(conn):
    series = [{"id": 10, "tvdbId": 1, "title": "S"}, {"id": 11, "tvdbId": 2, "title": "T"}]
    eps = {10: [{"id": 1, "seasonNumber": 1, "episodeNumber": 1}], 11: [{"id": 5, "seasonNumber": 1, "episodeNumber": 1}]}
    sonarr.sync_library(conn, FakeSonarr(series, eps), now="2026-01-01 00:00:00")
    result = sonarr.sync_library(conn, FakeSonarr(series, eps, fail={11}), now="2026-01-02 00:00:00")
    assert result["episode_fetch_errors"] == ["11"]
    assert q(conn, "src_sonarr_episode", "sonarr_episode_id")["5"]["removed_at"] is None


def test_removed_series_takes_its_episodes_with_it(conn):
    series = [{"id": 10, "tvdbId": 1, "title": "S"}, {"id": 11, "tvdbId": 2, "title": "T"}]
    eps = {10: [{"id": 1, "seasonNumber": 1, "episodeNumber": 1}], 11: [{"id": 5, "seasonNumber": 1, "episodeNumber": 1}]}
    sonarr.sync_library(conn, FakeSonarr(series, eps), now="2026-01-01 00:00:00")
    sonarr.sync_library(conn, FakeSonarr(series[:1], eps), now="2026-01-02 00:00:00")
    assert q(conn, "src_sonarr_series", "sonarr_id")["11"]["removed_at"]
    assert q(conn, "src_sonarr_episode", "sonarr_episode_id")["5"]["removed_at"] == "2026-01-02 00:00:00"
    assert q(conn, "src_sonarr_episode", "sonarr_episode_id")["1"]["removed_at"] is None


def test_queue_progress_and_target_mapping(conn):
    rq = [{"id": 7, "movieId": 1, "title": "Heat", "size": 1000, "sizeleft": 250, "trackedDownloadState": "downloading", "timeleft": "00:10:00"},
          {"id": 8, "title": "orphan record without a movie"}]
    radarr.sync_queue(conn, FakeRadarr([], rq), now="2026-01-01 00:00:00")
    sq = [{"id": 7, "episodeId": 100, "title": "Ep", "size": 0, "sizeleft": 0, "status": "queued"}]
    sonarr.sync_queue(conn, FakeSonarr([], {}, sq), now="2026-01-01 00:00:00")
    rows = [dict(r) for r in conn.execute("SELECT * FROM src_arr_queue ORDER BY service")]
    assert [(r["service"], r["queue_id"], r["kind"], r["target_id"], r["progress"], r["status"]) for r in rows] == [
        ("radarr", "7", "movie", "1", 0.75, "downloading"), ("sonarr", "7", "episode", "100", None, "queued")]


def test_queue_clears_when_empty_and_services_do_not_interfere(conn):
    radarr.sync_queue(conn, FakeRadarr([], [{"id": 1, "movieId": 1, "size": 10, "sizeleft": 5}]), now="2026-01-01 00:00:00")
    sonarr.sync_queue(conn, FakeSonarr([], {}, [{"id": 1, "episodeId": 9, "size": 10, "sizeleft": 5}]), now="2026-01-01 00:00:00")
    radarr.sync_queue(conn, FakeRadarr([], []), now="2026-01-02 00:00:00")      # an empty queue is normal, not a failure
    got = {r["service"]: r["removed_at"] for r in conn.execute("SELECT service, removed_at FROM src_arr_queue")}
    assert got == {"radarr": "2026-01-02 00:00:00", "sonarr": None}


def test_queue_item_that_returns_is_reopened(conn):
    rec = [{"id": 1, "movieId": 1, "size": 10, "sizeleft": 5}]
    radarr.sync_queue(conn, FakeRadarr([], rec), now="2026-01-01 00:00:00")
    radarr.sync_queue(conn, FakeRadarr([], []), now="2026-01-02 00:00:00")
    radarr.sync_queue(conn, FakeRadarr([], rec), now="2026-01-03 00:00:00")
    assert conn.execute("SELECT removed_at FROM src_arr_queue").fetchone()[0] is None
