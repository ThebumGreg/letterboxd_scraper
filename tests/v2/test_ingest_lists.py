import pytest

from synchub import db
from synchub.ingest import letterboxd as lb
from synchub.ingest import serializd as sz

PAGE = """<html><body>
<div class="react-component" data-item-slug="heat-1995" data-item-full-display-name="Heat (1995)"></div>
<div class="react-component" data-item-slug="toy-story-4" data-item-full-display-name="Toy Story 4 (2019)"></div>
<div class="react-component" data-item-slug="no-name-film"><img alt="Plain Alt Title"/></div>
{next}</body></html>"""
NEXT = '<a class="next" href="/u/watchlist/page/2/">Next</a>'


def page(slugs, has_next=False):
    posters = "".join(f'<div class="react-component" data-item-slug="{s}" data-item-full-display-name="{s.title()} (2000)"></div>' for s in slugs)
    return f"<html><body>{posters}{NEXT if has_next else ''}</body></html>"


def test_parse_page_extracts_slug_title_year_and_next():
    entries, has_next = lb.parse_page(PAGE.format(next=NEXT))
    assert has_next
    assert entries[0] == {"slug": "heat-1995", "title": "Heat", "year": 1995}
    assert entries[1]["year"] == 2019 and entries[2] == {"slug": "no-name-film", "title": "Plain Alt Title", "year": None}
    assert lb.parse_page(PAGE.format(next=""))[1] is False


def scripted(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        return pages.get(url, (404, ""))
    fetch.calls = calls
    return fetch


def test_scrape_list_follows_pagination():
    fetch = scripted({"https://letterboxd.com/u/watchlist/": (200, page(["a", "b"], True)),
                      "https://letterboxd.com/u/watchlist/page/2/": (200, page(["c"]))})
    entries, complete = lb.scrape_list("u", "watchlist", fetch)
    assert [e["slug"] for e in entries] == ["a", "b", "c"] and complete


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_scrape_is_incomplete(status):
    fetch = scripted({"https://letterboxd.com/u/watchlist/": (200, page(["a"], True)),
                      "https://letterboxd.com/u/watchlist/page/2/": (status, "")})
    entries, complete = lb.scrape_list("u", "watchlist", fetch)
    assert [e["slug"] for e in entries] == ["a"] and not complete


def test_unknown_user_is_not_a_complete_empty_list():
    assert lb.scrape_list("nobody", "watchlist", scripted({})) == ([], False)


def active(conn, list_name="watchlist"):
    return {r["slug"] for r in conn.execute("SELECT slug FROM src_letterboxd_entry WHERE list = ? AND removed_at IS NULL", (list_name,))}


def test_store_keeps_removed_entries_and_ids(conn):
    lb.store(conn, "Alice", "watchlist", [{"slug": "a", "title": "A", "year": 2000}, {"slug": "b", "title": "B", "year": 2001}], True, "2026-01-01 00:00:00")
    conn.execute("UPDATE src_letterboxd_entry SET tmdb_id = '123', id_source = 'letterboxd' WHERE slug = 'a'")   # filled in later by the resolver

    result = lb.store(conn, "alice", "watchlist", [{"slug": "a", "title": "A", "year": None}], True, "2026-01-02 00:00:00")

    assert result["removed"] == 1 and active(conn) == {"a"}
    rows = {r["slug"]: dict(r) for r in conn.execute("SELECT * FROM src_letterboxd_entry")}
    assert rows["b"]["removed_at"] == "2026-01-02 00:00:00"        # kept, flagged
    assert rows["a"]["tmdb_id"] == "123" and rows["a"]["year"] == 2000   # scrape did not wipe resolved data
    assert rows["a"]["username"] == "alice"


def test_incomplete_or_empty_scrape_never_marks_removed(conn):
    lb.store(conn, "alice", "watchlist", [{"slug": "a", "title": "A", "year": 1}], True, "2026-01-01 00:00:00")
    assert lb.store(conn, "alice", "watchlist", [{"slug": "x", "title": "X", "year": 1}], False, "2026-01-02 00:00:00")["removed"] == 0
    assert lb.store(conn, "alice", "watchlist", [], True, "2026-01-03 00:00:00")["removed"] == 0
    assert active(conn) == {"a", "x"}


def test_lists_and_users_are_scoped_independently(conn):
    lb.store(conn, "alice", "watchlist", [{"slug": "a", "title": "A", "year": 1}], True, "2026-01-01 00:00:00")
    lb.store(conn, "alice", "watched", [{"slug": "a", "title": "A", "year": 1}], True, "2026-01-01 00:00:00")
    lb.store(conn, "bob", "watchlist", [{"slug": "a", "title": "A", "year": 1}], True, "2026-01-01 00:00:00")
    lb.store(conn, "alice", "watchlist", [{"slug": "z", "title": "Z", "year": 1}], True, "2026-01-02 00:00:00")
    removed = {(r["username"], r["list"]): r["removed_at"] for r in conn.execute("SELECT username, list, removed_at FROM src_letterboxd_entry WHERE slug = 'a'")}
    assert removed == {("alice", "watchlist"): "2026-01-02 00:00:00", ("alice", "watched"): None, ("bob", "watchlist"): None}


def test_sync_all_scrapes_active_profiles_only(v2):
    with v2.connection() as c:
        c.execute("INSERT INTO profile (name, letterboxd_username) VALUES ('A', 'alice')")
        c.execute("INSERT INTO profile (name, letterboxd_username, is_active) VALUES ('B', 'bob', 0)")
        c.execute("INSERT INTO profile (name, serializd_username) VALUES ('C', 'carol_sz')")
    fetch = scripted({"https://letterboxd.com/alice/watchlist/": (200, page(["w1"])),
                      "https://letterboxd.com/alice/films/": (200, page(["f1", "f2"]))})
    summary = lb.sync_all(fetch=fetch, sleep=lambda s: None)
    assert list(summary) == ["alice"]
    assert summary["alice"]["watchlist"]["seen"] == 1 and summary["alice"]["watched"]["seen"] == 2
    assert all("bob" not in url and "carol" not in url for url in fetch.calls)
    with v2.connection() as c:
        assert {(r["list"], r["slug"]) for r in c.execute("SELECT list, slug FROM src_letterboxd_entry")} == {("watchlist", "w1"), ("watched", "f1"), ("watched", "f2")}


# ---------------------------------------------------------------- Serializd
def test_serializd_store_prunes_but_keeps_rows_and_ids(conn):
    shows = [{"id": 7, "name": "Show A", "tvdbId": 111, "tmdbId": 222}, {"id": 8, "name": "Show B"}]
    sz.store(conn, "Alice", shows, "2026-01-01 00:00:00")
    result = sz.store(conn, "alice", [{"id": 7, "name": "Show A"}], "2026-01-02 00:00:00")
    rows = {r["serializd_show_id"]: dict(r) for r in conn.execute("SELECT * FROM src_serializd_entry")}
    assert result == {"seen": 1, "removed": 1}
    assert rows["8"]["removed_at"] and rows["7"]["removed_at"] is None
    assert (rows["7"]["tvdb_id"], rows["7"]["tmdb_id"]) == ("111", "222")   # IDs kept when a later payload omits them


def test_serializd_empty_list_removes_nothing(conn):
    sz.store(conn, "alice", [{"id": 7, "name": "A"}], "2026-01-01 00:00:00")
    assert sz.store(conn, "alice", [], "2026-01-02 00:00:00")["removed"] == 0


def test_serializd_sync_all_reports_http_errors_without_changes(v2):
    with v2.connection() as c:
        c.execute("INSERT INTO profile (name, serializd_username) VALUES ('A', 'alice')")
        c.execute("INSERT INTO profile (name, serializd_username) VALUES ('B', 'bob')")
        sz.store(c, "alice", [{"id": 1, "name": "Keep"}], "2026-01-01 00:00:00")

    def fetch(username):
        return (429, None) if username == "alice" else (200, {"shows": [{"id": 5, "name": "New"}]})

    summary = sz.sync_all(fetch)
    assert summary["alice"] == {"error": "HTTP 429"} and summary["bob"]["seen"] == 1
    with v2.connection() as c:
        assert c.execute("SELECT removed_at FROM src_serializd_entry WHERE username = 'alice'").fetchone()[0] is None
