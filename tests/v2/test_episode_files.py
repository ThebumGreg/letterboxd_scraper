"""Files of TV episodes: linked to their episode, labelled per episode, shown on the show page; and the main file of a film or episode is never "unexplained"."""
from synchub import storage
from synchub.library import episode_link, file_labels, film_link

from .test_quality import NOW, fixture_file, rec_of


def show(conn, with_jellyfin_path=False):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "jellyfin_path": "/data/media"})
    media_id = conn.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('show', 'Severance', 2022, '95396')").lastrowid
    ep = {(1, n): conn.execute("INSERT INTO episode (media_id, season_number, episode_number, title) VALUES (?, 1, ?, ?)", (media_id, n, f"Ep {n}")).lastrowid for n in (1, 2)}
    if with_jellyfin_path:
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, first_seen, last_seen) VALUES ('s1', 'series', 'Severance', ?, ?)", (NOW, NOW))
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', 's1', 'x', 1.0)", (media_id,))
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, series_id, season_number, episode_number, first_seen, last_seen, path) VALUES ('e1', 'episode', 's1', 1, 2, ?, ?, "
                     "'/data/media/TV/Severance/odd name.mkv')", (NOW, NOW))
    conn.commit()
    return media_id, ep


def episode_file(conn, rel, **kw):
    file_id = fixture_file(conn, rel=rel, **kw)
    conn.execute("UPDATE library_file SET media_type = 'TV Episode', show_title = 'Severance', season_number = 1 WHERE id = ?", (file_id,))
    conn.commit()
    return file_id


def test_season_and_episode_numbers_are_read_from_a_name():
    assert episode_link.parse_numbers("Severance - S01E02 - Half Loop.mkv") == (1, 2)
    assert episode_link.parse_numbers("severance.s1e2.mkv") == (1, 2) and episode_link.parse_numbers("Show 1x05.mkv") == (1, 5)
    assert episode_link.parse_numbers("Severance - S01E01-E02.mkv") == (1, 1) and episode_link.parse_numbers("Heat (1995).mkv") is None


def test_episode_files_link_to_their_episode_by_name_or_by_jellyfin_path(conn):
    media_id, ep = show(conn, with_jellyfin_path=True)
    by_name = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01.mkv")
    by_path = episode_file(conn, "TV/Severance/odd name.mkv")
    nothing = episode_file(conn, "TV/Severance/Season 01/Severance - S01E09.mkv")             # no such episode in the list
    film_link.relink_all(conn)
    conn.commit()
    assert (rec_of(conn, by_name)["media_id"], rec_of(conn, by_name)["episode_id"]) == (media_id, ep[(1, 1)])
    assert rec_of(conn, by_path)["episode_id"] == ep[(1, 2)] and rec_of(conn, nothing)["episode_id"] is None and rec_of(conn, nothing)["media_id"] is None


def test_two_files_of_one_episode_are_labelled_per_episode_not_per_show(conn):
    media_id, ep = show(conn)
    one = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01.mkv", size=4_000_000_000, resolution="3840x2160")
    two = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01 copy.mkv", size=1_000_000_000)
    other = episode_file(conn, "TV/Severance/Season 01/Severance - S01E02.mkv")
    film_link.relink_all(conn)
    conn.commit()
    assert rec_of(conn, two)["file_label"] == "unexplained" and rec_of(conn, one)["file_label"] is None and rec_of(conn, other)["file_label"] is None
    counts = file_labels.counts(conn)
    assert counts["episodes"] == 1 and counts["films"] == 0 and counts["files"]["unexplained"] == 1
    assert file_labels.ids_for(conn, "many") == {one, two}


def test_the_main_file_is_never_unexplained(conn):
    media_id, ep = show(conn)
    big = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01.mkv", size=4_000_000_000, resolution="3840x2160")
    small = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01 copy.mkv", size=1_000_000_000)
    film_link.relink_all(conn)
    conn.commit()
    assert rec_of(conn, big)["file_label"] is None and rec_of(conn, small)["file_label"] == "unexplained"
    conn.execute("UPDATE library_file SET is_main = 1 WHERE id = ?", (small,))             # you chose the other one
    file_labels.refresh(conn)
    conn.commit()
    assert rec_of(conn, small)["file_label"] is None and rec_of(conn, big)["file_label"] == "unexplained"


def test_the_show_page_lists_only_episodes_with_more_than_one_file(client, conn):
    media_id, ep = show(conn)
    a = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01.mkv", size=4_000_000_000, resolution="3840x2160")
    b = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01 copy.mkv", size=1_000_000_000)
    episode_file(conn, "TV/Severance/Season 01/Severance - S01E02.mkv")
    film_link.relink_all(conn)
    conn.commit()
    page = client.get(f"/show/{media_id}").get_data(as_text=True)
    assert 'id="episodeFiles"' in page and "1 episode with more than one file" in page and "S01E01" in page and f'data-file="{b}"' in page and "Unexplained" in page
    assert "S01E02" not in page[page.index('id="episodeFiles"'):page.index("Episodes</h4>")] and "Not this episode" in page


def test_hiding_an_episode_file_keeps_the_last_file_and_never_the_main_one(client, conn):
    media_id, ep = show(conn)
    a = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01.mkv", size=4_000_000_000, resolution="3840x2160")
    b = episode_file(conn, "TV/Severance/Season 01/Severance - S01E01 copy.mkv", size=1_000_000_000)
    film_link.relink_all(conn)
    conn.commit()
    assert not client.post(f"/api/library/file/{a}/set-aside", json={"reason": "old_version"}).get_json()["ok"]      # the main file
    assert client.post(f"/api/library/file/{b}/set-aside", json={"reason": "old_version"}).get_json()["ok"]
    assert client.post(f"/api/library/file/{b}/not-this-film").get_json()["ok"]
    assert rec_of(conn, b)["episode_id"] is None and rec_of(conn, b)["media_link_source"] == "manual"
    film_link.relink_all(conn)
    assert rec_of(conn, b)["episode_id"] is None                                                             # a scan does not bring it back
