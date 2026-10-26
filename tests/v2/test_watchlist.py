"""Each person's own watchlist: seeded from Letterboxd and Serializd, edited in SyncHub, and separate from what drives auto-add."""
from synchub import access, catalog, db, watchlist

from .fakes import FakeTmdb

T0 = "2026-10-20 10:00:00"
USER = "aaaabbbbccccdddd1111222233334444"


def person(conn, name="Ana", letterboxd="ana_lb", serializd=None, jellyfin=None, active=1):
    return conn.execute("INSERT INTO profile (name, letterboxd_username, serializd_username, jellyfin_user_id, is_active) VALUES (?, ?, ?, ?, ?)",
                        (name, letterboxd, serializd, jellyfin, active)).lastrowid


def title(conn, kind, name, tmdb_id):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES (?, ?, ?)", (kind, name, tmdb_id)).lastrowid


def on_letterboxd(conn, username, slug, media_id, list_name="watchlist"):
    conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, title, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)", (username, slug, list_name, slug, T0, T0))
    conn.execute("INSERT OR IGNORE INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'letterboxd', ?, 'tmdb_id', 1.0)", (media_id, slug))


def played_movie(conn, user, media_id, item):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', ?, 'tmdb_id', 1.0)", (media_id, item))
    conn.execute("INSERT INTO src_jellyfin_userdata (jellyfin_user_id, item_id, played, first_seen, last_seen) VALUES (?, ?, 1, ?, ?)", (user, item, T0, T0))


def episode(conn, media_id, number, air_date, user=None, played=False):
    eid = conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date) VALUES (?, 1, ?, ?)", (media_id, number, air_date)).lastrowid
    if played:
        item = f"ep{media_id}x{number}"
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('episode', ?, 'jellyfin_episode', ?, 'tmdb_id', 1.0)", (eid, item))
        conn.execute("INSERT INTO src_jellyfin_userdata (jellyfin_user_id, item_id, played, first_seen, last_seen) VALUES (?, ?, 1, ?, ?)", (user, item, T0, T0))


def listed(conn, pid, hidden=False):
    return {r["title"] for r in watchlist.items(conn, pid, hidden=hidden)}


# ---------------------------------------------------------------- seeding and removal
def test_a_sync_puts_letterboxd_and_serializd_titles_on_the_list(conn):
    pid = person(conn, serializd="ana_sz")
    film, show = title(conn, "movie", "Heat", "949"), title(conn, "show", "Severance", "95396")
    on_letterboxd(conn, "ana_lb", "heat", film)
    conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, title, first_seen, last_seen) VALUES ('ana_sz', 's1', 'Severance', ?, ?)", (T0, T0))
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'serializd', 's1', 'tmdb_id', 1.0)", (show,))
    assert watchlist.seed(conn) == 2
    assert listed(conn, pid) == {"Heat", "Severance"}
    assert {r["title"]: r["source"] for r in watchlist.items(conn, pid)} == {"Heat": "letterboxd", "Severance": "serializd"}
    assert watchlist.seed(conn) == 0


def test_a_removed_title_is_not_added_back_by_the_next_sync(conn):
    pid = person(conn)
    film = title(conn, "movie", "Heat", "949")
    on_letterboxd(conn, "ana_lb", "heat", film)
    watchlist.seed(conn)
    assert watchlist.remove(conn, pid, film) and not watchlist.remove(conn, pid, film)
    assert watchlist.seed(conn) == 0
    assert listed(conn, pid) == set() and listed(conn, pid, hidden=True) == {"Heat"}
    assert watchlist.add(conn, pid, film) == "restored" and watchlist.add(conn, pid, film) == "already"
    assert listed(conn, pid) == {"Heat"}


def test_inactive_people_and_other_people_are_left_alone(conn):
    quiet, other = person(conn, "Quiet", "quiet_lb", active=0), person(conn, "Bo", "bo_lb")
    film = title(conn, "movie", "Heat", "949")
    on_letterboxd(conn, "bo_lb", "heat", film)
    watchlist.seed(conn)
    assert listed(conn, quiet) == set() and listed(conn, other) == {"Heat"}


def test_a_manual_add_stays_when_no_source_lists_it(conn):
    pid = person(conn, letterboxd=None)
    show = title(conn, "show", "Severance", "95396")
    assert watchlist.add(conn, pid, show) == "added"
    watchlist.seed(conn)
    assert {r["title"]: r["source"] for r in watchlist.items(conn, pid)} == {"Severance": "manual"}


def test_the_list_never_changes_what_is_wanted_for_auto_add(conn):
    pid = person(conn)
    film = title(conn, "movie", "Heat", "949")
    on_letterboxd(conn, "ana_lb", "heat", film)
    watchlist.seed(conn)
    before = catalog.demand(conn)
    watchlist.remove(conn, pid, film)
    assert catalog.demand(conn) == before and before[film]["wanted_by"] == ["Ana"]
    other = title(conn, "movie", "Alien", "348")
    watchlist.add(conn, pid, other)
    assert other not in catalog.demand(conn)


# ---------------------------------------------------------------- watched titles
def test_watched_films_and_shows_come_off_only_for_people_who_switched_it_on(conn):
    pid = person(conn, jellyfin=USER)
    seen, unseen, logged = title(conn, "movie", "Heat", "949"), title(conn, "movie", "Alien", "348"), title(conn, "movie", "Ran", "11645")
    done, partial, upcoming = title(conn, "show", "Done", "1"), title(conn, "show", "Partial", "2"), title(conn, "show", "Upcoming", "3")
    for m in (seen, unseen, logged, done, partial, upcoming):
        watchlist.add(conn, pid, m)
    played_movie(conn, USER, seen, "jf-heat")
    on_letterboxd(conn, "ana_lb", "ran", logged, "watched")
    episode(conn, done, 1, "2020-01-01", USER, True)
    episode(conn, done, 2, "2020-01-08", USER, True)
    episode(conn, done, 3, "2999-01-01")                                    # has not aired: does not hold it back
    episode(conn, partial, 1, "2020-01-01", USER, True)
    episode(conn, partial, 2, "2020-01-08")
    episode(conn, upcoming, 1, "2999-01-01")
    assert watchlist.drop_watched(conn, today="2026-10-26") == 0           # off by default
    watchlist.set_drop_watched(conn, pid, True)
    assert watchlist.drop_watched(conn, today="2026-10-26") == 3
    assert listed(conn, pid) == {"Alien", "Partial", "Upcoming"}
    assert {r["hidden_reason"] for r in watchlist.items(conn, pid, hidden=True)} == {"watched"}
    watchlist.seed(conn)
    assert listed(conn, pid) == {"Alien", "Partial", "Upcoming"}


def test_a_show_with_no_aired_episodes_is_never_watched(conn):
    pid = person(conn, jellyfin=USER)
    show = title(conn, "show", "Empty", "9")
    assert watchlist.watched_among(conn, {"id": pid, "jellyfin_user_id": USER, "letterboxd_username": None}, [show]) == set()


# ---------------------------------------------------------------- the pages
def signed_in(client, conn, **kw):
    pid = person(conn, **kw)
    conn.commit()
    client.set_cookie("synchub_profile", str(pid))
    return pid


def test_the_watchlist_page_and_its_buttons(client, conn):
    pid = signed_in(client, conn)
    film = title(conn, "movie", "Heat", "949")
    conn.commit()
    assert client.post("/api/watchlist/add", json={"media_id": film}).get_json()["result"] == "added"
    page = client.get("/watchlist").get_data(as_text=True)
    assert "Heat" in page and "Added here" in page
    assert client.post(f"/api/watchlist/{film}/remove").get_json()["ok"] is True
    assert client.post(f"/api/watchlist/{film}/remove").status_code == 404
    assert "Removed (1)" in client.get("/watchlist").get_data(as_text=True)
    assert client.post(f"/api/watchlist/{film}/restore").get_json()["ok"] is True
    on = client.post("/api/watchlist/settings", json={"drop_watched": True}).get_json()
    assert on["ok"] and "come off" in on["message"]
    with db.connection() as c:
        assert watchlist.drop_watched_enabled(c, pid) and watchlist.on_list(c, pid, film)
    assert client.post("/api/watchlist/settings", json={"drop_watched": "yes"}).status_code == 400
    assert "On your watchlist" in client.get(f"/movie/{film}").get_data(as_text=True)


def test_adding_from_tmdb_creates_the_title_and_requests_nothing(client, conn, monkeypatch):
    pid = signed_in(client, conn)
    tmdb = FakeTmdb(movies={"603": {"id": 603, "title": "The Matrix", "release_date": "1999-03-30"}})
    monkeypatch.setattr("synchub.web.watchlist.TmdbClient", lambda: tmdb)
    got = client.post("/api/watchlist/add", json={"kind": "movie", "tmdb_id": "603"}).get_json()
    assert got["ok"] and got["result"] == "added", got
    with db.connection() as c:
        assert listed(c, pid) == {"The Matrix"} and c.execute("SELECT COUNT(*) FROM manual_request").fetchone()[0] == 0
    assert client.post("/api/watchlist/add", json={"kind": "movie", "tmdb_id": "abc"}).status_code == 400
    assert client.post("/api/watchlist/add", json={"kind": "movie", "tmdb_id": "999"}).status_code == 400


def test_a_guest_must_choose_a_profile_first(client, conn):
    assert client.post("/api/watchlist/add", json={"media_id": 1}).status_code == 403
    assert "Choose your profile" in client.get("/watchlist").get_data(as_text=True)


def test_the_watchlist_endpoints_need_the_watchlist_permission():
    assert access.required("watchlist_page") == "watchlist" == access.required("api_watchlist_add")


def test_refresh_adds_new_titles(v2, conn):
    person(conn)
    film = title(conn, "movie", "Heat", "949")
    on_letterboxd(conn, "ana_lb", "heat", film)
    conn.commit()
    with v2.connection() as c:
        assert watchlist.refresh(c) == {"added": 1, "dropped": 0}
