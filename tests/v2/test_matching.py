import pytest

from synchub import matching
from synchub.matching import Matcher

from .fakes import FakeTmdb

NOW = "2026-01-01 00:00:00"


def add_jf(conn, item_id, type_, name, year=None, tmdb=None, tvdb=None, imdb=None):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, tmdb_id, tvdb_id, imdb_id, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                 (item_id, type_, name, year, tmdb, tvdb, imdb, NOW, NOW))


def add_lb(conn, slug, title, year=None, tmdb=None, user="alice", list_="watchlist"):
    conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, title, year, tmdb_id, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                 (user, slug, list_, title, year, tmdb, NOW, NOW))


def add_sonarr(conn, sid, title, tvdb, year=None, tmdb=None):
    conn.execute("INSERT INTO src_sonarr_series (sonarr_id, tvdb_id, tmdb_id, title, year, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)", (sid, tvdb, tmdb, title, year, NOW, NOW))


def links(conn):
    return {(r["source"], r["source_key"]): (r["entity_id"], r["method"], r["confidence"], r["status"]) for r in conn.execute("SELECT * FROM link")}


def reviews(conn, status="pending"):
    return [dict(r) for r in conn.execute("SELECT * FROM match_review WHERE status = ? ORDER BY id", (status,))]


def media(conn):
    return {r["tmdb_id"]: dict(r) for r in conn.execute("SELECT * FROM media")}


def run(conn, tmdb=None):
    matcher = Matcher(conn, tmdb, NOW)
    return matcher.resolve_all()


def test_tmdb_id_creates_media_and_links_every_source_to_it(conn):
    add_jf(conn, "jf1", "movie", "Heat", 1995, tmdb="949")
    conn.execute("INSERT INTO src_radarr_movie (radarr_id, tmdb_id, title, year, first_seen, last_seen) VALUES ('7', '949', 'Heat', 1995, ?, ?)", (NOW, NOW))
    add_lb(conn, "heat-1995", "Heat", 1995, tmdb="949")
    run(conn)
    m = media(conn)["949"]
    assert (m["kind"], m["title"], m["year"]) == ("movie", "Heat", 1995)
    got = links(conn)
    assert got[("jellyfin", "jf1")][:3] == (m["id"], "tmdb_id", 1.0)
    assert got[("radarr", "7")][0] == got[("letterboxd", "heat-1995")][0] == m["id"]
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_movie_and_show_with_the_same_tmdb_number_are_different_media(conn):
    add_jf(conn, "jf1", "movie", "Movie", 2000, tmdb="1")
    add_jf(conn, "jf2", "series", "Show", 2000, tmdb="1")
    run(conn)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2


def test_letterboxd_film_without_id_links_to_same_title_and_year_in_jellyfin(conn):
    """Toy Story 4: one Jellyfin item with a TMDB ID, one Letterboxd entry still without."""
    add_jf(conn, "jf1", "movie", "Toy Story 4", 2019, tmdb="301528")
    add_lb(conn, "toy-story-4", "Toy Story 4", 2019)
    run(conn)
    assert links(conn)[("letterboxd", "toy-story-4")][1:3] == ("title_year", 0.9)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_dash_variants_and_articles_do_not_block_a_match(conn):
    add_jf(conn, "jf1", "movie", "The Twilight Saga: Breaking Dawn - Part 1", 2011, tmdb="50619")
    add_lb(conn, "the-twilight-saga-breaking-dawn-part-1", "The Twilight Saga: Breaking Dawn – Part 1", 2011)
    run(conn)
    assert ("letterboxd", "the-twilight-saga-breaking-dawn-part-1") in links(conn)


def test_same_title_different_year_is_never_linked_automatically(conn):
    """Alice in Wonderland 1999 (Jellyfin) vs the 1951 film on a watchlist; The Jungle Book 2016 vs 1967."""
    add_jf(conn, "jf1", "movie", "Alice in Wonderland", 1999, tmdb="30923")
    add_jf(conn, "jf2", "movie", "The Jungle Book", 2016, tmdb="278927")
    add_lb(conn, "alice-in-wonderland-1951", "Alice in Wonderland", 1951)
    add_lb(conn, "the-jungle-book-1967", "The Jungle Book", 1967)
    stats = run(conn)
    assert ("letterboxd", "alice-in-wonderland-1951") not in links(conn)
    assert ("letterboxd", "the-jungle-book-1967") not in links(conn)
    assert reviews(conn) == [] and stats["unresolved"] == 2


def test_remake_with_exact_year_wins_over_namesake(conn):
    add_jf(conn, "jf1", "movie", "The Jungle Book", 2016, tmdb="278927")
    add_jf(conn, "jf2", "movie", "The Jungle Book", 1967, tmdb="9325")
    add_lb(conn, "the-jungle-book-1967", "The Jungle Book", 1967)
    run(conn)
    assert links(conn)[("letterboxd", "the-jungle-book-1967")][0] == media(conn)["9325"]["id"]


def test_year_off_by_one_goes_to_review_not_auto(conn):
    add_jf(conn, "jf1", "movie", "Some Film", 2019, tmdb="5")
    add_lb(conn, "some-film", "Some Film", 2020)
    run(conn)
    assert ("letterboxd", "some-film") not in links(conn)
    r = reviews(conn)
    assert len(r) == 1 and r[0]["confidence"] == 0.7 and r[0]["candidate_tmdb_id"] == "5" and r[0]["kind"] == "movie"


def test_title_without_year_only_suggests(conn):
    add_jf(conn, "jf1", "movie", "Some Film", 2019, tmdb="5")
    add_lb(conn, "some-film", "Some Film", None)
    run(conn)
    assert ("letterboxd", "some-film") not in links(conn) and reviews(conn)[0]["confidence"] == 0.6


def test_conflicting_ids_block_a_title_match(conn):
    add_jf(conn, "jf1", "series", "Show", 2010, tmdb="1", tvdb="10")
    add_sonarr(conn, "s1", "Show", "99", 2010)           # same name and year, but a different TVDB ID
    run(conn, FakeTmdb())                                 # TMDB knows nothing about TVDB 99
    assert ("sonarr", "s1") not in links(conn)


def test_sonarr_tvdb_only_show_resolves_to_existing_show_via_tvdb_id(conn):
    """Special Ops: Lioness (Jellyfin, both IDs) and Lioness (Sonarr, TVDB only)."""
    add_jf(conn, "jf1", "series", "Special Ops: Lioness", 2023, tmdb="113962", tvdb="388589")
    add_sonarr(conn, "s1", "Lioness", "388589", 2023)
    run(conn)       # no TMDB needed: the TVDB ID is already known locally
    assert links(conn)[("sonarr", "s1")][1:3] == ("tvdb_id", 1.0)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_sonarr_tvdb_only_show_resolves_through_tmdb_find(conn):
    add_sonarr(conn, "s1", "Daria", "77996", 1997)
    tmdb = FakeTmdb(find={("77996", "tvdb_id"): {"tv_results": [{"id": 2283, "name": "Daria", "first_air_date": "1997-03-03"}]}})
    run(conn, tmdb)
    m = media(conn)["2283"]
    assert (m["title"], m["year"], m["tvdb_id"]) == ("Daria", 1997, "77996")
    assert links(conn)[("sonarr", "s1")][1:3] == ("tvdb_via_tmdb_find", 0.95)


def test_sonarr_title_year_disambiguates_doctor_who(conn):
    add_jf(conn, "jf1", "series", "Doctor Who", 1963, tmdb="57243")
    add_sonarr(conn, "s1", "Doctor Who (2023)", "449991", 2024)
    tmdb = FakeTmdb(find={("449991", "tvdb_id"): {"tv_results": [{"id": 196322, "name": "Doctor Who", "first_air_date": "2023-11-25"}]}})
    run(conn, tmdb)
    assert links(conn)[("sonarr", "s1")][0] == media(conn)["196322"]["id"] != media(conn)["57243"]["id"]


def test_tmdb_outage_leaves_record_unlinked_and_retries_later(conn):
    add_sonarr(conn, "s1", "Daria", "77996", 1997)
    stats = run(conn, FakeTmdb(fail=True))
    assert stats["tmdb_unavailable"] == 1 and links(conn) == {}
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0           # no half-made media
    run(conn, FakeTmdb(find={("77996", "tvdb_id"): {"tv_results": [{"id": 2283, "name": "Daria", "first_air_date": "1997-03-03"}]}}))
    assert ("sonarr", "s1") in links(conn)


def test_without_a_tmdb_key_id_sources_still_link_and_tvdb_only_waits(conn):
    add_jf(conn, "jf1", "movie", "Heat", 1995, tmdb="949")
    add_sonarr(conn, "s1", "Daria", "77996", 1997)
    run(conn, None)
    assert ("jellyfin", "jf1") in links(conn) and ("sonarr", "s1") not in links(conn)


def test_conflicting_tvdb_id_is_linked_by_tmdb_and_raised_for_review(conn):
    add_jf(conn, "jf1", "series", "Show", 2010, tmdb="1", tvdb="10")
    conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, title, tmdb_id, tvdb_id, first_seen, last_seen) VALUES ('a', '7', 'Show', '1', '11', ?, ?)", (NOW, NOW))
    run(conn)
    assert links(conn)[("serializd", "7")][0] == media(conn)["1"]["id"]
    assert "TVDB ID 11" in reviews(conn)[0]["reason"]


def test_tmdb_search_suggests_but_never_auto_links(conn):
    add_lb(conn, "obscure-film", "Obscure Film", 2001)
    tmdb = FakeTmdb(search={("movie", "Obscure Film"): [
        {"id": 11, "title": "Obscure Film", "release_date": "2001-05-05"},
        {"id": 12, "title": "Obscure Film", "release_date": "1950-01-01"},
        {"id": 13, "title": "Different Name", "release_date": "2001-05-05"}]})
    run(conn, tmdb)
    assert links(conn) == {} and conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    r = reviews(conn)
    assert [(x["candidate_tmdb_id"], x["confidence"]) for x in r] == [("11", 0.85)]


def test_search_is_not_repeated_within_a_week(conn):
    add_lb(conn, "nothing-found", "Nothing Found", 2001)
    tmdb = FakeTmdb()
    run(conn, tmdb)
    run(conn, tmdb)
    assert [c for c in tmdb.calls if c[0] == "search_movie"] == [("search_movie", "Nothing Found", 2001)]
    later = Matcher(conn, tmdb, "2026-01-09 00:00:00")
    later.resolve_all()
    assert len([c for c in tmdb.calls if c[0] == "search_movie"]) == 2


def test_approving_a_review_creates_media_and_a_manual_link(conn):
    add_lb(conn, "obscure-film", "Obscure Film", 2001)
    run(conn, FakeTmdb(search={("movie", "Obscure Film"): [{"id": 11, "title": "Obscure Film", "release_date": "2001-05-05"}]}))
    review_id = reviews(conn)[0]["id"]
    media_id = matching.approve_review(conn, review_id)
    assert media(conn)["11"]["id"] == media_id
    assert links(conn)[("letterboxd", "obscure-film")] == (media_id, "manual", 1.0, "manual")
    assert reviews(conn) == [] and reviews(conn, "approved")[0]["id"] == review_id
    run(conn, FakeTmdb())      # a later sync does not touch the manual link
    assert links(conn)[("letterboxd", "obscure-film")][3] == "manual"


def test_dismissed_suggestion_does_not_come_back(conn):
    add_jf(conn, "jf1", "movie", "Some Film", 2019, tmdb="5")
    add_lb(conn, "some-film", "Some Film", 2020)
    run(conn)
    assert matching.dismiss_review(conn, reviews(conn)[0]["id"])
    run(conn)
    assert reviews(conn) == [] and len(reviews(conn, "dismissed")) == 1


def test_manual_link_overrides_and_survives(conn):
    add_jf(conn, "jf1", "movie", "Heat", 1995, tmdb="949")
    add_lb(conn, "heat-1995", "Heat", 1995, tmdb="949")
    run(conn)
    mid = matching.link_manually(conn, "letterboxd", "heat-1995", "movie", "1234", "Heat (other)", 1995)
    run(conn)
    assert links(conn)[("letterboxd", "heat-1995")] == (mid, "manual", 1.0, "manual")


def test_better_evidence_replaces_a_weaker_automatic_link(conn):
    add_jf(conn, "jf1", "movie", "Toy Story 4", 2019, tmdb="301528")
    add_lb(conn, "toy-story-4", "Toy Story 4", 2019)
    run(conn)
    assert links(conn)[("letterboxd", "toy-story-4")][1] == "title_year"
    conn.execute("UPDATE src_letterboxd_entry SET tmdb_id = '301528'")      # the film-page resolver found the ID
    run(conn)
    assert links(conn)[("letterboxd", "toy-story-4")][1:3] == ("tmdb_id", 1.0)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_entries_removed_from_a_list_stay_linked(conn):
    add_lb(conn, "heat-1995", "Heat", 1995, tmdb="949")
    conn.execute("UPDATE src_letterboxd_entry SET removed_at = ?", (NOW,))
    run(conn)
    assert ("letterboxd", "heat-1995") in links(conn)


def test_resolve_is_idempotent(conn):
    add_jf(conn, "jf1", "movie", "Heat", 1995, tmdb="949")
    add_lb(conn, "toy-story-4", "Toy Story 4", 2019)
    run(conn, FakeTmdb())
    snapshot = (links(conn), media(conn), reviews(conn))
    run(conn, FakeTmdb())
    assert (links(conn), media(conn), reviews(conn)) == snapshot
