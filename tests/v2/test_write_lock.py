"""Slow network calls must never happen while this process holds the database write lock."""
import sqlite3

import pytest

from synchub import db, tasks
from synchub.ingest import letterboxd_ids, sonarr
from synchub.matching import Matcher

from .fakes import FakeTmdb
from .test_ingest_arr import FakeSonarr
from .test_letterboxd_ids import entry, film_page

NOW = "2026-01-01 00:00:00"


@pytest.fixture
def quick_lock_timeout(monkeypatch, v2):
    """Fail fast (0.2 s) instead of waiting 30 s when another connection holds the write lock."""
    real = sqlite3.connect

    def connect():
        conn = real(v2.DB_FILE, timeout=0.2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    monkeypatch.setattr(db, "connect", connect)


def write_from_another_connection():
    """What a web request or another task would do at that moment."""
    tasks.set_status("probe", "in_progress", "other thread was able to write")


def test_matcher_does_not_hold_the_lock_during_tmdb_calls(v2, quick_lock_timeout):
    class Probing(FakeTmdb):
        def find(self, external_id, source):
            write_from_another_connection()
            return super().find(external_id, source)

    with v2.connection() as conn:
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, tmdb_id, first_seen, last_seen) VALUES ('j1', 'movie', 'Heat', 1995, '949', ?, ?)", (NOW, NOW))
        conn.execute("INSERT INTO src_sonarr_series (sonarr_id, tvdb_id, title, year, first_seen, last_seen) VALUES ('s1', '77996', 'Daria', 1997, ?, ?)", (NOW, NOW))
    with v2.connection() as conn:
        Matcher(conn, Probing(find={("77996", "tvdb_id"): {"tv_results": [{"id": 2283, "name": "Daria", "first_air_date": "1997-03-03"}]}}), NOW).resolve_all()
    with v2.connection() as conn:
        assert conn.execute("SELECT message FROM task_status WHERE task_name = 'probe'").fetchone()[0] == "other thread was able to write"
        assert conn.execute("SELECT COUNT(*) FROM link").fetchone()[0] == 2


def test_letterboxd_resolver_does_not_hold_the_lock_while_paused_between_pages(v2, quick_lock_timeout):
    with v2.connection() as conn:
        entry(conn, "a"); entry(conn, "b")
    pauses = []

    def pause():
        write_from_another_connection()
        pauses.append(1)

    def fetch(url):
        return 200, film_page("949" if url.endswith("/a/") else "950")

    with v2.connection() as conn:
        stats = letterboxd_ids.resolve(conn, fetch, pause=pause, now=NOW)
    assert stats["found"] == 2 and pauses == [1]


def test_sonarr_sync_does_not_hold_the_lock_while_fetching_episodes(v2, quick_lock_timeout):
    class Probing(FakeSonarr):
        def episodes(self, series_id):
            write_from_another_connection()
            return super().episodes(series_id)

    client = Probing([{"id": 10, "tvdbId": 1, "title": "S"}, {"id": 11, "tvdbId": 2, "title": "T"}],
                     {10: [{"id": 1, "seasonNumber": 1, "episodeNumber": 1}], 11: [{"id": 2, "seasonNumber": 1, "episodeNumber": 1}]})
    with v2.connection() as conn:
        result = sonarr.sync_library(conn, client, NOW)
    assert result["episodes"] == 2


def test_the_probe_really_detects_a_held_lock(v2, quick_lock_timeout):
    with v2.connection() as conn:
        conn.execute("INSERT INTO app_setting (key, value) VALUES ('x', 'y')")      # holds the write lock until the block ends
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            write_from_another_connection()
