import os
import time

import pytest

from synchub import posters, settings


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
