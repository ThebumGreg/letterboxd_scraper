"""The Library check: findings worked out from what Radarr, Sonarr, Jellyfin and the scan saved, using Greg's real cases."""
import pytest

from synchub.library import arr_check
from synchub.web import library_check

NOW = "2026-01-01 00:00:00"


@pytest.fixture
def share(conn):
    conn.execute("""INSERT INTO storage_root (name, protocol, share, mount_point, jellyfin_path, radarr_path, sonarr_path, jellyfin_extra_paths)
                    VALUES ('media', 'local', '/x', '/mnt/m', NULL, '/media/Media', '/data', '/media = Movies\n/KidsMovie = Kids Movies\n/TV2 = TV Shows')""")
    conn.commit()


def movie(conn, tmdb, title, year, folder, file, has_file=1):
    row = conn.execute("SELECT id FROM media WHERE kind = 'movie' AND tmdb_id = ?", (tmdb,)).fetchone()
    media = row[0] if row else conn.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('movie', ?, ?, ?)", (title, year, tmdb)).lastrowid
    rid = str(100 + conn.execute("SELECT COUNT(*) FROM src_radarr_movie").fetchone()[0])
    conn.execute("INSERT INTO src_radarr_movie (radarr_id, tmdb_id, title, year, has_file, folder, file_path, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                 (rid, tmdb, title, year, has_file, folder, file, NOW, NOW))
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'radarr', ?, 'x', 1)", (media, rid))
    conn.commit()
    return media, rid


def jellyfin(conn, item, name, year, tmdb, path):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, tmdb_id, path, first_seen, last_seen) VALUES (?, 'movie', ?, ?, ?, ?, ?, ?)", (item, name, year, tmdb, path, NOW, NOW))
    conn.commit()


def kinds(conn):
    return {(f["title"], f["kind"]) for f in arr_check.movie_findings(conn)}


def test_guess_reads_title_and_year_from_a_file_name():
    assert arr_check.guess("/m/The Dark Knight (2008).mkv") == ("The Dark Knight", 2008)
    assert arr_check.guess("Blade.Runner.2049.2017.PROPER.2160p.REMUX.mkv") == ("Blade Runner 2049", 2017)
    assert arr_check.guess("/x/Thomas Friends Blue Mountain Mystery (2012) [1080p].mp4") == ("Thomas Friends Blue Mountain Mystery", 2012)
    assert arr_check.guess("movie.mkv") == ("movie", None)


def test_a_radarr_film_holding_another_films_file_is_found_and_the_real_film_named(conn, share):
    media, rid = movie(conn, "300467", "Dark Was the Night", 2014, "/media/Media/Movies/THE_DARK_NIGHT", "/media/Media/Movies/THE_DARK_NIGHT/The Dark Knight (2008).mkv")
    jellyfin(conn, "j1", "The Dark Knight", 2008, "155", "/media/THE_DARK_NIGHT/The Dark Knight (2008).mkv")
    found = [f for f in arr_check.movie_findings(conn) if f["kind"] == arr_check.WRONG_FILE]
    assert len(found) == 1 and found[0]["real"] == "The Dark Knight (2008)" and found[0]["action"]["type"] == "radarr_repoint"
    assert "not in Radarr yet" in found[0]["fix"]


def test_same_year_but_a_different_name_is_also_found(conn, share):
    movie(conn, "862552", "The Ritual Killer", 2023, "/media/TFiles/x", "/media/Media/Movies/The Killer (2023)/The.Killer.2023.1080p.mkv")
    assert ("The Ritual Killer", arr_check.WRONG_FILE) in kinds(conn)


def test_a_right_file_is_not_flagged_and_a_one_year_difference_is_allowed(conn, share):
    movie(conn, "1", "Heat", 1995, "/media/Media/Movies/Heat (1995)", "/media/Media/Movies/Heat (1995)/Heat (1995).mkv")
    movie(conn, "2", "Wall-E", 2008, "/media/Media/Movies/WALL-E", "/media/Media/Movies/WALL-E/WALL-E (2009).mkv")
    jellyfin(conn, "a", "Heat", 1995, "1", "/media/Heat (1995)/Heat (1995).mkv")
    jellyfin(conn, "b", "Wall-E", 2008, "2", "/media/WALL-E/WALL-E (2009).mkv")
    assert kinds(conn) == set()


def test_jellyfin_showing_the_file_as_another_title_is_a_different_id_finding(conn, share):
    movie(conn, "335984", "Blade Runner 2049", 2017, "/media/Media/Movies/Blade Runner 2049", "/media/Media/Movies/Blade Runner 2049/Blade.Runner.2049.2017.REMUX.mkv")
    jellyfin(conn, "j", "Blade Runner", 1982, "78", "/media/Blade Runner 2049/Blade.Runner.2049.2017.REMUX.mkv")
    got = [f for f in arr_check.movie_findings(conn) if f["kind"] == arr_check.DIFFERENT_ID]
    assert len(got) == 1 and "TMDB 78" in got[0]["summary"] and got[0]["action"] is None


def test_a_file_jellyfin_has_not_seen_offers_a_scan_and_an_uncovered_folder_does_not(conn, share):
    movie(conn, "3", "WALL-E", 2008, "/media/Media/Kids Movies/WALL-E (2008)", "/media/Media/Kids Movies/WALL-E (2008)/WALL-E (2008).mkv")
    movie(conn, "4", "Hidden", 2010, "/media/Media/Old Stuff/Hidden (2010)", "/media/Media/Old Stuff/Hidden (2010)/Hidden (2010).mkv")
    got = {f["title"]: f for f in arr_check.movie_findings(conn)}
    assert got["WALL-E"]["kind"] == arr_check.NOT_IN_JELLYFIN and got["WALL-E"]["action"]["type"] == "scan_jellyfin"
    assert got["Hidden"]["kind"] == arr_check.NO_JELLYFIN_LIBRARY and got["Hidden"]["action"] is None


def test_a_path_no_share_covers_says_so_and_a_missing_file_is_found(conn, share):
    movie(conn, "5", "Lost", 2004, "/elsewhere/Lost", "/elsewhere/Lost/Lost (2004).mkv")
    media, _ = movie(conn, "6", "Zootopia 2", 2025, "/media/Media/Movies/Zootopia 2 (2025)", None, has_file=0)
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code, updated_at) VALUES ('movie', ?, 'downloaded', ?)", (media, NOW))
    conn.commit()
    assert kinds(conn) == {("Lost", arr_check.NO_MAPPING), ("Zootopia 2", arr_check.NO_FILE)}


def test_two_films_on_one_file_and_two_entries_for_one_tmdb_id_are_found(conn, share):
    movie(conn, "7", "A", 2000, "/media/Media/Movies/X", "/media/Media/Movies/X/A (2000).mkv")
    movie(conn, "8", "A Sequel", 2000, "/media/Media/Movies/X", "/media/Media/Movies/X/A (2000).mkv")
    movie(conn, "9", "Twin", 2001, "/media/Media/Movies/T1", None, has_file=0)
    movie(conn, "9", "Twin", 2001, "/media/Media/Movies/T2", None, has_file=0)
    got = kinds(conn)
    assert ("A", arr_check.SHARED_FILE) in got and ("A Sequel", arr_check.SHARED_FILE) in got and ("Twin", arr_check.DUPLICATE_RADARR) in got


def test_episodes_sonarr_has_but_jellyfin_lacks_offer_a_scan(conn, share):
    media = conn.execute("INSERT INTO media (kind, title, year, tmdb_id, tvdb_id) VALUES ('show', 'The Last Ship', 2014, '60802', '269533')").lastrowid
    conn.execute("INSERT INTO src_sonarr_series (sonarr_id, tvdb_id, title, year, folder, first_seen, last_seen) VALUES ('18', '269533', 'The Last Ship', 2014, '/data/TV Shows/The Last Ship', ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'sonarr', '18', 'x', 1)", (media,))
    ep = conn.execute("INSERT INTO episode (media_id, season_number, episode_number) VALUES (?, 1, 2)", (media,)).lastrowid
    conn.execute("INSERT INTO src_sonarr_episode (sonarr_episode_id, sonarr_id, season_number, episode_number, has_file, file_path, first_seen, last_seen) VALUES ('9', '18', 1, 2, 1, '/data/TV Shows/The Last Ship/S01E02.mkv', ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('episode', ?, 'sonarr_episode', '9', 'x', 1)", (ep,))
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code, updated_at) VALUES ('episode', ?, 'downloaded', ?)", (ep, NOW))
    conn.commit()
    got = arr_check.show_findings(conn)
    assert len(got) == 1 and got[0]["kind"] == arr_check.MISSING_EPISODES and "S01E02" in got[0]["summary"] and got[0]["action"]["type"] == "scan_jellyfin"
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, year, tvdb_id, path, first_seen, last_seen) VALUES ('s', 'series', 'Other Show', 2023, NULL, '/TV2/The Last Ship', ?, ?)", (NOW, NOW))
    conn.commit()
    assert arr_check.show_findings(conn)[0]["kind"] == arr_check.DIFFERENT_ID


class FakeRadarr:
    def __init__(self, movie):
        self._movie, self.updates, self.rescans = movie, [], []

    def movie(self, movie_id): return dict(self._movie)
    def update_movie(self, movie_id, body, move_files=False): self.updates.append((body["path"], move_files))
    def rescan_movie(self, movie_id): self.rescans.append(movie_id)


def test_repointing_uses_a_new_folder_and_never_moves_files():
    fake = FakeRadarr({"id": 5, "title": "Dark Was the Night", "year": 2014, "path": "/media/Media/Movies/THE_DARK_NIGHT", "rootFolderPath": "/media/Media/Movies"})
    assert library_check.repoint_radarr(fake, 5) == "/media/Media/Movies/Dark Was the Night (2014)"
    assert fake.updates == [("/media/Media/Movies/Dark Was the Night (2014)", False)] and fake.rescans == [5]


def test_the_page_lists_findings_and_only_changes_a_film_that_still_looks_wrong(client, conn, share, monkeypatch):
    media, rid = movie(conn, "300467", "Dark Was the Night", 2014, "/media/Media/Movies/THE_DARK_NIGHT", "/media/Media/Movies/THE_DARK_NIGHT/The Dark Knight (2008).mkv")
    page = client.get("/library/check").get_data(as_text=True)
    assert "Radarr has another film" in page and "Dark Was the Night" in page and "data-radarr" in page
    assert "Dark Was the Night" in client.get("/library/check.csv").get_data(as_text=True)
    good, good_id = movie(conn, "1", "Heat", 1995, "/media/Media/Movies/Heat (1995)", "/media/Media/Movies/Heat (1995)/Heat (1995).mkv")
    jellyfin(conn, "a", "Heat", 1995, "1", "/media/Heat (1995)/Heat (1995).mkv")
    from synchub.clients import arr
    fake = FakeRadarr({"id": 5, "title": "Dark Was the Night", "year": 2014, "path": "/media/Media/Movies/THE_DARK_NIGHT", "rootFolderPath": "/media/Media/Movies"})
    monkeypatch.setattr(arr, "RadarrClient", lambda: fake)
    refused = client.post("/api/library/check/radarr-repoint", json={"radarr_id": good_id})
    assert refused.status_code == 400 and fake.updates == []
    done = client.post("/api/library/check/radarr-repoint", json={"radarr_id": rid})
    assert done.get_json()["ok"] and len(fake.updates) == 1


def test_scan_jellyfin_asks_jellyfin_to_refresh(client, monkeypatch):
    from synchub.clients import jellyfin
    asked = []
    monkeypatch.setattr(jellyfin.JellyfinClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(jellyfin.JellyfinClient, "refresh_library", lambda self: asked.append(1))
    assert client.post("/api/library/check/scan-jellyfin").get_json()["ok"] and asked == [1]


@pytest.mark.parametrize("title,year,file,wrong", [
    ("Blade Runner 2049", 2017, "Blade.Runner.2049.2017.PROPER.2160p.US.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.7.1.Atmos-FGT.mkv", False),
    ("Borat Subsequent Moviefilm: Delivery of Prodigious Bribe to American Regime for Make Benefit Once Glorious Nation of Kazakhstan", 2020, "Borat Cultural Learnings of America for Make Benefit Glorious Nation of Kazakhstan (2006).mkv", True),
    ("Christmas Story", 2007, "Christmas Story (1983).mkv", True),
    ("Dark Was the Night", 2014, "The Dark Knight (2008).mkv", True),
    ("Doctor Strange in the Multiverse of Madness", 2022, "Doctor Strange (2016).mkv", True),
    ("Flatliners", 1990, "Flatliners (2017).mkv", True),
    ("Going Down in La-La Land", 2011, "La La Land (2016).mkv", True),
    ("Home Sweet Home Alone", 2021, "Home Alone (1990).mkv", True),
    ("Meg 2: The Trench", 2023, "The Meg (2018).mkv", True),
    ("Peter Pandesal", 1976, "Peter Pan (2003).mkv", True),
    ("Planes: Fire & Rescue: Smoke Jumpers", 2014, "Planes Fire & Rescue (2014).mkv", False),
    ("Teenage Mutant Ninja Turtles: Out of the Shadows", 2016, "Teenage Mutant Ninja Turtles II The Secret of the Ooze (1991).mkv", True),
    ("The Jungle Book", 1967, "The Jungle Book (2016).mkv", True),
    ("The Ritual Killer", 2023, "The.Killer.2023.Repack.1080p.NF.WEB-DL.DDP5.1.ITA.ENG.AC3.5.1.ITA.H.264-MeM.GP.mkv", True),
    ("Thomas & Friends: Blue Mountain Mystery", 2012, "Thomas Friends Blue Mountain Mystery (2012) [1080p] [BluRay] [5.1] [YTS.MX].mp4", False),
    ("Thomas & Friends: Journey Beyond Sodor", 2017, "Thomas Friends Journey Beyond Sodor (2017) [1080p] [WEBRip] [5.1] [YTS.MX].mp4", False),
    ("Thomas & Friends: Sodor's Legend of the Lost Treasure: The Movie", 2015, "Thomas (2009).mkv", True),
    ("WALL·E", 2008, "WALL·E (2008).mkv", False),
])
def test_greg_real_cases_are_told_apart(title, year, file, wrong):
    file_title, file_year = arr_check.guess(file)
    assert arr_check._plausible_other_film(title, year, file_title, file_year) is wrong
