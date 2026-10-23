import pytest

from synchub import catalog, search, settings
from synchub.automation import RequestError
from synchub.clients import ApiError

from . import test_end_to_end as e2e
from .fakes import FakeTmdb, ServarrSetup, SonarrSetup
from .test_states import link, radarr as radarr_row


def movie_hit(id_, title, year="1995-12-15", **extra):
    return {"id": id_, "title": title, "release_date": year, "overview": f"About {title}.", "poster_path": f"/{id_}.jpg", "vote_average": 7.84, **extra}


def show_hit(id_, name, year="2014-06-22", **extra):
    return {"id": id_, "name": name, "first_air_date": year, "overview": f"About {name}.", "poster_path": None, "vote_average": 0, **extra}


class FakeRadarr(ServarrSetup):
    def __init__(self, reject=None):
        self.added, self.reject = [], reject

    def lookup(self, tmdb_id):
        return {"title": f"Radarr {tmdb_id}", "titleSlug": "x", "tmdbId": int(tmdb_id)}

    def add_movie(self, movie, quality_profile_id, root_folder):
        if self.reject:
            raise ApiError(self.reject)
        self.added.append((movie["tmdbId"], quality_profile_id, root_folder))


class FakeSonarr(SonarrSetup):
    def __init__(self, reject=None):
        self.added, self.reject = [], reject

    def lookup(self, tvdb_id):
        return {"title": "Sonarr show", "titleSlug": "s", "tvdbId": int(tvdb_id), "seasons": [{"seasonNumber": n, "monitored": True} for n in (0, 1, 3)]}

    def add_series(self, show, qp, root, monitored_seasons=None, search=True):
        if self.reject:
            raise ApiError(self.reject)
        self.added.append((show["tvdbId"], monitored_seasons, qp, root))


# ---------------------------------------------------------------- query and results
@pytest.mark.parametrize("text,expected", [
    ("Heat 1995", ("Heat", 1995)), ("Heat (1995)", ("Heat", 1995)), ("  Heat   (1995) ", ("Heat", 1995)), ("The Last Ship 2014", ("The Last Ship", 2014)),
    ("Heat", ("Heat", None)), ("1917", ("1917", None)), ("2001: A Space Odyssey", ("2001: A Space Odyssey", None)), ("Se7en 1995", ("Se7en", 1995)),
    ("Heat 1895", ("Heat 1895", None)), ("Heat 12345", ("Heat 12345", None)),
])
def test_a_trailing_year_narrows_the_search(text, expected):
    assert search.parse_query(text) == expected


def test_find_returns_movies_then_shows_with_what_the_page_needs():
    tmdb = FakeTmdb(search={("movie", "heat"): [movie_hit(949, "Heat"), {"title": "No id"}], ("tv", "heat"): [show_hit(1, "Heat Wave")]})
    movies_and_shows = search.find(tmdb, "  heat ")
    assert [(r["kind"], r["tmdb_id"], r["title"], r["year"]) for r in movies_and_shows] == [("movie", "949", "Heat", 1995), ("show", "1", "Heat Wave", 2014)]
    first = movies_and_shows[0]
    assert first["poster"] == "https://image.tmdb.org/t/p/w185/949.jpg" and first["rating"] == 7.8 and first["overview"] == "About Heat."
    assert movies_and_shows[1]["poster"] is None and movies_and_shows[1]["rating"] is None
    assert [r["kind"] for r in search.find(tmdb, "heat", "movie")] == ["movie"] and [r["kind"] for r in search.find(tmdb, "heat", "show")] == ["show"]
    assert [r["kind"] for r in search.find(tmdb, "heat", "nonsense")] == ["movie", "show"]
    calls = len(tmdb.calls)
    assert search.find(tmdb, "   ") == [] and len(tmdb.calls) == calls                      # nothing typed: TMDB is not asked


def test_a_year_is_passed_to_tmdb_and_a_title_ending_in_a_number_is_retried_whole():
    tmdb = FakeTmdb(search={("movie", "Heat", 1995): [movie_hit(949, "Heat")], ("movie", "Blade Runner 2049"): [movie_hit(335984, "Blade Runner 2049", "2017-10-04")]})
    assert [r["tmdb_id"] for r in search.find(tmdb, "Heat (1995)", "movie")] == ["949"]
    assert ("search_movie", "Heat", 1995) in tmdb.calls
    assert [r["tmdb_id"] for r in search.find(tmdb, "Blade Runner 2049", "movie")] == ["335984"]
    assert ("search_movie", "Blade Runner", 2049) in tmdb.calls and ("search_movie", "Blade Runner 2049", None) in tmdb.calls


def test_only_the_best_ten_of_each_kind_are_shown():
    tmdb = FakeTmdb(search={("movie", "x"): [movie_hit(i, f"M{i}") for i in range(1, 21)], ("tv", "x"): [show_hit(i, f"S{i}") for i in range(1, 21)]})
    found = search.find(tmdb, "x")
    assert len(found) == 20 and [r["tmdb_id"] for r in found[:10]] == [str(i) for i in range(1, 11)]


def test_results_know_where_each_title_stands_in_synchub(conn):
    queued = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Queued', '2')").lastrowid
    idle = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Idle in Radarr', '3')").lastrowid
    show_known = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'Known show', '10')").lastrowid
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'queued')", (queued,))
    radarr_row(conn, "7", monitored=0)
    link(conn, "media", idle, "radarr", "7")
    conn.commit()
    results = [{"kind": "movie", "tmdb_id": t} for t in ("1", "2", "3")] + [{"kind": "show", "tmdb_id": "10"}, {"kind": "show", "tmdb_id": "11"}]
    got = {(r["kind"], r["tmdb_id"]): r for r in search.annotate(conn, results)}
    assert got[("movie", "1")]["media_id"] is None and got[("movie", "1")]["addable"] and got[("movie", "1")]["state"] is None
    q = got[("movie", "2")]
    assert (q["state"], q["state_label"], q["addable"], q["url"]) == ("queued", "Queued", False, f"/movie/{queued}")
    assert got[("movie", "3")]["state"] == "missing" and not got[("movie", "3")]["addable"]            # in Radarr already: nothing to add
    assert got[("show", "10")]["addable"] and got[("show", "10")]["url"] == f"/show/{show_known}"       # a show can always have seasons requested
    assert got[("show", "11")]["media_id"] is None and got[("show", "11")]["addable"]


def test_season_choices_put_specials_last_and_say_whether_sonarr_can_take_it():
    details = {"name": "Show", "external_ids": {"tvdb_id": 5}, "seasons": [
        {"season_number": 2, "name": "Season 2", "episode_count": 8, "air_date": "2020-01-01"}, {"season_number": 0, "name": "Specials", "episode_count": 3},
        {"season_number": 1, "episode_count": 10, "air_date": "2019-01-01"}, {"name": "broken"}]}
    got = search.season_choices(FakeTmdb(shows={"9": details}), 9)
    assert got["title"] == "Show" and got["has_tvdb"] is True
    assert [(s["number"], s["name"], s["episodes"]) for s in got["seasons"]] == [(1, "Season 1", 10), (2, "Season 2", 8), (0, "Specials (Season 0)", 3)]
    no_tvdb = dict(details, external_ids={})
    assert search.season_choices(FakeTmdb(shows={"9": no_tvdb}), 9)["has_tvdb"] is False
    with pytest.raises(RequestError, match="does not know"):
        search.season_choices(FakeTmdb(), 404)


# ---------------------------------------------------------------- adding
def profile(conn, name="Alice", active=1):
    return conn.execute("INSERT INTO profile (name, is_active) VALUES (?, ?)", (name, active)).lastrowid


def test_adding_a_movie_sends_it_to_radarr_and_credits_the_profile(v2, conn):
    alice = profile(conn)
    conn.commit()
    tmdb, radarr = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15", "poster_path": "/h.jpg"}}), FakeRadarr()
    got = search.add_movie(tmdb, radarr, "949", alice)
    assert got["title"] == "Radarr 949" and radarr.added == [(949, 1, "/movies")]
    row = conn.execute("SELECT * FROM media WHERE id = ?", (got["media_id"],)).fetchone()
    assert (row["title"], row["year"], row["poster_path"]) == ("Heat", 1995, "/h.jpg") and row["metadata_refreshed_at"]
    assert catalog.demand(conn)[got["media_id"]]["wanted_by"] == ["Alice"]
    assert conn.execute("SELECT profile_id FROM manual_request WHERE media_id = ?", (got["media_id"],)).fetchone()[0] == alice


def test_a_guest_can_add_but_nobody_is_credited(v2, conn):
    tmdb = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15"}})
    got = search.add_movie(tmdb, FakeRadarr(), "949", None)
    assert conn.execute("SELECT COUNT(*) FROM manual_request").fetchone()[0] == 0 and got["media_id"] not in catalog.demand(conn)


def test_a_failed_add_leaves_nothing_behind(v2, conn):
    alice = profile(conn)
    conn.commit()
    tmdb = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15"}})
    with pytest.raises(RequestError, match="HTTP 400"):
        search.add_movie(tmdb, FakeRadarr(reject="Radarr AddMovie failed: HTTP 400 root folder"), "949", alice)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM manual_request").fetchone()[0] == 0


def test_a_title_that_was_already_known_survives_a_failed_add(v2, conn):
    existing = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Heat', '949')").lastrowid
    conn.commit()
    tmdb = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15"}})
    with pytest.raises(RequestError):
        search.add_movie(tmdb, FakeRadarr(reject="Radarr AddMovie failed: HTTP 400"), "949", None)
    assert conn.execute("SELECT id FROM media").fetchone()[0] == existing


def test_an_unknown_or_already_added_movie_is_explained(v2, conn):
    with pytest.raises(RequestError, match="does not know that movie"):
        search.add_movie(FakeTmdb(), FakeRadarr(), "1", None)
    first = search.add_movie(FakeTmdb(movies={"949": {"id": 949, "title": "Heat"}}), FakeRadarr(), "949", None)
    conn.execute("INSERT INTO src_radarr_movie (radarr_id, first_seen, last_seen) VALUES ('1', 'a', 'a')")
    link(conn, "media", first["media_id"], "radarr", "1")
    conn.commit()
    with pytest.raises(RequestError, match="already in Radarr"):
        search.add_movie(FakeTmdb(movies={"949": {"id": 949, "title": "Heat"}}), FakeRadarr(), "949", None)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1                      # the second attempt did not remove the first


def test_adding_a_movie_with_a_bad_radarr_setup_says_so_and_adds_nothing(v2, conn):
    settings.set_value("radarr_root_folder", "/gone", conn=conn)
    conn.commit()
    with pytest.raises(RequestError, match="root folder '/gone'"):
        search.add_movie(FakeTmdb(movies={"949": {"id": 949, "title": "Heat"}}), FakeRadarr(), "949", None)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def show_tmdb(**overrides):
    return FakeTmdb(shows={"60802": {**e2e.LAST_SHIP, **overrides}}, seasons={"60802": e2e.LAST_SHIP_SEASONS})


def test_adding_a_show_reads_every_episode_and_sends_the_chosen_seasons(v2, conn):
    alice = profile(conn)
    conn.commit()
    sonarr = FakeSonarr()
    got = search.add_show(show_tmdb(), sonarr, "60802", [1, 3], alice)
    assert got["title"] == "The Last Ship" and got["status"] == "added" and got["seasons"] == [1, 3]
    assert sonarr.added == [(269533, [1, 3], 1, "/tv")]
    counts = {r["season_number"]: r["n"] for r in conn.execute("SELECT season_number, COUNT(*) AS n FROM episode WHERE media_id = ? GROUP BY season_number", (got["media_id"],))}
    assert counts == {0: 1, 1: 3, 3: 4}                                                      # all seasons are stored, Specials included
    assert conn.execute("SELECT tvdb_id FROM media WHERE id = ?", (got["media_id"],)).fetchone()[0] == "269533"
    assert catalog.demand(conn)[got["media_id"]]["wanted_by"] == ["Alice"]


def test_a_show_tmdb_cannot_tie_to_a_tvdb_id_is_refused_cleanly(v2, conn):
    with pytest.raises(RequestError, match="TMDB has none"):
        search.add_show(show_tmdb(external_ids={}), FakeSonarr(), "60802", [1], None)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 0


def test_a_failed_show_add_removes_the_show_and_its_episodes_it_created(v2, conn):
    with pytest.raises(RequestError, match="HTTP 500"):
        search.add_show(show_tmdb(), FakeSonarr(reject="Sonarr AddSeries failed: HTTP 500"), "60802", [1], None)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 0
    with pytest.raises(RequestError, match="No seasons selected"):
        search.add_show(show_tmdb(), FakeSonarr(), "60802", [], None)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    with pytest.raises(RequestError, match="does not know that show"):
        search.add_show(FakeTmdb(), FakeSonarr(), "1", [1], None)


def test_requests_of_an_inactive_or_deleted_profile_stop_counting_but_are_kept(v2, conn):
    alice, bob = profile(conn, "Alice"), profile(conn, "Bob")
    conn.commit()
    got = search.add_movie(FakeTmdb(movies={"949": {"id": 949, "title": "Heat"}}), FakeRadarr(), "949", alice)
    conn.execute("INSERT INTO manual_request (media_id, profile_id) VALUES (?, ?)", (got["media_id"], bob))
    conn.commit()
    assert catalog.demand(conn)[got["media_id"]]["wanted_by"] == ["Alice", "Bob"]
    conn.execute("UPDATE profile SET is_active = 0 WHERE id = ?", (bob,))
    assert catalog.demand(conn)[got["media_id"]]["wanted_by"] == ["Alice"]
    conn.execute("DELETE FROM profile WHERE id = ?", (alice,))                                  # allowed: requests are not tied to the profile row
    conn.commit()
    assert got["media_id"] not in catalog.demand(conn)
    assert conn.execute("SELECT COUNT(*) FROM manual_request").fetchone()[0] == 2               # and kept


def test_asking_for_a_title_you_watched_still_counts(v2, conn):
    with v2.connection() as c:
        alice = profile(c)
        mid = c.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Heat', '949')").lastrowid
        c.execute("INSERT INTO profile (name, letterboxd_username) VALUES ('Watcher', 'w')")
        c.execute("INSERT INTO src_letterboxd_entry (username, slug, list, first_seen, last_seen) VALUES ('w', 'heat', 'watched', 'a', 'a')")
        c.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'letterboxd', 'heat', 'x', 1.0)", (mid,))
        watcher = c.execute("SELECT id FROM profile WHERE name = 'Watcher'").fetchone()[0]
        c.execute("INSERT INTO manual_request (media_id, profile_id) VALUES (?, ?)", (mid, watcher))
        c.execute("INSERT INTO manual_request (media_id, profile_id) VALUES (?, ?)", (mid, alice))
    with v2.connection() as c:
        assert catalog.demand(c)[mid] == {"wanted_by": ["Alice", "Watcher"], "watched_by": ["Watcher"]}
