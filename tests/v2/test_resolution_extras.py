"""The Resolution center's "This is a TV show" button and its list of Jellyfin movies that are not linked."""
from synchub import catalog, matching

NOW = "2026-01-01 00:00:00"


class FakeTmdb:
    def search_tv(self, title, year=None):
        return [{"id": 111, "name": "Scott Pilgrim Takes Off", "first_air_date": "2023-11-17"}, {"id": 222, "name": "Another Show"}]


def entry(conn, slug="scott-pilgrim-takes-off", title="Scott Pilgrim Takes Off"):
    conn.execute("INSERT INTO src_letterboxd_entry (username, slug, title, year, list, first_seen, last_seen) VALUES ('tristanisepic', ?, ?, 2023, 'watchlist', ?, ?)", (slug, title, NOW, NOW))


def test_a_film_that_is_really_a_series_gets_tv_suggestions(conn):
    entry(conn)
    assert [u["title"] for u in catalog.unmatched_sources(conn)] == ["Scott Pilgrim Takes Off"]
    assert matching.suggest_as_show(conn, FakeTmdb(), "letterboxd", "scott-pilgrim-takes-off") == 2
    best = catalog.pending_reviews(conn)[0]
    assert best["candidates"][0]["candidate_tmdb_id"] == "111" and best["candidates"][0]["confidence"] == 0.9 and best["candidates"][0]["kind"] == "show"
    assert "TV series" in best["candidates"][0]["reason"]
    assert matching.suggest_as_show(conn, FakeTmdb(), "letterboxd", "scott-pilgim-nope") == 0
    assert matching.suggest_as_show(conn, FakeTmdb(), "letterboxd", "scott-pilgrim-takes-off") == 0          # the same suggestions are not added twice


def test_jellyfin_movies_without_a_link_are_listed_with_why(conn):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, first_seen, last_seen) VALUES ('a', 'movie', 'No Id', 2001, ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, tmdb_id, first_seen, last_seen) VALUES ('b', 'movie', 'Has Id', 2002, '55', ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, first_seen, last_seen, removed_at) VALUES ('c', 'movie', 'Gone', ?, ?, ?)", (NOW, NOW, NOW))
    got = {r["item_id"]: r for r in catalog.unlinked_jellyfin_movies(conn)}
    assert set(got) == {"a", "b"} and "no TMDB id" in got["a"]["reason"] and "TMDB 55" in got["b"]["reason"]
    assert len(got) == catalog.jellyfin_movie_reconcile(conn)["unmatched"]                         # the same number Library status shows


def test_the_page_lists_both_and_the_status_number_links_to_it(client, v2):
    with v2.connection() as conn:
        entry(conn)
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, first_seen, last_seen) VALUES ('a', 'movie', 'No Id', 2001, ?, ?)", (NOW, NOW))
    page = client.get("/resolution").get_data(as_text=True)
    assert "This is a TV show" in page and "In Jellyfin, not linked" in page and "No Id" in page and 'id="jellyfin-unlinked"' in page
    assert "/resolution#jellyfin-unlinked" in client.get("/system/library").get_data(as_text=True)


def test_a_jellyfin_item_can_be_linked_by_tmdb_id(client, v2, monkeypatch):
    with v2.connection() as conn:
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, first_seen, last_seen) VALUES ('a', 'movie', 'No Id', 2001, ?, ?)", (NOW, NOW))
    monkeypatch.setattr("synchub.web.api.tasks.start_async", lambda *a, **k: None)
    got = client.post("/api/resolution/link", json={"source": "jellyfin", "key": "a", "kind": "movie", "tmdb_id": "999"}).get_json()
    assert got["ok"] is True
    with v2.connection() as conn:
        assert catalog.unlinked_jellyfin_movies(conn) == []
