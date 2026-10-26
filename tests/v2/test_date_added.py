"""Date Added: Jellyfin's own date is stored, a show is as new as its newest episode, and the dashboard can sort by it."""
import re

from config import BASE_DIR
from synchub import catalog, settings
from synchub.ingest import jellyfin as jf

from .test_ingest_jellyfin import FakeJellyfin, episode, rows, season, series


def movie(id_, name, created=None):
    item = {"Id": id_, "Type": "Movie", "Name": name, "ProductionYear": 2000, "ProviderIds": {}}
    if created:
        item["DateCreated"] = created
    return item


# ---------------------------------------------------------------- stored from Jellyfin
def test_the_date_jellyfin_added_an_item_is_stored(conn):
    jf.sync(conn, FakeJellyfin([movie("m1", "Heat", "2026-10-21T18:04:11.1234567Z"), movie("m2", "No date")]), "movies", now="2026-11-01 00:00:00")
    got = rows(conn)
    assert got["m1"]["date_created"] == "2026-10-21 18:04:11" and got["m2"]["date_created"] is None


def test_a_later_sync_that_does_not_give_the_date_keeps_the_one_we_have_and_a_new_one_replaces_it(conn):
    jf.sync(conn, FakeJellyfin([movie("m1", "Heat", "2026-10-21T18:04:11Z")]), "movies", now="2026-11-01 00:00:00")
    jf.sync(conn, FakeJellyfin([movie("m1", "Heat")]), "movies", now="2026-11-02 00:00:00")
    assert rows(conn)["m1"]["date_created"] == "2026-10-21 18:04:11"
    jf.sync(conn, FakeJellyfin([movie("m1", "Heat", "2026-11-05T09:00:00Z")]), "movies", now="2026-11-06 00:00:00")      # re-added later
    assert rows(conn)["m1"]["date_created"] == "2026-11-05 09:00:00"


def test_the_jellyfin_request_asks_for_the_date():
    assert "DateCreated" in (BASE_DIR / "synchub/clients/jellyfin.py").read_text()


# ---------------------------------------------------------------- the catalog
def library(conn):
    """A movie, a show with two episodes added on different days, a show whose episodes carry no date, and a movie that is not in Jellyfin."""
    def media(kind, title, tmdb):
        return conn.execute("INSERT INTO media (kind, title, tmdb_id, year) VALUES (?, ?, ?, 2000)", (kind, title, tmdb)).lastrowid

    def link(media_id, key):
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', ?, 'x', 1.0)", (media_id, key))
    film, show, bare, absent = media("movie", "Film", "1"), media("show", "Show", "2"), media("show", "Bare", "3"), media("movie", "Absent", "4")
    link(film, "m1"); link(show, "s1"); link(bare, "s2")
    jf.sync(conn, FakeJellyfin([
        movie("m1", "Film", "2026-10-21T18:04:11Z"),
        series("s1", "Show"), season("sea1", "s1", 1),
        episode("e1", "s1", "sea1", 1, 1, DateCreated="2026-09-01T10:00:00Z"), episode("e2", "s1", "sea1", 1, 2, DateCreated="2026-10-30T20:15:00Z"),
        series("s2", "Bare"), episode("e3", "s2", "sea1", 1, 1),
    ]), "all", now="2026-11-01 00:00:00")
    conn.execute("UPDATE src_jellyfin_item SET date_created = '2026-08-08 08:00:00' WHERE item_id = 's2'")      # the show's own date, no episode dates
    return {r["title"]: r for r in catalog.list_catalog(conn, "2026-11-01")}


def test_a_movie_has_its_own_date_and_a_show_is_as_new_as_its_newest_episode(conn):
    got = library(conn)
    assert (got["Film"]["date_added"], got["Film"]["date_added_label"]) == ("2026-10-21", "Oct 21, 2026")
    assert (got["Show"]["date_added"], got["Show"]["date_added_label"]) == ("2026-10-30", "Oct 30, 2026")      # not the 1 Sep episode
    assert got["Bare"]["date_added"] == "2026-08-08"                                                              # no episode dates: the show's own
    assert got["Absent"]["date_added"] is None and got["Absent"]["date_added_label"] is None


def test_the_detail_pages_carry_the_same_dates(conn):
    got = library(conn)
    assert catalog.media_detail(conn, got["Film"]["id"])["date_added_label"] == "Oct 21, 2026"
    assert catalog.media_detail(conn, got["Show"]["id"])["date_added_label"] == "Oct 30, 2026"
    assert catalog.media_detail(conn, got["Absent"]["id"])["date_added_label"] is None


# ---------------------------------------------------------------- the pages
def test_the_dashboard_offers_date_added_and_shows_the_date(client, v2, conn):
    got = library(conn)
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (got["Film"]["id"],))
    conn.commit()
    page = client.get("/").get_data(as_text=True)
    assert '<option value="added-desc">Date added</option>' in page
    film = re.search(r'<tr class="item-row[^>]*data-id="%d"[^>]*>.*?</tr>' % got["Film"]["id"], page, re.S).group(0)
    assert 'data-added="2026-10-21"' in film and 'data-added-label="Oct 21, 2026"' in film and "Added Oct 21, 2026" in film
    absent = re.search(r'<tr class="item-row[^>]*data-id="%d"[^>]*>.*?</tr>' % got["Absent"]["id"], page, re.S).group(0)
    assert 'data-added=""' in absent and "Added " not in absent


def test_the_dashboard_script_sorts_newest_first_with_undated_titles_last():
    script = (BASE_DIR / "static/js/dashboard.js").read_text()
    assert 'sortSel.value === "added-desc"' in script and 'b.dataset.added || ""' in script and '"Added " + d.addedLabel' in script


def test_the_movie_and_show_pages_say_when_they_were_added(client, v2, conn):
    got = library(conn)
    conn.commit()                                                           # the fixture's open write would block the settings write
    settings.set_value("jellyfin_url", "http://jf:8096")
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (got["Film"]["id"],))
    conn.commit()
    assert "Added to library:</strong> Oct 21, 2026" in client.get(f"/movie/{got['Film']['id']}").get_data(as_text=True)
    assert "Newest episode added to library:</strong> Oct 30, 2026" in client.get(f"/show/{got['Show']['id']}").get_data(as_text=True)
    assert "Added to library" not in client.get(f"/movie/{got['Absent']['id']}").get_data(as_text=True)
