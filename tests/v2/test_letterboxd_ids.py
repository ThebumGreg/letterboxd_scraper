from synchub.ingest import letterboxd_ids as ids

NOW = "2026-01-10 00:00:00"


def entry(conn, slug, user="alice", list_="watchlist", tmdb=None, checked=None):
    conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, title, tmdb_id, id_checked_at, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                 (user, slug, list_, slug, tmdb, checked, NOW, NOW))


def film_page(tmdb="949", kind="movie", imdb="tt0113277"):
    return f'<html><body data-tmdb-id="{tmdb}" data-tmdb-type="{kind}"><a href="https://www.imdb.com/title/{imdb}/maindetails">IMDb</a></body></html>'


def fetcher(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        return pages.get(url.split("/film/")[1].rstrip("/"), (404, ""))
    fetch.calls = calls
    return fetch


def test_parse_film_page_variants():
    assert ids.parse_film_page(film_page()) == {"tmdb_id": "949", "tmdb_type": "movie", "imdb_id": "tt0113277"}
    link_only = '<html><body><a href="https://www.themoviedb.org/movie/603/">TMDB</a></body></html>'
    assert ids.parse_film_page(link_only)["tmdb_id"] == "603"
    assert ids.parse_film_page('<html><body data-tmdb-id="abc"></body></html>')["tmdb_id"] is None
    assert ids.parse_film_page("<html></html>") == {"tmdb_id": None, "tmdb_type": None, "imdb_id": None}


def test_resolve_stores_ids_on_every_entry_of_a_film(conn):
    entry(conn, "heat-1995", "alice")
    entry(conn, "heat-1995", "bob", "watched")
    stats = ids.resolve(conn, fetcher({"heat-1995": (200, film_page())}), now=NOW)
    assert stats == {"checked": 1, "found": 1, "no_id": 0, "errors": 0, "blocked": False}
    rows = conn.execute("SELECT tmdb_id, imdb_id, id_source, id_checked_at FROM src_letterboxd_entry").fetchall()
    assert all(tuple(r) == ("949", "tt0113277", "letterboxd", NOW) for r in rows)


def test_tv_pages_and_missing_ids_are_marked_checked_without_an_id(conn):
    entry(conn, "tv-thing")
    entry(conn, "no-id")
    stats = ids.resolve(conn, fetcher({"tv-thing": (200, film_page("1399", "tv")), "no-id": (200, "<html><body></body></html>")}), now=NOW)
    assert stats["no_id"] == 2 and stats["found"] == 0
    assert conn.execute("SELECT COUNT(*) FROM src_letterboxd_entry WHERE tmdb_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM src_letterboxd_entry WHERE id_checked_at = ?", (NOW,)).fetchone()[0] == 2


def test_recently_checked_resolved_and_linked_films_are_skipped(conn):
    entry(conn, "recent", checked="2026-01-08 00:00:00")
    entry(conn, "old", checked="2025-12-01 00:00:00")
    entry(conn, "done", tmdb="1")
    entry(conn, "linked")
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence, status) VALUES ('media', 1, 'letterboxd', 'linked', 'manual', 1.0, 'manual')")
    f = fetcher({"old": (200, film_page())})
    ids.resolve(conn, f, now=NOW)
    assert f.calls == ["https://letterboxd.com/film/old/"]


def test_never_checked_films_come_first_and_limit_applies(conn):
    entry(conn, "stale", checked="2025-12-01 00:00:00")
    entry(conn, "fresh-a")
    entry(conn, "fresh-b")
    assert ids.slugs_needing_ids(conn, 2, now=NOW) == ["fresh-a", "fresh-b"]


def test_blocked_response_stops_the_run(conn):
    for slug in ("a", "b", "c"):
        entry(conn, slug)
    f = fetcher({"a": (200, film_page()), "b": (429, ""), "c": (200, film_page("2"))})
    stats = ids.resolve(conn, f, now=NOW)
    assert stats["blocked"] and stats["found"] == 1 and len(f.calls) == 2


def test_consecutive_errors_stop_the_run(conn):
    for i in range(8):
        entry(conn, f"s{i}")
    f = fetcher({f"s{i}": (500, "") for i in range(8)})
    stats = ids.resolve(conn, f, now=NOW)
    assert stats["errors"] == 5 and len(f.calls) == 5
    assert conn.execute("SELECT COUNT(*) FROM src_letterboxd_entry WHERE id_checked_at IS NOT NULL").fetchone()[0] == 0   # errors are retried next run
