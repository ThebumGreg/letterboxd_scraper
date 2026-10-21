import pytest

from synchub import automation, settings
from synchub.clients import ApiError
from synchub.automation import RequestError

NOW = "2026-01-01 00:00:00"


class FakeRadarr:
    def __init__(self, known=("949", "11"), fail_on=()):
        self.known, self.fail_on, self.added, self.lookups = set(known), set(fail_on), [], []

    def lookup(self, tmdb_id):
        self.lookups.append(tmdb_id)
        return {"title": f"Movie {tmdb_id}", "titleSlug": f"movie-{tmdb_id}", "tmdbId": int(tmdb_id)} if str(tmdb_id) in self.known else None

    def add_movie(self, movie, quality_profile_id, root_folder):
        if str(movie["tmdbId"]) in self.fail_on:
            raise ApiError("Radarr AddMovie failed: HTTP 400 already exists")
        self.added.append((movie["title"], quality_profile_id, root_folder))


class FakeSonarr:
    def __init__(self, series=None, known=("111",), fail_search=False):
        self.series, self.known, self.fail_search = series, set(known), fail_search
        self.added, self.updated, self.searched_seasons, self.monitored, self.searched_eps = [], None, [], [], []

    def lookup(self, tvdb_id):
        return {"title": f"Show {tvdb_id}", "titleSlug": "s", "tvdbId": int(tvdb_id), "seasons": [{"seasonNumber": n, "monitored": True} for n in (0, 1, 2, 3)]} if str(tvdb_id) in self.known else None

    def add_series(self, show, qp, root, monitored_seasons=None, search=True):
        self.added.append((show["tvdbId"], monitored_seasons, search, qp, root))

    def get_series(self, sonarr_id):
        return self.series

    def update_series(self, sonarr_id, payload):
        self.updated = (sonarr_id, payload)

    def search_season(self, sonarr_id, season):
        if self.fail_search:
            raise ApiError("HTTP 500")
        self.searched_seasons.append((sonarr_id, season))

    def monitor_episodes(self, ids, monitored=True):
        self.monitored.append(list(ids))

    def search_episodes(self, ids):
        self.searched_eps.append(list(ids))


def profile(conn, name, lb=None, sz=None, active=1):
    conn.execute("INSERT INTO profile (name, letterboxd_username, serializd_username, is_active) VALUES (?,?,?,?)", (name, lb, sz, active))


def movie(conn, tmdb, title="M"):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', ?, ?)", (title, tmdb)).lastrowid


def show(conn, tmdb, tvdb="111", title="S"):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id, tvdb_id) VALUES ('show', ?, ?, ?)", (title, tmdb, tvdb)).lastrowid


def lb(conn, user, slug, media_id, list_="watchlist", removed=None):
    conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, first_seen, last_seen, removed_at) VALUES (?,?,?,?,?,?)", (user, slug, list_, NOW, NOW, removed))
    conn.execute("INSERT OR IGNORE INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'letterboxd', ?, 'x', 1.0)", (media_id, slug))


def link(conn, source, key, media_id, entity="media"):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES (?,?,?,?, 'x', 1.0)", (entity, media_id, source, key))


# ---------------------------------------------------------------- movies
def test_pending_movies_follow_the_rules(conn):
    profile(conn, "A", "alice"); profile(conn, "B", "bob"); profile(conn, "Off", "off", active=0)
    wanted_by_two, wanted_by_one, in_jf, in_radarr, removed_from_list, watched_by_wanter, only_inactive = (movie(conn, str(i)) for i in range(7))
    lb(conn, "alice", "a", wanted_by_two); lb(conn, "bob", "a", wanted_by_two)
    lb(conn, "alice", "b", wanted_by_one)
    lb(conn, "alice", "c", in_jf)
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen, removed_at) VALUES ('j', 'movie', ?, ?, ?)", (NOW, NOW, NOW))   # deleted from Jellyfin
    link(conn, "jellyfin", "j", in_jf)
    lb(conn, "alice", "d", in_radarr)
    conn.execute("INSERT INTO src_radarr_movie (radarr_id, first_seen, last_seen) VALUES ('r', ?, ?)", (NOW, NOW)); link(conn, "radarr", "r", in_radarr)
    lb(conn, "alice", "e", removed_from_list, removed=NOW)
    lb(conn, "alice", "f", watched_by_wanter); lb(conn, "alice", "f", watched_by_wanter, "watched")
    lb(conn, "off", "g", only_inactive)
    assert [m["media_id"] for m in automation.pending_movies(conn, 1)] == [wanted_by_two, wanted_by_one]
    assert [m["media_id"] for m in automation.pending_movies(conn, 2)] == [wanted_by_two]


def test_radarr_additions_add_dry_run_and_report_errors(conn):
    profile(conn, "A", "alice")
    ok, unknown, rejected = movie(conn, "949", "Heat"), movie(conn, "404", "Nope"), movie(conn, "11", "Dup")
    for i, mid in enumerate((ok, unknown, rejected)):
        lb(conn, "alice", f"s{i}", mid)
    settings.set_value("radarr_quality_profile_id", "4", conn=conn); settings.set_value("radarr_root_folder", "/films", conn=conn)
    conn.commit()
    radarr = FakeRadarr(fail_on=("11",))
    result = automation.run_radarr_additions(conn, radarr)
    assert result["added"] == ["Movie 949"] and radarr.added == [("Movie 949", "4", "/films")]
    assert len(result["errors"]) == 2 and any("Nope" in e and "not found" in e for e in result["errors"]) and any("Dup" in e for e in result["errors"])
    dry = FakeRadarr()
    dry_result = automation.run_radarr_additions(conn, dry, dry_run=True)
    assert dry_result["would_add"] == ["Dup", "Heat"] and len(dry_result["errors"]) == 1 and dry.added == []


def test_request_movie(conn):
    mid = movie(conn, "949")
    radarr = FakeRadarr()
    assert automation.request_movie(conn, radarr, mid) == "Movie 949"
    conn.execute("INSERT INTO src_radarr_movie (radarr_id, first_seen, last_seen) VALUES ('r', ?, ?)", (NOW, NOW)); link(conn, "radarr", "r", mid)
    with pytest.raises(RequestError, match="already in Radarr"):
        automation.request_movie(conn, radarr, mid)


# ---------------------------------------------------------------- shows
def episodes(conn, media_id, plan):
    """plan: list of (season, number, air_date, tvdb_season) -> ids"""
    return [conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date, tvdb_season_number) VALUES (?,?,?,?,?)",
                         (media_id, s, n, air, ts)).lastrowid for s, n, air, ts in plan]


def sonarr_series(conn, media_id, sid="10"):
    conn.execute("INSERT INTO src_sonarr_series (sonarr_id, first_seen, last_seen) VALUES (?,?,?)", (sid, NOW, NOW)); link(conn, "sonarr", sid, media_id)


def test_pending_shows_need_a_tvdb_id_and_use_serializd_seasons(conn):
    profile(conn, "A", sz="alice_sz")
    with_tvdb, without = show(conn, "1", "111", "Has TVDB"), show(conn, "2", None, "No TVDB")
    for sid, mid, season in (("s1", with_tvdb, 2), ("s2", without, None)):
        conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, season_number, first_seen, last_seen) VALUES ('alice_sz', ?, ?, ?, ?)", (sid, season, NOW, NOW)); link(conn, "serializd", sid, mid)
    shows, skipped = automation.pending_shows(conn, 1)
    assert [(s["title"], s["seasons"]) for s in shows] == [("Has TVDB", [2])] and skipped == ["No TVDB"]


def test_sonarr_additions(conn):
    profile(conn, "A", sz="alice_sz")
    mid = show(conn, "1", "111", "Show")
    conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, first_seen, last_seen) VALUES ('alice_sz', 's1', ?, ?)", (NOW, NOW)); link(conn, "serializd", "s1", mid)
    sonarr = FakeSonarr()
    result = automation.run_sonarr_additions(conn, sonarr)
    assert result["added"] == ["Show"] and sonarr.added[0][:3] == (111, None, True)


def test_request_seasons_keeps_other_seasons_and_searches(conn):
    mid = show(conn, "1")
    sonarr_series(conn, mid)
    sonarr = FakeSonarr(series={"id": 10, "monitored": False, "seasons": [{"seasonNumber": n, "monitored": n == 1} for n in (0, 1, 2, 3)]})
    result = automation.request_seasons(conn, sonarr, mid, [2, "3"])
    assert result == {"status": "updated", "seasons": [2, 3], "search_errors": []}
    assert {s["seasonNumber"]: s["monitored"] for s in sonarr.updated[1]["seasons"]} == {0: False, 1: True, 2: True, 3: True}
    assert sonarr.searched_seasons == [("10", 2), ("10", 3)]


def test_request_seasons_uses_tvdb_numbering_when_it_differs(conn):
    mid = show(conn, "1")
    sonarr_series(conn, mid)
    episodes(conn, mid, [(1, 13, "2015-06-01", 2)])         # TMDB season 1 episode 13 is TVDB season 2
    sonarr = FakeSonarr(series={"id": 10, "seasons": [{"seasonNumber": n, "monitored": False} for n in (1, 2)]})
    result = automation.request_seasons(conn, sonarr, mid, [1])
    assert result["seasons"] == [2] and sonarr.searched_seasons == [("10", 2)]


def test_request_seasons_adds_unknown_show_with_only_those_seasons(conn):
    mid = show(conn, "1")
    sonarr = FakeSonarr()
    assert automation.request_seasons(conn, sonarr, mid, [2])["status"] == "added"
    assert sonarr.added[0][:3] == (111, [2], True)


def test_request_errors_are_clear(conn):
    no_tvdb = show(conn, "9", None)
    with pytest.raises(RequestError, match="TVDB ID"):
        automation.request_seasons(conn, FakeSonarr(), no_tvdb, [1])
    mid = show(conn, "1", "999")
    with pytest.raises(RequestError, match="not found by Sonarr"):
        automation.request_seasons(conn, FakeSonarr(), mid, [1])
    with pytest.raises(RequestError, match="No seasons"):
        automation.request_seasons(conn, FakeSonarr(), mid, [])
    with pytest.raises(RequestError, match="whole numbers"):
        automation.request_seasons(conn, FakeSonarr(), mid, ["x"])


def test_season_search_failure_is_reported_not_fatal(conn):
    mid = show(conn, "1")
    sonarr_series(conn, mid)
    sonarr = FakeSonarr(series={"id": 10, "seasons": [{"seasonNumber": 1}]}, fail_search=True)
    assert automation.request_seasons(conn, sonarr, mid, [1])["search_errors"] == ["S1: HTTP 500"]


def test_request_episodes_maps_to_sonarr_ids(conn):
    mid = show(conn, "1")
    sonarr_series(conn, mid)
    e1, e2 = episodes(conn, mid, [(1, 1, "2020-01-01", None), (1, 2, "2020-01-08", None)])
    for sid, eid in (("501", e1), ("502", e2)):
        conn.execute("INSERT INTO src_sonarr_episode (sonarr_episode_id, sonarr_id, first_seen, last_seen) VALUES (?, '10', ?, ?)", (sid, NOW, NOW)); link(conn, "sonarr_episode", sid, eid, "episode")
    sonarr = FakeSonarr()
    assert automation.request_episodes(conn, sonarr, mid, [e1, e2]) == {"status": "updated", "episodes": 2}
    assert sonarr.monitored == [["501", "502"]] and sonarr.searched_eps == [["501", "502"]]


def test_request_episodes_for_a_show_not_in_sonarr_adds_it_then_resyncs(conn):
    mid = show(conn, "1")
    (e1,) = episodes(conn, mid, [(1, 1, "2020-01-01", None)])
    sonarr = FakeSonarr()

    def resync():
        sonarr_series(conn, mid)
        conn.execute("INSERT INTO src_sonarr_episode (sonarr_episode_id, sonarr_id, first_seen, last_seen) VALUES ('501', '10', ?, ?)", (NOW, NOW)); link(conn, "sonarr_episode", "501", e1, "episode")

    automation.request_episodes(conn, sonarr, mid, [e1], resync)
    assert sonarr.added[0][1:3] == ([], False) and sonarr.monitored == [["501"]]


def test_request_episodes_unknown_to_sonarr_is_an_error(conn):
    mid = show(conn, "1")
    sonarr_series(conn, mid)
    (e1,) = episodes(conn, mid, [(1, 1, "2020-01-01", None)])
    with pytest.raises(RequestError, match="not known to Sonarr"):
        automation.request_episodes(conn, FakeSonarr(), mid, [e1])


def test_request_all_picks_seasons_with_aired_missing_episodes_but_not_specials(conn):
    mid = show(conn, "1")
    sonarr_series(conn, mid)
    s0, s1a, s1b, s2, s3 = episodes(conn, mid, [(0, 1, "2010-01-01", None), (1, 1, "2020-01-01", None), (1, 2, "2020-01-08", None), (2, 1, "2021-01-01", None), (3, 1, "2099-01-01", None)])
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('episode', ?, 'in_library')", (s2,))      # season 2 is complete
    sonarr = FakeSonarr(series={"id": 10, "seasons": [{"seasonNumber": n} for n in (0, 1, 2, 3)]})
    result = automation.request_all(conn, sonarr, mid, today="2026-06-15")
    assert result["seasons"] == [1]       # not 0 (Specials), not 2 (complete), not 3 (unaired)
    with pytest.raises(RequestError, match="Nothing missing"):
        conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('episode', ?, 'in_library')", (s1a,))
        conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('episode', ?, 'in_library')", (s1b,))
        automation.request_all(conn, sonarr, mid, today="2026-06-15")
