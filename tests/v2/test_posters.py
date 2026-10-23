import os
import time

import pytest

from synchub import metadata, posters, settings, tasks

from .fakes import FakeTmdb


class Resp:
    def __init__(self, status=200, content=b"x" * 5000):
        self.status_code, self.content = status, content


@pytest.fixture
def poster_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(posters, "POSTER_DIR_OVERRIDE", tmp_path / "posters")
    return tmp_path / "posters"


def make(conn, poster_path="/p.jpg"):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id, poster_path) VALUES ('movie', 'M', '1', ?)", (poster_path,)).lastrowid


def test_downloads_from_tmdb_once_and_caches(conn, poster_dir, monkeypatch):
    mid = make(conn)
    calls = []
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: calls.append(url) or Resp())
    path = posters.ensure_poster(conn, mid)
    assert path == poster_dir / f"v2-{mid}.jpg" and path.stat().st_size == 5000
    assert calls == ["https://image.tmdb.org/t/p/w342/p.jpg"]
    posters.ensure_poster(conn, mid)
    assert len(calls) == 1


def test_falls_back_to_jellyfin_when_tmdb_has_no_poster(conn, poster_dir, monkeypatch):
    mid = make(conn, poster_path=None)
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES ('jf1', 'movie', 'a', 'a')")
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', 'jf1', 'x', 1.0)", (mid,))
    settings.set_value("jellyfin_url", "http://jf:8096", conn=conn); settings.set_value("jellyfin_api_key", "K", conn=conn)
    conn.commit()
    seen = []
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: seen.append((url, kw.get("headers"))) or Resp())
    assert posters.ensure_poster(conn, mid) is not None
    assert seen == [("http://jf:8096/Items/jf1/Images/Primary", {"X-Emby-Token": "K"})]


def test_failures_are_remembered_for_a_day_not_forever(conn, poster_dir, monkeypatch):
    mid = make(conn)
    calls = []
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: calls.append(url) or Resp(404, b""))
    assert posters.ensure_poster(conn, mid) is None and posters.ensure_poster(conn, mid) is None
    assert len(calls) == 1                                                     # second call skipped by the marker
    marker = poster_dir / f"v2-{mid}.missing"
    old = time.time() - 2 * 24 * 3600
    os.utime(marker, (old, old))
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: calls.append(url) or Resp())
    assert posters.ensure_poster(conn, mid) is not None and not marker.exists()


def test_tiny_responses_are_not_saved_and_unknown_media_is_none(conn, poster_dir, monkeypatch):
    mid = make(conn)
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: Resp(200, b"tiny"))
    assert posters.ensure_poster(conn, mid) is None
    assert posters.ensure_poster(conn, 9999) is None


def _fail(poster_dir, mid, age_seconds=60):
    poster_dir.mkdir(parents=True, exist_ok=True)
    marker = poster_dir / f"v2-{mid}.missing"
    marker.touch()
    stamp = time.time() - age_seconds
    os.utime(marker, (stamp, stamp))
    return marker


def test_a_remembered_failure_is_dropped_once_the_title_was_re_read_from_tmdb(conn, poster_dir, monkeypatch):
    mid = make(conn)
    marker = _fail(poster_dir, mid, age_seconds=3600)                     # failed an hour ago
    calls = []
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: calls.append(url) or Resp())
    conn.execute("UPDATE media SET metadata_refreshed_at = ? WHERE id = ?", (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 7200)), mid))
    assert posters.ensure_poster(conn, mid) is None and calls == []       # refreshed before it failed: the failure stands
    conn.execute("UPDATE media SET metadata_refreshed_at = ? WHERE id = ?", (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 600)), mid))
    assert posters.ensure_poster(conn, mid) is not None and len(calls) == 1 and not marker.exists()      # refreshed after: retried now, not tomorrow


def test_clear_failures_removes_only_failure_notes(poster_dir):
    poster_dir.mkdir(parents=True)
    (poster_dir / "v2-1.jpg").write_bytes(b"x" * 5000)
    (poster_dir / "old-slug.missing").touch()                             # not ours: left alone
    for mid in (1, 2, 3):
        _fail(poster_dir, mid)
    assert posters.clear_failures() == 3
    assert sorted(f.name for f in poster_dir.iterdir()) == ["old-slug.missing", "v2-1.jpg"]
    assert posters.clear_failures() == 0


def test_refresh_all_downloads_what_is_missing_and_names_what_has_no_poster(conn, poster_dir, monkeypatch):
    have, fetchable, nothing = (conn.execute("INSERT INTO media (kind, title, year, tmdb_id, poster_path) VALUES ('movie', ?, ?, ?, ?)", row).lastrowid
                                for row in (("Have", 2000, "1", "/p.jpg"), ("Fetchable", 2001, "2", "/p.jpg"), ("Nothing", 1999, "3", None)))
    poster_dir.mkdir(parents=True)
    (poster_dir / f"v2-{have}.jpg").write_bytes(b"x" * 5000)
    _fail(poster_dir, fetchable, age_seconds=60)                          # a remembered failure does not stop a bulk retry...
    posters.clear_failures()                                              # ...once the caller has cleared it
    calls, said = [], []
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: calls.append(url) or Resp())
    stats = posters.refresh_all(conn, progress=lambda message, force=False: said.append(message))
    assert (stats["cached"], stats["downloaded"], stats["none"]) == (1, 1, 1) and stats["none_titles"] == ["Nothing (1999)"]
    assert len(calls) == 1 and any("2 of 3" in m or "3 of 3" in m for m in said)


def test_fill_missing_poster_paths_reads_only_what_is_missing_and_isolates_failures(conn):
    movie = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Movie', '1')").lastrowid
    show = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'Show', '2')").lastrowid
    bare = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Bare', '3')").lastrowid
    has = conn.execute("INSERT INTO media (kind, title, tmdb_id, poster_path) VALUES ('movie', 'Has', '4', '/keep.jpg')").lastrowid
    boom = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Boom', '5')").lastrowid
    conn.commit()

    class Tmdb(FakeTmdb):
        def movie(self, tmdb_id):
            if str(tmdb_id) == "5":
                raise RuntimeError("TMDB hiccup")
            return super().movie(tmdb_id)

    tmdb = Tmdb(movies={"1": {"poster_path": "/m.jpg"}, "3": {"title": "no poster"}, "4": {"poster_path": "/other.jpg"}}, shows={"2": {"poster_path": "/s.jpg"}})
    stats = metadata.fill_missing_poster_paths(conn, tmdb)
    assert stats == {"checked": 4, "found": 2, "none": 1, "errors": 1}
    got = {r["id"]: (r["poster_path"], r["metadata_refreshed_at"]) for r in conn.execute("SELECT * FROM media")}
    assert got[movie] == ("/m.jpg", None) and got[show] == ("/s.jpg", None)       # only the poster changed: not marked as a full refresh
    assert got[bare][0] is None and got[boom][0] is None and got[has][0] == "/keep.jpg"
    assert ("movie", "4") not in tmdb.calls and [c[0] for c in tmdb.calls].count("seasons") == 0


def test_refresh_posters_task_clears_failures_fills_paths_and_downloads(v2, poster_dir, monkeypatch):
    with v2.connection() as c:
        mid = c.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('movie', 'Missing One', 2020, '1')").lastrowid
        none = c.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('movie', 'Hopeless', 2021, '2')").lastrowid
    old_failure = _fail(poster_dir, mid, age_seconds=60)
    monkeypatch.setattr(tasks, "TmdbClient", lambda: FakeTmdb(movies={"1": {"poster_path": "/one.jpg"}, "2": {"title": "no poster"}}))
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: Resp() if url.endswith("/one.jpg") else Resp(404, b""))
    message = tasks.refresh_posters()
    assert "1 downloaded" in message and "1 not available" in message and "1 found, 1 have none" in message
    assert (poster_dir / f"v2-{mid}.jpg").exists() and not old_failure.exists()
    assert (poster_dir / f"v2-{none}.missing").exists()                          # the hopeless one is remembered again, for a day
    with v2.connection() as c:
        assert c.execute("SELECT status FROM task_status WHERE task_name = 'posters'").fetchone()[0] == "completed"


def test_refresh_posters_task_without_a_tmdb_key_still_uses_jellyfin(v2, poster_dir, monkeypatch):
    from synchub.clients import ConfigError

    def no_key():
        raise ConfigError("TMDB API key is not configured.")
    monkeypatch.setattr(tasks, "TmdbClient", no_key)
    with v2.connection() as c:
        c.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'M', '1')")
    assert "TMDB is not configured" in tasks.refresh_posters()
