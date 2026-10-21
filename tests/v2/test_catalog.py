from synchub import catalog, settings

NOW = "2026-01-01 00:00:00"
TODAY = "2026-06-15"


def profile(conn, name, lb=None, sz=None, active=1):
    conn.execute("INSERT INTO profile (name, letterboxd_username, serializd_username, is_active) VALUES (?,?,?,?)", (name, lb, sz, active))


def media(conn, kind, tmdb, title, year=2000, tvdb=None):
    return conn.execute("INSERT INTO media (kind, title, year, tmdb_id, tvdb_id) VALUES (?,?,?,?,?)", (kind, title, year, tmdb, tvdb)).lastrowid


def link(conn, source, key, media_id):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, ?, ?, 'x', 1.0)", (media_id, source, key))


def lb(conn, user, slug, media_id, list_="watchlist", removed=None):
    conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, first_seen, last_seen, removed_at) VALUES (?,?,?,?,?,?)", (user, slug, list_, NOW, NOW, removed))
    conn.execute("INSERT OR IGNORE INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'letterboxd', ?, 'x', 1.0)", (media_id, slug))


def test_demand_rules(conn):
    profile(conn, "Alice", "alice"); profile(conn, "bob", "BOB"); profile(conn, "Off", "off", active=0); profile(conn, "Carol", sz="carol_sz")
    m = media(conn, "movie", "1", "Film")
    lb(conn, "alice", "f", m)
    lb(conn, "bob", "f", m); lb(conn, "bob", "f", m, "watched")            # bob has watched it: no longer wants it
    lb(conn, "off", "f", m)                                                # inactive profile does not count
    sh = media(conn, "show", "2", "Show", tvdb="9")
    conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, first_seen, last_seen) VALUES ('carol_sz', 's1', ?, ?)", (NOW, NOW)); link(conn, "serializd", "s1", sh)
    got = catalog.demand(conn)
    assert got[m] == {"wanted_by": ["Alice"], "watched_by": ["bob"]}
    assert got[sh] == {"wanted_by": ["Carol"], "watched_by": []}


def test_a_title_removed_from_the_watchlist_is_kept_but_not_wanted(conn):
    profile(conn, "Alice", "alice")
    m = media(conn, "movie", "1", "Film")
    lb(conn, "alice", "f", m, removed=NOW)
    assert catalog.requesters_of(conn, m) == {"wanted_by": [], "watched_by": []}
    assert [r["title"] for r in catalog.list_catalog(conn, TODAY)] == ["Film"]        # still listed


def test_list_catalog_movies_and_shows(conn):
    settings.set_value("jellyfin_url", "http://jf:8096/", conn=conn)
    conn.commit()
    profile(conn, "Alice", "alice")
    movie_ok, movie_dl, movie_none = media(conn, "movie", "1", "Alpha"), media(conn, "movie", "2", "Beta"), media(conn, "movie", "3", "Gamma")
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES ('jf1', 'movie', ?, ?)", (NOW, NOW)); link(conn, "jellyfin", "jf1", movie_ok)
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (movie_ok,))
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code, progress) VALUES ('movie', ?, 'downloading', 0.426)", (movie_dl,))
    lb(conn, "alice", "beta", movie_dl)
    show = media(conn, "show", "10", "Delta", tvdb="99")
    for n, state, air in ((1, "in_library", "2020-01-01"), (2, "missing", "2020-01-08"), (3, "missing", "2099-01-01")):
        eid = conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date) VALUES (?, 1, ?, ?)", (show, n, air)).lastrowid
        conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('episode', ?, ?)", (eid, state))
    conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date) VALUES (?, 0, 1, '2010-01-01')", (show,))      # a special: ignored in the rollup
    rows = {r["title"]: r for r in catalog.list_catalog(conn, TODAY)}
    assert list(rows) == ["Alpha", "Beta", "Delta", "Gamma"]
    a = rows["Alpha"]
    assert (a["state"], a["state_label"], a["in_jellyfin"], a["url"], a["poster"]) == ("in_library", "In Library", True, f"/movie/{movie_ok}", f"/poster/{movie_ok}")
    assert a["jellyfin_link"] == "http://jf:8096/web/index.html#/details?id=jf1" and a["jellyfin_app_link"] == "jellyfin://details?id=jf1"
    b = rows["Beta"]
    assert (b["state"], b["progress_pct"], b["wanted_by"], b["user_count"], b["jellyfin_link"]) == ("downloading", 43, ["Alice"], 1, None)
    assert rows["Gamma"]["state"] == "missing"
    d = rows["Delta"]
    assert (d["kind"], d["state"], d["state_label"], d["episodes"], d["aired"], d["in_library"], d["percent"], d["url"]) == ("show", "partial", "Partial", 3, 2, 1, 50, f"/show/{show}")
    stats = catalog.dashboard_stats(list(rows.values()))
    assert stats == {"total": 4, "movies": 3, "shows": 1, "in_library": 1, "in_flight": 1, "queued": 0, "missing": 1, "partial": 1, "users": 1}


def test_removed_jellyfin_item_is_not_a_library_link(conn):
    m = media(conn, "movie", "1", "Gone")
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen, removed_at) VALUES ('jf1', 'movie', ?, ?, ?)", (NOW, NOW, NOW)); link(conn, "jellyfin", "jf1", m)
    row = catalog.list_catalog(conn, TODAY)[0]
    assert row["in_jellyfin"] is False and row["jellyfin_link"] is None


def test_media_detail(conn):
    profile(conn, "Alice", "alice")
    m = media(conn, "movie", "1", "Film")
    lb(conn, "alice", "f", m)
    detail = catalog.media_detail(conn, m)
    assert detail["title"] == "Film" and detail["wanted_by"] == ["Alice"] and detail["in_jellyfin"] is False
    assert catalog.media_detail(conn, 999) is None


def test_unmatched_sources_and_pending_reviews(conn):
    m = media(conn, "movie", "1", "Linked")
    lb_ = lambda slug, title, user="alice": conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, title, year, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)", (user, slug, "watchlist", title, 1999, NOW, NOW))
    lb_("linked", "Linked"); link(conn, "letterboxd", "linked", m)
    lb_("mystery", "Mystery", "alice"); lb_("mystery", "Mystery", "bob")
    conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, title, first_seen, last_seen) VALUES ('carol', 's9', 'Some Show', ?, ?)", (NOW, NOW))
    unmatched = catalog.unmatched_sources(conn)
    assert [(u["source"], u["key"], u["kind"], sorted(u["users"].split(","))) for u in unmatched] == [("letterboxd", "mystery", "movie", ["alice", "bob"]), ("serializd", "s9", "show", ["carol"])]

    cand = media(conn, "movie", "5", "Mystery Candidate")
    conn.execute("INSERT INTO match_review (source, source_key, source_title, kind, candidate_media_id, candidate_tmdb_id, confidence, reason) VALUES ('letterboxd', 'mystery', 'Mystery', 'movie', ?, '5', 0.7, 'year')", (cand,))
    conn.execute("INSERT INTO match_review (source, source_key, source_title, kind, candidate_tmdb_id, confidence, reason) VALUES ('letterboxd', 'mystery', 'Mystery', 'movie', '6', 0.85, 'search')")
    conn.execute("INSERT INTO match_review (source, source_key, source_title, kind, candidate_tmdb_id, confidence, reason, status) VALUES ('letterboxd', 'old', 'Old', 'movie', '7', 0.9, 'x', 'dismissed')")
    reviews = catalog.pending_reviews(conn)
    assert len(reviews) == 1 and reviews[0]["key"] == "mystery"
    assert [c["candidate_tmdb_id"] for c in reviews[0]["candidates"]] == ["6", "5"]                  # strongest first
    assert reviews[0]["candidates"][1]["candidate_title"] == "Mystery Candidate"
