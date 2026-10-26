"""The Discover page, the System status page and the daily picks."""
import re

import pytest

from synchub import discover, settings, status
from synchub.clients import ApiError, ConfigError

from .test_search_web import card  # noqa: F401


def hit(i, title, date="2026-11-01", kind="movie"):
    if kind == "movie":
        return {"id": i, "title": title, "release_date": date, "overview": "", "vote_average": 7.5, "poster_path": f"/{i}.jpg"}
    return {"id": i, "name": title, "first_air_date": date, "overview": "", "vote_average": 8.0, "poster_path": f"/{i}.jpg"}


class ListsTmdb:
    """TMDB's lists. A list set to a string raises it as an error."""
    def __init__(self, **lists):
        self.lists, self.calls = lists, []

    def _list(self, name):
        self.calls.append(name)
        got = self.lists.get(name, [])
        if isinstance(got, Exception):
            raise got
        return got

    def trending(self, kind="all", window="day"):
        return self._list("trending")

    def popular_movies(self):
        return self._list("popular_movies")

    def popular_tv(self):
        return self._list("popular_tv")

    def upcoming_movies(self):
        return self._list("upcoming_movies")

    def movie(self, tmdb_id):
        return None

    def show(self, tmdb_id):
        return None


@pytest.fixture(autouse=True)
def fresh_cache():
    discover.clear_cache()
    yield
    discover.clear_cache()


@pytest.fixture
def lists(monkeypatch):
    import synchub.web.pages as pages
    fake = ListsTmdb(
        trending=[{**hit(1, "Dune"), "media_type": "movie"}, {**hit(2, "Severance", kind="show"), "media_type": "tv"}, {"id": 3, "name": "Some Person", "media_type": "person"}],
        popular_movies=[hit(10, "Heat", "1995-12-15")], popular_tv=[hit(20, "The Bear", kind="show")],
        upcoming_movies=[hit(30, "Later", "2999-01-02"), hit(31, "Sooner", "2999-01-01"), hit(32, "Already out", "2001-01-01")])
    monkeypatch.setattr(pages, "TmdbClient", lambda: fake)
    return fake


def titles(page):
    return re.findall(r'<a class="text-decoration-none text-reset"[^>]*>([^<]+)</a>', page)


def test_the_page_lists_trending_popular_and_upcoming_and_leaves_out_people_and_past_releases(client, v2, lists):
    page = client.get("/discover").get_data(as_text=True)
    for heading in ("Trending Today", "Popular Movies", "Popular TV Shows", "Upcoming Releases"):
        assert heading in page
    assert {"Dune", "Severance", "Heat", "The Bear", "Sooner", "Later"} <= set(titles(page))
    assert "Some Person" not in page and "Already out" not in page
    assert page.index("Sooner") < page.index("Later")                                           # soonest first
    assert "Add to Radarr" in card(page, 1, "movie") and "not endorsed or certified by TMDB" in page
    assert 'href="/discover"' in page and "New &amp; trending" in page                           # in the menu


def test_lists_are_kept_for_a_while_so_the_page_costs_few_tmdb_calls(client, v2, lists):
    client.get("/discover")
    client.get("/discover")
    assert sorted(lists.calls) == ["popular_movies", "popular_tv", "trending", "upcoming_movies"]


def test_a_list_that_fails_leaves_its_own_section_with_a_note_and_the_rest_stay(client, v2, lists):
    lists.lists["popular_tv"] = ApiError("TMDB /tv/popular failed: HTTP 500")
    page = client.get("/discover").get_data(as_text=True)
    assert "TMDB /tv/popular failed: HTTP 500" in page and "Heat" in page and "Dune" in page


def test_without_a_tmdb_key_the_page_says_so_and_still_shows_the_library_sections(client, v2, monkeypatch):
    import synchub.web.pages as pages
    monkeypatch.setattr(pages, "TmdbClient", lambda: (_ for _ in ()).throw(ConfigError("no key")))
    page = client.get("/discover")
    assert page.status_code == 200 and "Add your TMDB API key in Settings" in page.get_data(as_text=True)


def test_a_title_already_in_syncHub_shows_where_it_stands_not_an_add_button(client, v2, lists, conn):
    mid = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Dune', '1')").lastrowid
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (mid,))
    conn.commit()
    dune = card(client.get("/discover").get_data(as_text=True), 1, "movie")
    assert "In Library" in dune and f'href="/movie/{mid}"' in dune and "Add to Radarr" not in dune


def test_recently_added_is_the_newest_jellyfin_titles_without_a_network_call(v2, conn, monkeypatch):
    rows = [{"title": "Old", "date_added": "2026-01-01", "jellyfin_link": "x"}, {"title": "New", "date_added": "2026-10-01", "jellyfin_link": "x"},
            {"title": "Unlinked", "date_added": "2026-11-01", "jellyfin_link": None}, {"title": "Undated", "date_added": None, "jellyfin_link": "x"}]
    monkeypatch.setattr(discover.catalog, "list_catalog", lambda conn, max_rating=None, **k: rows)
    with v2.connection() as c:
        assert [r["title"] for r in discover.recently_added(c, 12)] == ["New", "Old"]


class Calendar:
    def __init__(self, items=None, fail=None):
        self.items, self.fail = items or [], fail

    def calendar(self, start, end):
        if self.fail:
            raise self.fail
        return self.items


def test_coming_up_joins_the_radarr_and_sonarr_calendars_and_names_a_service_that_failed(v2, conn, monkeypatch):
    monkeypatch.setattr(discover, "RadarrClient", lambda: Calendar([{"title": "Heat 2", "tmdbId": 5, "digitalRelease": "2026-11-10T00:00:00Z"},
                                                                    {"title": "Far away", "tmdbId": 6, "inCinemas": "2027-05-01T00:00:00Z"}]))
    monkeypatch.setattr(discover, "SonarrClient", lambda: Calendar([{"airDate": "2026-11-05", "seasonNumber": 2, "episodeNumber": 1, "series": {"title": "Severance", "tmdbId": 9}},
                                                                    {"airDate": "2026-11-06", "seasonNumber": 2, "episodeNumber": 2, "series": {"title": "Severance", "tmdbId": 9}}]))
    with v2.connection() as c:
        items, notes = discover.coming_up(c, today="2026-11-01")
    assert [(i["title"], i["date"], i["detail"]) for i in items] == [("Severance", "2026-11-05", "S2E1 and 1 more"), ("Heat 2", "2026-11-10", "Movie release")] and notes == []
    monkeypatch.setattr(discover, "RadarrClient", lambda: Calendar(fail=RuntimeError("down")))
    monkeypatch.setattr(discover, "SonarrClient", lambda: (_ for _ in ()).throw(ConfigError("not set up")))
    with v2.connection() as c:
        items, notes = discover.coming_up(c, today="2026-11-01")
    assert items == [] and notes == ["Radarr's calendar could not be read."]


def test_the_daily_text_names_a_few_titles_from_trending_and_upcoming():
    sections = [{"key": "trending", "title": "Trending Today", "cards": [{"title": t, "year": 2026} for t in "ABCDEF"]},
                {"key": "popular_movies", "title": "Popular Movies", "cards": [{"title": "Z"}]}, {"key": "upcoming", "title": "Upcoming Releases", "cards": [{"title": "Later", "year": None}]}]
    assert discover.picks_text(sections) == "Trending Today: A (2026), B (2026), C (2026), D (2026)\nUpcoming Releases: Later"


def test_daily_picks_go_only_to_people_who_switched_them_on_and_never_twice_a_day(v2, conn, monkeypatch):
    import synchub.notifications.emit as emit
    from synchub.notifications import store
    import synchub.discover as d
    a = conn.execute("INSERT INTO profile (name) VALUES ('Ann')").lastrowid
    b = conn.execute("INSERT INTO profile (name) VALUES ('Ben')").lastrowid
    conn.commit()
    monkeypatch.setattr(d, "TmdbClient", lambda: object())
    monkeypatch.setattr(d, "tmdb_sections", lambda tmdb, limit: [{"key": "trending", "title": "Trending Today", "cards": [{"title": "Dune", "year": 2026}]}])
    sent = []
    monkeypatch.setattr(emit, "notify", lambda c, event, key, payload, profile_ids=None, now=None: sent.append((event, key, profile_ids, payload["text"])) or [1])
    assert d.send_daily_picks(now="2026-11-01 08:00:00") == 0 and sent == []                    # off until a person chooses it
    with v2.connection() as c:
        store.set_rule(c, a, "daily_picks", True)
    assert d.send_daily_picks(now="2026-11-01 08:00:00") == 1
    assert sent == [("daily_picks", f"picks:2026-11-01:profile:{a}", [a], "Trending Today: Dune (2026)")]
    monkeypatch.setattr(d, "TmdbClient", lambda: (_ for _ in ()).throw(ConfigError("no key")))
    assert d.send_daily_picks() == 0


# ---------------------------------------------------------------- System status
def test_status_calls_an_unset_up_connection_off_not_a_problem(v2, conn):
    with v2.connection() as c:
        snap = status.snapshot(c)
    by = {i["label"]: i for i in snap["items"]}
    assert by["Jellyfin"]["state"] == "off" and by["Radarr"]["detail"] == "Not set up" and by["Workers"]["state"] == "off"
    assert snap["state"] == "ok" and snap["summary"] == "All systems OK"


def test_a_failed_sync_or_a_stale_one_or_an_offline_worker_needs_attention(v2, conn):
    settings.set_value("radarr_url", "http://radarr")
    settings.set_value("radarr_api_key", "k")
    settings.set_value("sonarr_url", "http://sonarr")
    settings.set_value("sonarr_api_key", "k")
    conn.execute("INSERT INTO task_status (task_name, status, last_run, message) VALUES ('radarr', 'failed', '2026-11-01 00:00:00', 'HTTP 401')")
    conn.execute("INSERT INTO task_status (task_name, status, last_run, message) VALUES ('sonarr', 'completed', '2020-01-01 00:00:00', 'done')")
    conn.execute("INSERT INTO worker (name, token_hash, last_seen) VALUES ('pc1', 'x', '2020-01-01 00:00:00')")
    conn.commit()
    with v2.connection() as c:
        snap = status.snapshot(c)
    by = {i["label"]: i for i in snap["items"]}
    assert by["Radarr"]["state"] == "bad" and "HTTP 401" in by["Radarr"]["detail"]
    assert by["Sonarr"]["state"] == "warn" and "days ago" in by["Sonarr"]["detail"]
    assert by["Workers"]["state"] == "bad" and "0 of 1 online" in by["Workers"]["detail"]
    assert snap["state"] == "bad" and snap["attention"] == 3 and snap["summary"] == "3 things need attention"


def test_jobs_waiting_for_you_and_failures_in_the_last_day_show_in_the_queue(v2, conn):
    conn.execute("INSERT INTO job (kind, status) VALUES ('library', 'needs_review')")
    conn.execute("INSERT INTO job (kind, status) VALUES ('library', 'running')")
    conn.execute("INSERT INTO job (kind, status) VALUES ('library', 'failed')")
    conn.commit()
    with v2.connection() as c:
        by = {i["label"]: i for i in status.snapshot(c)["items"]}
    assert by["Waiting for you"]["state"] == "warn" and by["Waiting for you"]["detail"] == "1 job needs a decision"
    assert by["Work queue"]["detail"] == "1 running, 0 waiting" and by["Failed jobs"]["state"] == "bad"


def test_the_status_page_and_the_dashboard_dot(client, v2, conn):
    page = client.get("/system/status")
    assert page.status_code == 200 and "System status" in page.get_data(as_text=True) and "All systems OK" in page.get_data(as_text=True)
    dash = client.get("/").get_data(as_text=True)
    assert 'id="healthDot"' in dash and "All systems OK" in dash and 'href="/system/status"' in dash


def test_someone_who_cannot_read_the_logs_gets_neither_the_page_nor_the_dot(app, conn):
    from synchub import access
    pid = conn.execute("INSERT INTO profile (name, permissions, pin_hash) VALUES ('Kid', 'dashboard', 'x')").lastrowid
    conn.commit()
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "0")
    settings.set_value("access_guest_permissions", "dashboard")
    assert browser.get("/system/status").status_code == 403
    assert 'id="healthDot"' not in browser.get("/").get_data(as_text=True)
