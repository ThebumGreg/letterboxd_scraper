from synchub import metadata

from .fakes import FakeTmdb


def make_show(conn, tmdb_id="100", title="Show"):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', ?, ?)", (title, tmdb_id)).lastrowid


SHOW = {"id": 100, "name": "Test Show", "original_name": "Test Show", "first_air_date": "2010-09-01", "status": "Returning Series",
        "overview": "An overview.", "poster_path": "/p.jpg", "external_ids": {"tvdb_id": 5555, "imdb_id": "tt1"},
        "seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": 2}]}
SEASONS = {100: {
    0: {"episodes": [{"id": 1, "episode_number": 1, "name": "Making of", "air_date": "2010-08-01", "runtime": 10}]},
    1: {"episodes": [{"id": 2, "episode_number": 1, "name": "Pilot", "air_date": "2010-09-01", "runtime": 45},
                     {"id": 3, "episode_number": 2, "name": "Two", "air_date": "2010-09-08", "runtime": 45}]},
    2: {"episodes": [{"id": 4, "episode_number": 1, "name": "Future", "air_date": "2099-01-01"}]},       # not aired yet
}}


def tmdb_for_show():
    return FakeTmdb(shows={"100": SHOW}, seasons={"100": SEASONS[100]})


def test_refresh_show_stores_every_episode_including_specials_and_unaired(conn):
    mid = make_show(conn)
    result = metadata.refresh_show(conn, tmdb_for_show(), mid, "2026-01-01 00:00:00")
    assert result == {"found": True, "episodes": 4, "seasons": 3}
    eps = [(r["season_number"], r["episode_number"], r["title"], r["air_date"]) for r in conn.execute("SELECT * FROM episode ORDER BY season_number, episode_number")]
    assert eps == [(0, 1, "Making of", "2010-08-01"), (1, 1, "Pilot", "2010-09-01"), (1, 2, "Two", "2010-09-08"), (2, 1, "Future", "2099-01-01")]
    show = conn.execute("SELECT * FROM media WHERE id = ?", (mid,)).fetchone()
    assert (show["title"], show["year"], show["tvdb_id"], show["imdb_id"], show["status"], show["poster_path"]) == ("Test Show", 2010, "5555", "tt1", "Returning Series", "/p.jpg")


def test_refresh_is_idempotent_and_never_deletes_episodes(conn):
    mid = make_show(conn)
    metadata.refresh_show(conn, tmdb_for_show(), mid, "2026-01-01 00:00:00")
    trimmed = FakeTmdb(shows={"100": SHOW}, seasons={"100": {1: {"episodes": [SEASONS[100][1]["episodes"][0]]}}})
    metadata.refresh_show(conn, trimmed, mid, "2026-01-02 00:00:00")
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 4      # TMDB dropping episodes does not remove them
    metadata.refresh_show(conn, tmdb_for_show(), mid, "2026-01-03 00:00:00")
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 4


def test_tvdb_id_is_not_taken_from_another_show(conn):
    other = conn.execute("INSERT INTO media (kind, title, tmdb_id, tvdb_id) VALUES ('show', 'Other', '999', '5555')").lastrowid
    mid = make_show(conn)
    metadata.refresh_show(conn, tmdb_for_show(), mid)
    assert conn.execute("SELECT tvdb_id FROM media WHERE id = ?", (mid,)).fetchone()[0] is None
    assert conn.execute("SELECT tvdb_id FROM media WHERE id = ?", (other,)).fetchone()[0] == "5555"


def test_unknown_show_is_reported_not_created(conn):
    mid = make_show(conn, tmdb_id="404")
    assert metadata.refresh_show(conn, FakeTmdb(), mid) == {"found": False, "episodes": 0}


def test_refresh_movie(conn):
    mid = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'x', '949')").lastrowid
    tmdb = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "original_title": "Heat", "release_date": "1995-12-15", "imdb_id": "tt0113277", "overview": "o", "poster_path": "/h.jpg"}})
    assert metadata.refresh_movie(conn, tmdb, mid)
    row = conn.execute("SELECT * FROM media WHERE id = ?", (mid,)).fetchone()
    assert (row["title"], row["year"], row["imdb_id"]) == ("Heat", 1995, "tt0113277")


def test_needs_refresh_policy(conn):
    def row(kind="show", status="Returning Series", refreshed=None):
        return {"kind": kind, "status": status, "metadata_refreshed_at": refreshed}
    now = "2026-02-01 12:00:00"
    assert metadata.needs_refresh(row(), now)                                                   # never refreshed
    assert not metadata.needs_refresh(row(refreshed="2026-02-01 06:00:00"), now)                # airing, 6h ago (default 24h)
    assert metadata.needs_refresh(row(refreshed="2026-01-30 12:00:00"), now)                    # airing, 2 days ago
    assert not metadata.needs_refresh(row(status="Ended", refreshed="2026-01-20 12:00:00"), now)  # ended: only monthly
    assert metadata.needs_refresh(row(status="Ended", refreshed="2025-12-01 12:00:00"), now)


def test_refresh_pending_continues_after_errors_and_respects_limit(conn):
    good = make_show(conn, "100")
    bad = make_show(conn, "666", "Bad")
    movie = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'm', '1')").lastrowid

    class Flaky(FakeTmdb):
        def show(self, tmdb_id):
            if str(tmdb_id) == "666":
                raise RuntimeError("HTTP 500")
            return super().show(tmdb_id)

    tmdb = Flaky(shows={"100": SHOW}, seasons={"100": SEASONS[100]}, movies={"1": {"id": 1, "title": "M", "release_date": "2000-01-01"}})
    stats = metadata.refresh_pending(conn, tmdb, now="2026-01-01 00:00:00")
    assert stats == {"movies": 1, "shows": 1, "episodes": 4, "errors": 1}
    assert conn.execute("SELECT metadata_refreshed_at FROM media WHERE id = ?", (bad,)).fetchone()[0] is None     # retried next run
    assert metadata.refresh_pending(conn, tmdb, now="2026-01-01 01:00:00")["shows"] == 0                           # fresh items skipped


def test_refresh_pending_honours_the_limit(conn):
    for i in range(3):
        conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'm', ?)", (str(i),))
    tmdb = FakeTmdb(movies={str(i): {"id": i, "title": f"M{i}", "release_date": "2000-01-01"} for i in range(3)})
    assert metadata.refresh_pending(conn, tmdb, limit=2, now="2026-01-01 00:00:00")["movies"] == 2
    assert metadata.refresh_pending(conn, tmdb, limit=2, now="2026-01-01 00:00:00")["movies"] == 1      # the remaining one next time
