import re

import pytest

from synchub import settings, tasks
from synchub.clients import ApiError, ConfigError

from . import test_end_to_end as e2e
from .fakes import FakeTmdb
from .test_search import FakeRadarr, FakeSonarr, movie_hit, show_hit


@pytest.fixture
def tmdb(monkeypatch):
    import synchub.web.api as api
    import synchub.web.pages as pages
    fake = FakeTmdb(search={("movie", "heat"): [movie_hit(949, "Heat"), movie_hit(2, "Heat <b>2</b> \"Quoted\"", "2001-01-01")], ("tv", "heat"): [show_hit(60802, "The Last Ship")]},
                    movies={"949": {"id": 949, "title": "Heat", "release_date": "1995-12-15"}},
                    shows={"60802": e2e.LAST_SHIP}, seasons={"60802": e2e.LAST_SHIP_SEASONS})
    monkeypatch.setattr(pages, "TmdbClient", lambda: fake)
    monkeypatch.setattr(api, "TmdbClient", lambda: fake)
    return fake


@pytest.fixture
def refreshed(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "refresh_soon", lambda service, job, **kw: calls.append(service))
    return calls


def card(page, tmdb_id, kind):
    return re.search(r'<div class="panel h-100[^>]*data-kind="%s" data-tmdb-id="%s".*?data-result role="status"></div>' % (kind, tmdb_id), page, re.S).group(0)


# ---------------------------------------------------------------- the page
def test_search_page_shows_the_form_and_is_in_the_nav(client, v2):
    page = client.get("/search").get_data(as_text=True)
    assert 'name="q"' in page and 'name="type"' in page and 'href="/search"' in page and "Find &amp; request" in page
    assert 'id="results"' not in page


def test_results_show_movies_then_shows_with_escaped_titles_and_the_right_buttons(client, v2, tmdb):
    page = client.get("/search?q=heat").get_data(as_text=True)
    heat, last_ship = card(page, 949, "movie"), card(page, 60802, "show")
    assert "Heat" in heat and "(1995)" in heat and "Add to Radarr" in heat and "7.8" in heat
    assert "Add to Sonarr..." in last_ship and "Add to Radarr" not in last_ship
    assert page.index('data-tmdb-id="949"') < page.index('data-tmdb-id="60802"')
    assert "&lt;b&gt;2&lt;/b&gt;" in page and "<b>2</b>" not in page and 'data-title="Heat &lt;b&gt;2&lt;/b&gt; &#34;Quoted&#34;"' in page
    assert 'value="heat"' in page and "https://image.tmdb.org/t/p/w185/949.jpg" in page


def test_type_filters_and_unknown_types_fall_back_to_everything(client, v2, tmdb):
    assert 'data-kind="show"' not in client.get("/search?q=heat&type=movie").get_data(as_text=True)
    assert 'data-kind="movie"' not in client.get("/search?q=heat&type=show").get_data(as_text=True)
    both = client.get("/search?q=heat&type=nonsense").get_data(as_text=True)
    assert 'data-kind="movie"' in both and 'data-kind="show"' in both and re.search(r'<option value="all" selected>', both)


def test_titles_already_in_syncHub_show_their_state_not_an_add_button(client, v2, tmdb):
    with v2.connection() as conn:
        mid = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Heat', '949')").lastrowid
        conn.execute("INSERT INTO item_state (target_type, target_id, state_code, progress) VALUES ('movie', ?, 'downloading', 0.42)", (mid,))
    heat = card(client.get("/search?q=heat").get_data(as_text=True), 949, "movie")
    assert "Downloading 42%" in heat and f'href="/movie/{mid}"' in heat and "Add to Radarr" not in heat


def test_empty_results_and_failures_are_explained(client, v2, tmdb, monkeypatch):
    import synchub.web.pages as pages
    assert "Nothing found for" in client.get("/search?q=zzzz").get_data(as_text=True)
    monkeypatch.setattr(pages, "TmdbClient", lambda: (_ for _ in ()).throw(ConfigError("TMDB API key is not configured.")))
    page = client.get("/search?q=heat").get_data(as_text=True)
    assert "Add your TMDB API key in Settings" in page and 'href="/settings"' in page
    monkeypatch.setattr(pages, "TmdbClient", lambda: type("T", (), {"search_movie": lambda s, *a: (_ for _ in ()).throw(ApiError("TMDB /search/movie failed: HTTP 401"))})())
    assert "TMDB would not search right now: TMDB /search/movie failed: HTTP 401" in client.get("/search?q=heat").get_data(as_text=True)
    monkeypatch.setattr(pages, "TmdbClient", lambda: type("T", (), {"search_movie": lambda s, *a: (_ for _ in ()).throw(OSError("down"))})())
    assert "Could not reach TMDB." in client.get("/search?q=heat").get_data(as_text=True)


# ---------------------------------------------------------------- adding
def test_adding_a_movie_credits_the_profile_in_the_cookie(client, v2, tmdb, refreshed, monkeypatch):
    import synchub.web.api as api
    radarr = FakeRadarr()
    monkeypatch.setattr(api, "RadarrClient", lambda: radarr)
    with v2.connection() as conn:
        alice = conn.execute("INSERT INTO profile (name) VALUES ('Alice')").lastrowid
    client.set_cookie("synchub_profile", str(alice))
    r = client.post("/api/search/add/movie", json={"tmdb_id": "949"}).get_json()
    assert r["ok"] and r["message"] == "Sent 'Radarr 949' to Radarr. Credited to Alice." and r["url"] == f"/movie/{r['media_id']}"
    assert radarr.added == [(949, 1, "/movies")] and refreshed == ["radarr"]
    with v2.connection() as conn:
        assert conn.execute("SELECT profile_id FROM manual_request").fetchone()[0] == alice


def test_a_guest_adds_without_credit_and_is_told_how_to_get_it(client, v2, tmdb, refreshed, monkeypatch):
    import synchub.web.api as api
    monkeypatch.setattr(api, "RadarrClient", lambda: FakeRadarr())
    r = client.post("/api/search/add/movie", json={"tmdb_id": 949}).get_json()
    assert r["ok"] and "Not credited to anyone" in r["message"]
    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM manual_request").fetchone()[0] == 0


def test_add_failures_are_reported_and_leave_nothing(client, v2, tmdb, refreshed, monkeypatch):
    import synchub.web.api as api
    monkeypatch.setattr(api, "RadarrClient", lambda: FakeRadarr(reject="Radarr AddMovie failed: HTTP 400 nope"))
    r = client.post("/api/search/add/movie", json={"tmdb_id": "949"})
    assert r.status_code == 400 and "HTTP 400 nope" in r.get_json()["message"] and refreshed == []
    for bad in ({}, {"tmdb_id": "abc"}, {"tmdb_id": ""}, {"tmdb_id": "12; DROP"}):
        assert client.post("/api/search/add/movie", json=bad).status_code == 400, bad
    assert client.post("/api/search/add/movie", json={"tmdb_id": "404"}).get_json()["message"] == "TMDB does not know that movie."
    monkeypatch.setattr(api, "TmdbClient", lambda: (_ for _ in ()).throw(ConfigError("TMDB API key is not configured.")))
    assert client.post("/api/search/add/movie", json={"tmdb_id": "949"}).get_json()["message"] == "TMDB API key is not configured."
    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_the_season_picker_gets_its_choices_from_tmdb(client, v2, tmdb):
    r = client.get("/api/search/show/60802/seasons").get_json()
    assert r["ok"] and r["title"] == "The Last Ship" and r["has_tvdb"] is True
    assert [s["number"] for s in r["seasons"]] == [1, 3, 0]                                       # Specials last
    assert client.get("/api/search/show/999/seasons").status_code == 400


def test_adding_a_show_sends_the_chosen_seasons(client, v2, tmdb, refreshed, monkeypatch):
    import synchub.web.api as api
    sonarr = FakeSonarr()
    monkeypatch.setattr(api, "SonarrClient", lambda: sonarr)
    r = client.post("/api/search/add/show", json={"tmdb_id": "60802", "seasons": [1, 3]}).get_json()
    assert r["ok"] and r["message"].startswith("Sent 'The Last Ship' in Sonarr (seasons 1, 3).") and r["url"] == f"/show/{r['media_id']}"
    assert sonarr.added == [(269533, [1, 3], 1, "/tv")] and refreshed == ["sonarr"]


def test_adding_a_show_rejects_bad_requests(client, v2, tmdb, refreshed, monkeypatch):
    import synchub.web.api as api
    monkeypatch.setattr(api, "SonarrClient", lambda: FakeSonarr())
    for body in ({"tmdb_id": "60802"}, {"tmdb_id": "60802", "seasons": "1"}, {"tmdb_id": "60802", "seasons": [1, "2"]}, {"tmdb_id": "60802", "seasons": [True]},
                 {"tmdb_id": "x", "seasons": [1]}, {"tmdb_id": "60802", "seasons": []}):
        assert client.post("/api/search/add/show", json=body).status_code == 400, body
    assert refreshed == []
    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_a_requested_title_appears_on_the_dashboard_as_wanted_by_the_requester(client, v2, tmdb, refreshed, monkeypatch):
    import synchub.web.api as api
    monkeypatch.setattr(api, "RadarrClient", lambda: FakeRadarr())
    with v2.connection() as conn:
        alice = conn.execute("INSERT INTO profile (name) VALUES ('Alice')").lastrowid
    client.set_cookie("synchub_profile", str(alice))
    client.post("/api/search/add/movie", json={"tmdb_id": "949"})
    row = re.search(r'<tr class="item-row[^>]*data-title="heat"[^>]*>', client.get("/").get_data(as_text=True)).group(0)
    assert 'data-added-by="Alice"' in row and 'data-user-count="1"' in row
