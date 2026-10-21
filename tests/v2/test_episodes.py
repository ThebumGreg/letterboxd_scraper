from synchub import episodes

NOW = "2026-01-01 00:00:00"


def make_show(conn, tmdb="100"):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'Show', ?)", (tmdb,)).lastrowid


def add_episode(conn, media_id, s, e, title, air=None):
    return conn.execute("INSERT INTO episode (media_id, season_number, episode_number, title, air_date) VALUES (?,?,?,?,?)", (media_id, s, e, title, air)).lastrowid


def link_series(conn, source, key, media_id):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, ?, ?, 'tmdb_id', 1.0)", (media_id, source, key))


def jf_ep(conn, item_id, series_id, s, e, name="Ep", premiere=None):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, series_id, name, season_number, episode_number, premiere_date, first_seen, last_seen) VALUES (?, 'episode', ?, ?, ?, ?, ?, ?, ?)",
                 (item_id, series_id, name, s, e, premiere, NOW, NOW))


def sonarr_ep(conn, eid, sonarr_id, s, e, title="Ep", air=None, tvdb_ep=None):
    conn.execute("INSERT INTO src_sonarr_episode (sonarr_episode_id, sonarr_id, season_number, episode_number, title, air_date, tvdb_episode_id, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                 (eid, sonarr_id, s, e, title, air, tvdb_ep, NOW, NOW))


def ep_links(conn, source):
    return {r["source_key"]: (r["entity_id"], r["method"], r["confidence"]) for r in conn.execute("SELECT * FROM link WHERE source = ? AND entity_type = 'episode'", (source,))}


def test_jellyfin_episodes_link_through_the_series_not_through_ids(conn):
    mid = make_show(conn)
    e1 = add_episode(conn, mid, 3, 10, "Scuttle")
    sp = add_episode(conn, mid, 0, 1, "Featurette")
    link_series(conn, "jellyfin", "ser1", mid)
    jf_ep(conn, "ep1", "ser1", 3, 10)
    jf_ep(conn, "sp1", "ser1", 0, 1)
    stats = episodes.link_jellyfin_episodes(conn, NOW)
    assert stats == {"linked": 2}
    assert ep_links(conn, "jellyfin_episode") == {"ep1": (e1, "series+season_episode", 1.0), "sp1": (sp, "series+season_episode", 1.0)}


def test_episodes_of_a_series_are_not_matched_to_another_show(conn):
    a, b = make_show(conn, "1"), make_show(conn, "2")
    add_episode(conn, a, 1, 1, "A1")
    eb = add_episode(conn, b, 1, 1, "B1")
    link_series(conn, "jellyfin", "serA", a)
    link_series(conn, "jellyfin", "serB", b)
    jf_ep(conn, "x", "serB", 1, 1)
    episodes.link_jellyfin_episodes(conn, NOW)
    assert ep_links(conn, "jellyfin_episode")["x"][0] == eb


def test_jellyfin_episode_of_unlinked_series_is_counted(conn):
    jf_ep(conn, "x", "unknown-series", 1, 1)
    assert episodes.link_jellyfin_episodes(conn, NOW) == {"series_not_linked": 1}


def test_jellyfin_numbering_difference_falls_back_to_air_date_and_title(conn):
    mid = make_show(conn)
    target = add_episode(conn, mid, 2, 5, "The Big Finale", "2020-05-05")
    add_episode(conn, mid, 2, 6, "Something Else", "2020-05-12")
    link_series(conn, "jellyfin", "ser1", mid)
    jf_ep(conn, "x", "ser1", 1, 99, name="Big Finale", premiere="2020-05-05")
    jf_ep(conn, "y", "ser1", 7, 7, name="No such episode", premiere="2001-01-01")
    stats = episodes.link_jellyfin_episodes(conn, NOW)
    assert stats == {"linked_by_date": 1, "no_tmdb_episode": 1}
    assert ep_links(conn, "jellyfin_episode")["x"] == (target, "air_date+title", 0.8)


def test_relinking_is_idempotent_and_keeps_manual_links(conn):
    mid = make_show(conn)
    e1 = add_episode(conn, mid, 1, 1, "One")
    e2 = add_episode(conn, mid, 1, 2, "Two")
    link_series(conn, "jellyfin", "ser1", mid)
    jf_ep(conn, "x", "ser1", 1, 1)
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence, status) VALUES ('episode', ?, 'jellyfin_episode', 'x', 'manual', 1.0, 'manual')", (e2,))
    episodes.link_jellyfin_episodes(conn, NOW)
    episodes.link_jellyfin_episodes(conn, NOW)
    assert ep_links(conn, "jellyfin_episode")["x"][0] == e2 and e1 != e2


def test_sonarr_same_numbering_is_confirmed_by_air_date(conn):
    mid = make_show(conn)
    t = add_episode(conn, mid, 1, 1, "Pilot", "2010-09-01")
    link_series(conn, "sonarr", "10", mid)
    sonarr_ep(conn, "100", "10", 1, 1, "Pilot", "2010-09-02", tvdb_ep="555")      # a day apart is fine (time zones)
    assert episodes.map_sonarr_episodes(conn, NOW) == {"same_numbering": 1}
    row = conn.execute("SELECT * FROM episode WHERE id = ?", (t,)).fetchone()
    assert (row["tvdb_episode_id"], row["tvdb_season_number"], row["tvdb_episode_number"], row["numbering_status"]) == ("555", 1, 1, "same")
    assert ep_links(conn, "sonarr_episode")["100"] == (t, "season_episode+air_date", 0.95)


def test_sonarr_without_air_dates_links_with_lower_confidence(conn):
    mid = make_show(conn)
    t = add_episode(conn, mid, 1, 1, "Pilot")
    link_series(conn, "sonarr", "10", mid)
    sonarr_ep(conn, "100", "10", 1, 1, "Pilot")
    episodes.map_sonarr_episodes(conn, NOW)
    assert ep_links(conn, "sonarr_episode")["100"] == (t, "season_episode", 0.85)


def test_sonarr_different_numbering_is_mapped_by_air_date_and_title(conn):
    """TMDB has one long season 1; TVDB (Sonarr) splits it into seasons 1 and 2."""
    mid = make_show(conn)
    t1 = add_episode(conn, mid, 1, 1, "Beginnings", "2015-01-01")
    t2 = add_episode(conn, mid, 1, 13, "Second Half Opener", "2015-06-01")
    link_series(conn, "sonarr", "10", mid)
    sonarr_ep(conn, "a", "10", 1, 1, "Beginnings", "2015-01-01")
    sonarr_ep(conn, "b", "10", 2, 1, "Second Half Opener", "2015-06-01")
    stats = episodes.map_sonarr_episodes(conn, NOW)
    assert stats == {"same_numbering": 1, "mapped": 1}
    row = conn.execute("SELECT * FROM episode WHERE id = ?", (t2,)).fetchone()
    assert (row["tvdb_season_number"], row["tvdb_episode_number"], row["numbering_status"]) == (2, 1, "mapped")
    assert ep_links(conn, "sonarr_episode")["b"] == (t2, "air_date+title", 0.8)


def test_sonarr_same_numbers_but_different_date_is_not_trusted(conn):
    mid = make_show(conn)
    add_episode(conn, mid, 1, 1, "Alpha", "2015-01-01")
    add_episode(conn, mid, 1, 5, "Totally Different", "2016-03-03")
    link_series(conn, "sonarr", "10", mid)
    sonarr_ep(conn, "a", "10", 1, 1, "Another Episode", "2020-01-01")       # S1E1 exists in TMDB but aired years apart
    stats = episodes.map_sonarr_episodes(conn, NOW)
    assert stats == {"unmapped": 1} and ep_links(conn, "sonarr_episode") == {}


def test_sonarr_ambiguous_date_is_left_unmapped(conn):
    mid = make_show(conn)
    add_episode(conn, mid, 1, 1, "Part One", "2015-01-01")
    add_episode(conn, mid, 1, 2, "Part Two", "2015-01-01")
    link_series(conn, "sonarr", "10", mid)
    sonarr_ep(conn, "a", "10", 5, 5, "Part", "2015-01-01")
    assert episodes.map_sonarr_episodes(conn, NOW) == {"unmapped": 1}


def test_removed_sonarr_episodes_are_ignored(conn):
    mid = make_show(conn)
    add_episode(conn, mid, 1, 1, "Pilot", "2010-09-01")
    link_series(conn, "sonarr", "10", mid)
    sonarr_ep(conn, "100", "10", 1, 1, "Pilot", "2010-09-01")
    conn.execute("UPDATE src_sonarr_episode SET removed_at = ?", (NOW,))
    assert episodes.map_sonarr_episodes(conn, NOW) == {}


def test_titles_similar():
    assert episodes.titles_similar("The Big Finale", "Big Finale")
    assert episodes.titles_similar("Part 1: The Start", "Part 1 - The Start")
    assert not episodes.titles_similar("Pilot", "Finale") and not episodes.titles_similar("", "x")
