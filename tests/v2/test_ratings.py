"""Age ratings on the movie and show pages, parental controls (a rating limit for each profile) and the stricter guest account."""
import re

import pytest

from synchub import access, catalog, metadata, ratings, settings, tasks
from synchub.clients import ApiError
from synchub.tv import runtime
from synchub.web import create_app

from .fakes import FakeTmdb
from . import test_access
from .test_search import movie_hit, show_hit


def text(response):
    return response.get_data(as_text=True)


@pytest.fixture(autouse=True)
def default_guests(v2, app):
    """The shared `app` fixture opens the guest account up for other tests; these tests are about the defaults a new installation has."""
    with v2.connection() as conn:
        conn.execute("DELETE FROM app_setting WHERE key IN ('access_guest_permissions', 'guest_max_rating')")


def release_dates(*entries, country="US"):
    return {"results": [{"iso_3166_1": country, "release_dates": [{"certification": c, "type": t} for c, t in entries]}]}


# ---------------------------------------------------------------- arranging
def add_title(conn, kind, title, tmdb_id, certification, vote=7.5, with_rating_row=True):
    media_id = conn.execute("INSERT INTO media (kind, title, tmdb_id, year) VALUES (?, ?, ?, 2001)", (kind, title, str(tmdb_id))).lastrowid
    if with_rating_row:
        label = ratings.normalize(certification)
        conn.execute("INSERT INTO title_rating (kind, tmdb_id, certification, rating_rank, vote_average, checked_at) VALUES (?, ?, ?, ?, ?, '2026-01-01 00:00:00')",
                     (kind, str(tmdb_id), label or certification, ratings.RANKS.get(label), vote))
    conn.commit()
    return media_id


@pytest.fixture
def library(v2, conn):
    """One title at every rating, an unrated one, and one SyncHub has not looked up yet."""
    ids = {"cartoon": add_title(conn, "movie", "Cartoon", 1, "G"), "family": add_title(conn, "movie", "Family", 2, "PG"),
           "teen": add_title(conn, "movie", "Teen", 3, "PG-13"), "adult": add_title(conn, "movie", "Adult", 4, "R"),
           "explicit": add_title(conn, "movie", "Explicit", 5, "NC-17"), "unrated": add_title(conn, "movie", "Unrated", 6, None),
           "unknown": add_title(conn, "movie", "Unknown", 7, None, with_rating_row=False),
           "kidshow": add_title(conn, "show", "Kid Show", 8, "TV-Y7"), "dramashow": add_title(conn, "show", "Drama Show", 9, "TV-MA")}
    return ids


def shown(rows):
    return sorted(r["title"] for r in rows)


def on_dashboard(client):
    page = text(client.get("/"))
    return sorted(set(re.findall(r'data-display-title="([^"]*)"', page)))


def add_profile(conn, name, permissions=None, pin="1234"):
    """A profile with a PIN (a profile without one is treated as a guest, and once nobody has a PIN nothing is restricted at all)."""
    pid = test_access.add_profile(conn, name, permissions)
    if pin:                                  # straight into the table: access.set_pin would refuse a first PIN on a profile that cannot open People
        from werkzeug.security import generate_password_hash
        conn.execute("UPDATE profile SET pin_hash = ?, pin_version = 1 WHERE id = ?", (generate_password_hash(pin), pid))
        conn.commit()
    return pid


def as_profile(app, profile_id):
    """A device where this profile has been chosen and its PIN entered."""
    from synchub import access, db
    browser = app.test_client()
    browser.set_cookie("synchub_profile", str(profile_id))
    with db.connection() as conn:
        version = conn.execute("SELECT pin_version FROM profile WHERE id = ?", (profile_id,)).fetchone()["pin_version"]
    browser.set_cookie("synchub_pin", access.issue_proof(app.secret_key, profile_id, version))
    return browser


# ---------------------------------------------------------------- the scale
@pytest.mark.parametrize("label,expected", [
    ("G", 0), ("TV-Y", 0), ("TV-Y7", 0), ("TV-G", 0), ("PG", 1), ("TV-PG", 1), ("PG-13", 2), ("TV-14", 2), ("R", 3), ("TV-MA", 3), ("NC-17", 4),
    ("Rated PG-13", 2), ("pg-13", 2), ("TV14", 2), ("tv-ma", 3), ("US-TV-MA", 3), ("NR", None), ("Not Rated", None), ("", None), (None, None), ("FSK-12", None),
])
def test_every_known_label_is_put_on_one_scale(label, expected):
    assert ratings.rank(label) == expected


def test_a_title_is_permitted_up_to_the_limit_and_unrated_ones_only_without_a_limit():
    assert ratings.permitted(None, None) and ratings.permitted(4, None)
    assert [ratings.permitted(rank, 1) for rank in (0, 1, 2, 3, 4, None)] == [True, True, False, False, False, False]
    assert ratings.permitted(0, 0) and not ratings.permitted(1, 0)


@pytest.mark.parametrize("value,expected", [("0", 0), ("1", 1), ("3", 3), (2, 2), ("", None), (None, None), ("4", None), ("-1", None), ("nope", None), ("none", None)])
def test_a_stored_limit_outside_the_four_choices_means_no_limit(value, expected):
    assert ratings.parse_level(value) == expected


# ---------------------------------------------------------------- reading TMDB
def test_a_movie_takes_its_us_theatrical_rating_over_other_releases():
    details = {"release_dates": {"results": [
        {"iso_3166_1": "GB", "release_dates": [{"certification": "15", "type": 3}]},
        {"iso_3166_1": "US", "release_dates": [{"certification": "", "type": 1}, {"certification": "NR", "type": 4}, {"certification": "PG-13", "type": 3}]}]}}
    assert ratings.from_movie(details) == "PG-13"


def test_a_movie_with_no_theatrical_rating_takes_another_us_release_and_one_with_none_has_none():
    assert ratings.from_movie({"release_dates": release_dates(("R", 5))}) == "R"
    assert ratings.from_movie({"release_dates": release_dates(("", 3))}) is None
    assert ratings.from_movie({"release_dates": release_dates(("PG", 3), country="FR")}) is None
    assert ratings.from_movie({}) is None


def test_a_show_takes_its_us_content_rating():
    details = {"content_ratings": {"results": [{"iso_3166_1": "DE", "rating": "12"}, {"iso_3166_1": "US", "rating": "TV-MA"}]}}
    assert ratings.from_show(details) == "TV-MA"
    assert ratings.from_show({"content_ratings": {"results": [{"iso_3166_1": "DE", "rating": "12"}]}}) is None


def test_the_details_calls_ask_tmdb_for_the_ratings_too(monkeypatch):
    from synchub.clients.tmdb import TmdbClient
    asked = []
    client = TmdbClient("key")
    monkeypatch.setattr(client, "_get", lambda path, **params: asked.append((path, params)) or {})
    client.movie(1)
    client.show(2)
    assert asked == [("/movie/1", {"append_to_response": "release_dates"}), ("/tv/2", {"append_to_response": "external_ids,content_ratings"})]


def test_refreshing_a_title_keeps_its_rating_and_audience_score(conn):
    movie = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'x', '949')").lastrowid
    show = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'y', '60')").lastrowid
    tmdb = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15", "vote_average": 7.84, "release_dates": release_dates(("R", 3))}},
                    shows={"60": {"id": 60, "name": "Show", "first_air_date": "2010-01-01", "vote_average": 8.26, "seasons": [],
                                  "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-14"}]}}})
    metadata.refresh_movie(conn, tmdb, movie)
    metadata.refresh_show(conn, tmdb, show)
    assert ratings.get(conn, "movie", "949") == {"certification": "R", "rank": 3, "vote_average": 7.8}
    assert ratings.get(conn, "show", "60") == {"certification": "TV-14", "rank": 2, "vote_average": 8.3}


def test_details_that_came_without_ratings_are_not_read_as_unrated(conn):
    movie = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'x', '949')").lastrowid
    metadata.refresh_movie(conn, FakeTmdb(movies={"949": {"id": 949, "title": "Heat"}}), movie)
    assert ratings.get(conn, "movie", "949") is None


def test_titles_with_no_rating_yet_are_looked_up_one_call_each_and_unrated_ones_are_not_asked_again_soon(conn):
    add_title(conn, "movie", "Heat", 949, None, with_rating_row=False)
    add_title(conn, "show", "Show", 60, None, with_rating_row=False)
    add_title(conn, "movie", "Done", 1, "PG")
    tmdb = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "vote_average": 7, "release_dates": release_dates(("R", 3))}},
                    shows={"60": {"id": 60, "name": "Show", "content_ratings": {"results": []}}})
    assert ratings.fill_missing(conn, tmdb) == {"checked": 2, "rated": 1, "unrated": 1, "errors": 0}
    assert sorted(c[1] for c in tmdb.calls) == ["60", "949"]                 # the one that already had a rating was not asked about
    again = FakeTmdb(movies=tmdb.movies, shows=tmdb.shows)
    assert ratings.fill_missing(conn, again)["checked"] == 0                 # an unrated show is only asked about again after a month
    conn.execute("UPDATE title_rating SET checked_at = '2000-01-01 00:00:00' WHERE kind = 'show'")
    assert ratings.fill_missing(conn, again)["checked"] == 1


def test_a_failed_lookup_is_counted_and_does_not_stop_the_rest(conn):
    add_title(conn, "movie", "Heat", 949, None, with_rating_row=False)
    assert ratings.fill_missing(conn, FakeTmdb(fail=True)) == {"checked": 1, "rated": 0, "unrated": 0, "errors": 1}


def test_the_reconcile_job_fills_in_ratings(conn, monkeypatch):
    add_title(conn, "movie", "Heat", 949, None, with_rating_row=False)
    conn.commit()
    tmdb = FakeTmdb(movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15", "release_dates": release_dates(("R", 3))}})
    tasks.reconcile_now(tmdb)
    assert ratings.get(conn, "movie", "949")["certification"] == "R"


# ---------------------------------------------------------------- showing the rating
def test_the_movie_page_shows_the_rating_and_the_audience_score(client, library):
    page = text(client.get(f"/movie/{library['adult']}"))
    assert 'data-rating="R"' in page and 'data-score="7.5"' in page and "7.5" in page


def test_the_show_page_shows_the_rating_and_says_so_when_there_is_none(client, library, conn):
    assert 'data-rating="TV-MA"' in text(client.get(f"/show/{library['dramashow']}"))
    assert 'data-rating="none"' in text(client.get(f"/movie/{library['unrated']}"))


def test_the_dashboard_and_search_label_titles_with_their_rating(client, library):
    assert '<span class="badge border text-body-secondary ms-1 fw-normal" title="Age rating">PG-13</span>' in text(client.get("/"))


# ---------------------------------------------------------------- the dashboard under a limit
def test_a_profile_with_a_limit_sees_only_titles_up_to_it_and_unrated_ones_are_hidden(app, v2, conn, library):
    pid = add_profile(conn, "Kid")
    access.set_max_rating(conn, pid, 1)
    conn.commit()
    assert on_dashboard(as_profile(app, pid)) == ["Cartoon", "Family", "Kid Show"]
    access.set_max_rating(conn, pid, 3)
    conn.commit()
    assert on_dashboard(as_profile(app, pid)) == ["Adult", "Cartoon", "Drama Show", "Family", "Kid Show", "Teen"]


def test_a_profile_with_no_limit_sees_everything(app, v2, conn, library):
    pid = add_profile(conn, "Grown-up")
    assert len(on_dashboard(as_profile(app, pid))) == 9


def test_the_counts_on_the_dashboard_only_count_what_the_person_may_see(v2, conn, library):
    rows = catalog.list_catalog(conn, max_rating=0)
    assert shown(rows) == ["Cartoon", "Kid Show"] and catalog.dashboard_stats(rows)["total"] == 2
    assert len(catalog.list_catalog(conn)) == 9


def test_a_title_above_the_limit_has_no_page_for_that_profile(app, v2, conn, library):
    pid = add_profile(conn, "Kid")
    access.set_max_rating(conn, pid, 1)
    conn.commit()
    kid = as_profile(app, pid)
    assert kid.get(f"/movie/{library['family']}").status_code == 200
    for path in (f"/movie/{library['adult']}", f"/movie/{library['unrated']}", f"/movie/{library['unknown']}", f"/show/{library['dramashow']}"):
        response = kid.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/"), path
    assert kid.get(f"/api/show/{library['dramashow']}/season/1").status_code == 403
    assert kid.get(f"/api/show/{library['kidshow']}/season/1").status_code == 200


def test_the_export_only_lists_what_the_person_may_see(app, v2, conn, library):
    pid = add_profile(conn, "Kid", permissions="dashboard,tools")
    access.set_max_rating(conn, pid, 0)
    conn.commit()
    csv_text = text(as_profile(app, pid).get("/tools/export-csv"))
    assert "Cartoon" in csv_text and "Kid Show" in csv_text and "Adult" not in csv_text and "Drama Show" not in csv_text


def test_requests_for_titles_above_the_limit_are_refused(app, v2, conn, library, monkeypatch):
    called = []
    monkeypatch.setattr("synchub.automation.request_movie", lambda *a: called.append(a) or "x")
    pid = add_profile(conn, "Kid", permissions="dashboard,requests")
    access.set_max_rating(conn, pid, 1)
    conn.commit()
    kid = as_profile(app, pid)
    blocked = kid.post(f"/api/request/movie/{library['adult']}")
    assert blocked.get_json() == {"ok": False, "message": "That title is not available for your profile."} and not called
    for path, body in ((f"/api/request/show/{library['dramashow']}/seasons", {"seasons": [1]}), (f"/api/request/show/{library['dramashow']}/episodes", {"episode_ids": [1]}),
                       (f"/api/request/show/{library['dramashow']}/all", {})):
        assert kid.post(path, json=body).get_json()["ok"] is False, path
    assert kid.post(f"/api/movie/{library['adult']}/refresh").get_json()["ok"] is False


# ---------------------------------------------------------------- search under a limit
@pytest.fixture
def search_tmdb(monkeypatch):
    import synchub.web.api as api
    import synchub.web.pages as pages
    fake = FakeTmdb(search={("movie", "heat"): [movie_hit(949, "Heat"), movie_hit(950, "Heat Kids", "2001-01-01"), movie_hit(951, "Heat Mystery", "2001-01-01")],
                            ("tv", "heat"): [show_hit(60, "Heat Show")]},
                    movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15", "release_dates": release_dates(("R", 3))},
                            "950": {"id": 950, "title": "Heat Kids", "release_dates": release_dates(("G", 3))},
                            "951": {"id": 951, "title": "Heat Mystery", "release_dates": release_dates(("", 3))}},
                    shows={"60": {"id": 60, "name": "Heat Show", "external_ids": {}, "seasons": [], "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-PG"}]}}})
    monkeypatch.setattr(pages, "TmdbClient", lambda: fake)
    monkeypatch.setattr(api, "TmdbClient", lambda: fake)
    return fake


def found(browser):
    return sorted(re.findall(r'data-kind="\w+" data-tmdb-id="(\d+)"', text(browser.get("/search?q=heat"))))


def test_search_only_finds_titles_within_the_limit_and_asks_tmdb_once_per_title(app, v2, conn, search_tmdb):
    pid = add_profile(conn, "Kid", permissions="dashboard,search")
    access.set_max_rating(conn, pid, 1)
    conn.commit()
    kid = as_profile(app, pid)
    assert found(kid) == ["60", "950"]                                       # Heat is R, Heat Mystery is unrated
    asked = len(search_tmdb.calls)
    assert found(kid) == ["60", "950"] and [c for c in search_tmdb.calls[asked:] if c[0] in ("movie", "show")] == []
    assert "Heat Kids" in text(kid.get("/search?q=heat")) and 'title="Age rating">G<' in text(kid.get("/search?q=heat"))


def test_search_without_a_limit_shows_everything_and_makes_no_extra_tmdb_calls(app, v2, conn, search_tmdb):
    pid = add_profile(conn, "Grown-up", permissions="dashboard,search")
    assert found(as_profile(app, pid)) == ["60", "949", "950", "951"]
    assert [c for c in search_tmdb.calls if c[0] in ("movie", "show")] == []


def test_adding_a_title_above_the_limit_from_search_is_refused(app, v2, conn, search_tmdb):
    pid = add_profile(conn, "Kid", permissions="dashboard,search,requests")
    access.set_max_rating(conn, pid, 1)
    conn.commit()
    kid = as_profile(app, pid)
    for body, path in (({"tmdb_id": "949"}, "/api/search/add/movie"), ({"tmdb_id": "951"}, "/api/search/add/movie")):
        assert kid.post(path, json=body).get_json() == {"ok": False, "message": "That title is not available for your profile."}
    assert kid.get("/api/search/show/60/seasons").get_json()["ok"] is True
    assert as_profile(app, add_profile(conn, "Other", permissions="dashboard,search,requests")).get("/api/search/show/60/seasons").get_json()["ok"] is True


# ---------------------------------------------------------------- setting the limits
def test_a_limit_is_saved_for_a_profile_and_for_guests(app, v2, conn):
    admin = add_profile(conn, "Admin")
    kid = add_profile(conn, "Kid")
    browser = as_profile(app, admin)
    assert browser.post(f"/api/access/profile/{kid}/rating", json={"max_rating": "2"}).get_json()["ok"] is True
    assert conn.execute("SELECT max_rating FROM profile WHERE id = ?", (kid,)).fetchone()[0] == 2
    assert browser.post(f"/api/access/profile/{kid}/rating", json={"max_rating": ""}).get_json()["ok"] is True
    assert conn.execute("SELECT max_rating FROM profile WHERE id = ?", (kid,)).fetchone()[0] is None
    assert browser.post("/api/access/guest-rating", json={"max_rating": "0"}).get_json()["ok"] is True
    assert settings.get("guest_max_rating") == "0" and ratings.guest_limit(conn) == 0
    assert browser.post("/api/access/guest-rating", json={"max_rating": ""}).get_json()["ok"] is True
    assert ratings.guest_limit(conn) is None


@pytest.mark.parametrize("value", ["4", "-1", "R", "99", True, [1]])
def test_a_limit_that_is_not_one_of_the_choices_is_refused_not_read_as_no_limit(app, v2, conn, value):
    admin, kid = add_profile(conn, "Admin"), add_profile(conn, "Kid")
    access.set_max_rating(conn, kid, 1)
    conn.commit()
    browser = as_profile(app, admin)
    assert browser.post(f"/api/access/profile/{kid}/rating", json={"max_rating": value}).get_json()["ok"] is False
    assert browser.post("/api/access/guest-rating", json={"max_rating": value}).get_json()["ok"] is False
    assert conn.execute("SELECT max_rating FROM profile WHERE id = ?", (kid,)).fetchone()[0] == 1 and ratings.guest_limit(conn) == 1


def test_only_someone_with_the_people_permission_can_change_a_limit(app, v2, conn):
    kid = add_profile(conn, "Kid", permissions="dashboard,search")
    other = add_profile(conn, "Other")
    browser = as_profile(app, kid)
    assert browser.post(f"/api/access/profile/{other}/rating", json={"max_rating": "0"}).status_code == 403
    assert browser.post("/api/access/guest-rating", json={"max_rating": ""}).status_code == 403


def test_the_people_page_offers_a_limit_for_guests_and_every_profile(app, v2, conn):
    admin, kid = add_profile(conn, "Admin"), add_profile(conn, "Kid")
    access.set_max_rating(conn, kid, 2)
    conn.commit()
    browser = as_profile(app, admin)
    page, person = text(browser.get("/people")), text(browser.get(f"/people/{kid}"))
    assert page.count("data-save-rating=") == 1 and person.count("data-save-rating=") == 1
    assert re.search(r'id="rating-guest".*?<option value="1" selected>PG ', page, re.S)
    assert re.search(r'id="rating-%d".*?<option value="2" selected>PG-13 ' % kid, person, re.S)


def test_the_people_page_warns_when_guests_have_no_limit(v2, conn):
    add_profile(conn, "Admin")
    assert not [w for w in access.overview(conn)["warnings"] if "rating limit" in w]
    access.set_guest_max_rating(conn, None)
    assert [w for w in access.overview(conn)["warnings"] if "no rating limit" in w]


def test_a_limit_survives_the_profile_being_edited_on_the_people_page(app, v2, conn):
    admin, kid = add_profile(conn, "Admin"), add_profile(conn, "Kid")
    access.set_max_rating(conn, kid, 1)
    conn.commit()
    as_profile(app, admin).post("/settings/profiles/save", data={"profile_id": kid, "profile_name": "Kiddo", "letterboxd_username": "", "serializd_username": ""})
    assert conn.execute("SELECT name, max_rating FROM profile WHERE id = ?", (kid,)).fetchone()[:] == ("Kiddo", 1)


# ---------------------------------------------------------------- guests
def test_before_any_profile_exists_everyone_can_open_everything_so_setup_is_possible(client, library):
    for path in ("/", "/search", "/settings", "/people", "/tools", f"/movie/{library['adult']}"):
        assert client.get(path).status_code == 200, path
    assert len(on_dashboard(client)) == 9


def test_once_a_profile_exists_a_guest_can_only_open_the_dashboard_and_the_remote(app, v2, conn, library):
    add_profile(conn, "Admin")
    guest = app.test_client()
    guest.set_cookie("synchub_profile", "0")
    assert guest.get("/").status_code == 200 and guest.get("/tv/remote").status_code == 200
    for path in ("/search", "/settings", "/people", "/tools", "/logs", "/payloads", "/library", "/workers", "/storage", "/ddns", "/resolution", "/tv/devices", "/tv/advanced"):       # (TV apps is part of the Remote permission)
        assert guest.get(path).status_code == 403, path
    page = text(guest.get("/"))
    assert 'href="/search"' not in page and 'href="/settings"' not in page and 'href="/tv/remote"' in page


def test_a_guest_sees_nothing_above_pg_until_the_limit_is_changed(app, v2, conn, library):
    add_profile(conn, "Admin")
    guest = app.test_client()
    guest.set_cookie("synchub_profile", "0")
    assert on_dashboard(guest) == ["Cartoon", "Family", "Kid Show"]
    assert guest.get(f"/movie/{library['adult']}").status_code == 302 and guest.get(f"/movie/{library['family']}").status_code == 200
    access.set_guest_max_rating(conn, 3)
    conn.commit()
    assert on_dashboard(guest) == ["Adult", "Cartoon", "Drama Show", "Family", "Kid Show", "Teen"]
    access.set_guest_max_rating(conn, None)
    conn.commit()
    assert len(on_dashboard(guest)) == 9


def test_a_device_that_has_not_chosen_anyone_is_sent_to_the_chooser_and_stays_limited_as_a_guest(app, v2, conn, library):
    add_profile(conn, "Admin")
    fresh = app.test_client()
    assert fresh.get("/").status_code == 302 and "/who" in fresh.get("/").headers["Location"]
    assert fresh.get(f"/api/show/{library['dramashow']}/season/1").status_code in (302, 403)


def test_the_last_profile_can_still_be_deleted_whatever_the_guest_pages_are(v2, conn):
    pid = add_profile(conn, "Only")
    access.ensure_profile_can_go(conn, pid)                                   # no profile left: everyone can open everything again


def test_the_last_person_with_the_people_permission_still_cannot_be_removed_while_others_exist(v2, conn):
    from synchub.access import AccessError
    admin = add_profile(conn, "Admin")
    add_profile(conn, "Other", permissions="dashboard")
    with pytest.raises(AccessError):
        access.ensure_profile_can_go(conn, admin)


# ---------------------------------------------------------------- the remote
class FakeJf:
    """A Jellyfin with one title of each kind; every call is written down."""
    ITEMS = {"kid": {"Id": "kid", "Type": "Movie", "Name": "Kid Movie", "OfficialRating": "G"},
             "adult": {"Id": "adult", "Type": "Movie", "Name": "Adult Movie", "OfficialRating": "R"},
             "plain": {"Id": "plain", "Type": "Movie", "Name": "Unrated Movie"},
             "tmdbonly": {"Id": "tmdbonly", "Type": "Movie", "Name": "Via TMDB", "ProviderIds": {"Tmdb": "1"}},
             "series": {"Id": "series", "Type": "Series", "Name": "Adult Show", "OfficialRating": "TV-MA"},
             "ep": {"Id": "ep", "Type": "Episode", "Name": "Pilot", "SeriesId": "series"},
             "kidseries": {"Id": "kidseries", "Type": "Series", "Name": "Kid Show", "OfficialRating": "TV-Y"},
             "kidep": {"Id": "kidep", "Type": "Episode", "Name": "Kid Pilot", "SeriesId": "kidseries"}}

    def __init__(self):
        self.played = []

    def search(self, q, user_id=None):
        from synchub.tv.jellyfin import summarize_item
        return [summarize_item(i) for i in self.ITEMS.values()]

    def item(self, item_id):
        return self.ITEMS.get(item_id, {})

    def episodes(self, series_id, user_id=None):
        return [{"Id": "ep"}]

    def user_data(self, item_id, user_id):
        return {}

    def sessions(self, quiet=True):
        return [{"Id": "s1", "Client": "Jellyfin Roku", "DeviceName": "Bed Room Roku", "SupportsRemoteControl": True, "LastActivityDate": "2026-10-25T10:00:00Z",
                 "NowPlayingItem": dict(self.ITEMS["adult"]), "PlayState": {}, "LastPlaybackCheckIn": "2099-01-01T00:00:00Z"}]

    def play(self, sid, params):
        self.played.append(params)


@pytest.fixture
def tv(tmp_path, monkeypatch):
    rt = runtime.configure(tmp_path / "tv")
    rt.prefs["devices"] = [{"id": "bed", "name": "Bed Room Roku", "type": "roku", "host": "10.0.0.5"}]
    rt.reload_devices()
    jf = FakeJf()
    monkeypatch.setattr(rt, "jf", lambda: jf)
    rt.fake_jf = jf
    yield rt
    runtime._runtime = None


@pytest.fixture
def kid_remote(app, v2, conn, tv):
    pid = add_profile(conn, "Kid", permissions="dashboard,tv")
    access.set_max_rating(conn, pid, 1)
    add_title(conn, "movie", "Via TMDB", 1, "PG")
    conn.commit()
    return as_profile(app, pid)


def test_the_remote_search_only_lists_what_the_limit_allows(kid_remote):
    names = sorted(i["Name"] for i in kid_remote.get("/api/tv/jf/search?q=x").get_json())
    assert names == ["Kid Movie", "Kid Pilot", "Kid Show", "Via TMDB"]        # not the R movie, the unrated one, the TV-MA show or its episode


def test_the_remote_search_lists_everything_to_someone_without_a_limit(app, v2, conn, tv):
    pid = add_profile(conn, "Grown-up", permissions="dashboard,tv")
    assert len(as_profile(app, pid).get("/api/tv/jf/search?q=x").get_json()) == len(FakeJf.ITEMS)


def test_the_remote_will_not_list_or_play_what_the_limit_hides(kid_remote, tv):
    assert kid_remote.get("/api/tv/jf/episodes/series").get_json()["ok"] is False
    assert kid_remote.get("/api/tv/jf/episodes/kidseries").status_code == 200
    for hidden in ("adult", "plain", "series", "ep"):
        response = kid_remote.post("/api/tv/devices/bed/play", json={"playCommand": "PlayNow", "itemIds": [hidden]})
        assert response.status_code == 502 and "not available for your profile" in response.get_json()["body"], hidden
    both = kid_remote.post("/api/tv/devices/bed/play", json={"playCommand": "PlayNow", "itemIds": ["kid", "adult"]})
    assert both.status_code == 502


def test_the_remote_will_play_what_the_limit_allows(kid_remote, tv, monkeypatch):
    from synchub.web import tv as web_tv
    sent = []
    monkeypatch.setattr(tv.devices["bed"], "key", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("synchub.tv.orchestrator.play_deeplink", lambda d, jf, log, item, pos: sent.append(item) or {"ok": True, "steps": ["sent"], "confirmed": True})
    for allowed in ("kid", "tmdbonly", "kidep"):
        response = kid_remote.post("/api/tv/devices/bed/play", json={"playCommand": "PlayNow", "itemIds": [allowed]})
        assert response.status_code == 200 and response.get_json()["ok"] is True, allowed
    assert sent == ["kid", "tmdbonly", "kidep"]


def test_the_playing_now_strip_leaves_out_what_the_limit_hides(app, v2, conn, tv, kid_remote):
    assert kid_remote.get("/api/tv/now").get_json() == {"ok": True, "tvs": []}
    pid = add_profile(conn, "Grown-up", permissions="dashboard,tv")
    assert [t["id"] for t in as_profile(app, pid).get("/api/tv/now").get_json()["tvs"]] == ["bed"]


def test_the_strictest_of_jellyfins_rating_and_tmdbs_applies(v2, conn):
    add_title(conn, "movie", "Both", 77, "R")
    conn.commit()
    assert ratings.of_jellyfin_item({"Type": "Movie", "OfficialRating": "PG", "ProviderIds": {"Tmdb": "77"}}) == 3
    assert ratings.of_jellyfin_item({"Type": "Movie", "OfficialRating": "PG"}) == 1
    assert ratings.of_jellyfin_item({"Type": "Movie", "Name": "nothing known"}) is None
    assert ratings.of_jellyfin_item({"Type": "Episode", "OfficialRating": "TV-Y"}, {"Type": "Series", "OfficialRating": "TV-MA"}) == 3
