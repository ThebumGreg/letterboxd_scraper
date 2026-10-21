"""The whole backend on realistic data: the show-page bug from the production export must be gone."""
from synchub import episodes, states, tasks
from synchub.ingest import jellyfin as jf_ingest
from synchub.ingest import radarr as radarr_ingest
from synchub.ingest import sonarr as sonarr_ingest

from .fakes import FakeTmdb
from .test_ingest_arr import FakeRadarr, FakeSonarr
from .test_ingest_jellyfin import FakeJellyfin, episode, season, series

TODAY = "2026-06-15"

LAST_SHIP = {"id": 60802, "name": "The Last Ship", "first_air_date": "2014-06-22", "status": "Ended",
             "external_ids": {"tvdb_id": 269533, "imdb_id": "tt2402207"}, "seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": 3}]}
LAST_SHIP_SEASONS = {0: {"episodes": [{"id": 900, "episode_number": 1, "name": "Behind the Scenes", "air_date": "2014-06-01"}]},
                     1: {"episodes": [{"id": 901, "episode_number": n, "name": f"S1E{n}", "air_date": f"2014-07-{n:02d}"} for n in (1, 2, 3)]},
                     3: {"episodes": [{"id": 910 + n, "episode_number": n, "name": f"S3E{n}", "air_date": f"2016-08-{n:02d}"} for n in (9, 10, 11)]
                         + [{"id": 999, "episode_number": 12, "name": "Not yet", "air_date": "2099-01-01"}]}}


def tmdb():
    return FakeTmdb(shows={"60802": LAST_SHIP}, seasons={"60802": LAST_SHIP_SEASONS},
                    find={("269533", "tvdb_id"): {"tv_results": [{"id": 60802, "name": "The Last Ship", "first_air_date": "2014-06-22"}]}})


def load_sources(conn):
    # Jellyfin as it really reports: the series has IDs, its episodes carry their OWN episode-level TVDB IDs.
    jf = FakeJellyfin([
        series("ser1", "The Last Ship", tvdb="269533", tmdb="60802"),
        season("sea0", "ser1", 0), season("sea3", "ser1", 3),
        episode("sp1", "ser1", "sea0", 0, 1, tvdb="7000001"),
        episode("e10", "ser1", "sea3", 3, 10, tvdb="5648401"),
    ])
    jf_ingest.sync(conn, jf, "all", "2026-06-01 00:00:00")
    # Sonarr knows the show only by TVDB ID and has S3E9 + S3E10 downloaded, S3E11 monitored.
    sonarr = FakeSonarr(
        [{"id": 10, "tvdbId": 269533, "title": "The Last Ship", "year": 2014, "monitored": True}],
        {10: [{"id": 1, "seasonNumber": 3, "episodeNumber": 9, "title": "S3E9", "airDate": "2016-08-09", "monitored": True, "hasFile": True},
              {"id": 2, "seasonNumber": 3, "episodeNumber": 10, "title": "S3E10", "airDate": "2016-08-10", "monitored": True, "hasFile": True},
              {"id": 3, "seasonNumber": 3, "episodeNumber": 11, "title": "S3E11", "airDate": "2016-08-11", "monitored": True, "hasFile": False}]},
        queue=[{"id": 77, "episodeId": 3, "size": 100, "sizeleft": 25, "trackedDownloadState": "downloading", "title": "S3E11"}])
    sonarr_ingest.sync_library(conn, sonarr, "2026-06-01 00:00:00")
    sonarr_ingest.sync_queue(conn, sonarr, "2026-06-01 00:00:00")


def test_show_page_data_is_correct_for_a_show_with_episode_level_provider_ids(v2):
    with v2.connection() as conn:
        load_sources(conn)
    result = tasks.reconcile_now(tmdb())
    assert result["matching"]["linked"] == 2           # Jellyfin series (TMDB ID) and Sonarr series (TVDB ID via /find) -> ONE show
    assert result["jellyfin_episodes"] == {"linked": 2}
    assert result["sonarr_episodes"] == {"same_numbering": 3}

    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
        media_id = conn.execute("SELECT id FROM media").fetchone()[0]
        summary = states.show_summary(conn, media_id, TODAY)
        eps = {e["episode_number"]: e["state"] for e in states.season_episodes(conn, media_id, 3, TODAY)}
        specials = states.season_episodes(conn, media_id, 0, TODAY)

    assert [s["label"] for s in summary["seasons"]] == ["Season 1", "Season 3", "Specials (Season 0)"]
    season3 = summary["seasons"][1]
    assert eps == {9: "downloaded", 10: "in_library", 11: "downloading", 12: "not_aired"}
    assert season3["in_library"] == 1 and season3["aired"] == 3 and season3["state"] == "partial"
    assert summary["seasons"][0]["state"] == "missing" and summary["seasons"][0]["aired"] == 3        # S1 exists in TMDB, nowhere in the library
    assert [e["state"] for e in specials] == ["in_library"]       # the special is in Jellyfin as S00E01 and TMDB lists it as Season 0
    totals = summary["totals"]
    assert totals["episodes"] == 7 and totals["aired"] == 6 and totals["in_library"] == 1       # specials are not counted
    assert totals["counts"] == {"missing": 3, "downloaded": 1, "in_library": 1, "downloading": 1, "not_aired": 1}


def test_reconcile_is_idempotent(v2):
    with v2.connection() as conn:
        load_sources(conn)
    tasks.reconcile_now(tmdb())
    with v2.connection() as conn:
        snapshot = [tuple(r) for r in conn.execute("SELECT target_type, target_id, state_code, progress, updated_at FROM item_state ORDER BY 1, 2")]
        counts = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("media", "episode", "link", "match_review", "processing_event")]
    second = tasks.reconcile_now(tmdb())
    with v2.connection() as conn:
        assert [tuple(r) for r in conn.execute("SELECT target_type, target_id, state_code, progress, updated_at FROM item_state ORDER BY 1, 2")] == snapshot
        assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("media", "episode", "link", "match_review", "processing_event")] == counts
    assert second["states"]["changed"] == 0


def test_removing_an_episode_from_jellyfin_changes_its_state_but_keeps_its_row(v2):
    with v2.connection() as conn:
        load_sources(conn)
    tasks.reconcile_now(tmdb())
    with v2.connection() as conn:       # the next sync no longer reports episode e10
        jf_ingest.sync(conn, FakeJellyfin([series("ser1", "The Last Ship", tvdb="269533", tmdb="60802"), season("sea0", "ser1", 0), season("sea3", "ser1", 3)]), "all", "2026-06-02 00:00:00")
    tasks.reconcile_now(tmdb())
    with v2.connection() as conn:
        media_id = conn.execute("SELECT id FROM media").fetchone()[0]
        eps = {e["episode_number"]: e["state"] for e in states.season_episodes(conn, media_id, 3, TODAY)}
        assert conn.execute("SELECT COUNT(*) FROM src_jellyfin_item WHERE type = 'episode' AND removed_at IS NOT NULL").fetchone()[0] == 2     # kept, flagged removed
    assert eps[10] == "downloaded"      # no longer in Jellyfin, but Sonarr still has the file


def test_movies_end_to_end(v2):
    with v2.connection() as conn:
        from synchub.ingest import letterboxd as lb
        from .test_ingest_jellyfin import FakeJellyfin
        jf_ingest.sync(conn, FakeJellyfin([{"Id": "m1", "Type": "Movie", "Name": "Toy Story 4", "ProductionYear": 2019, "ProviderIds": {"Tmdb": "301528"}}]), "movies")
        radarr_ingest.sync_library(conn, FakeRadarr([{"id": 5, "tmdbId": 301528, "title": "Toy Story 4", "year": 2019, "monitored": True, "hasFile": True},
                                                      {"id": 6, "tmdbId": 11, "title": "Wanted", "year": 2000, "monitored": True, "hasFile": False}]))
        lb.store(conn, "alice", "watchlist", [{"slug": "toy-story-4", "title": "Toy Story 4", "year": 2019}, {"slug": "unknown-film", "title": "Unknown Film", "year": 1999}], True)
    result = tasks.reconcile_now(FakeTmdb(movies={"301528": {"id": 301528, "title": "Toy Story 4", "release_date": "2019-06-19"}, "11": {"id": 11, "title": "Wanted", "release_date": "2000-01-01"}}))
    with v2.connection() as conn:
        by_title = {r["title"]: r["state_code"] for r in conn.execute("SELECT m.title, s.state_code FROM media m JOIN item_state s ON s.target_type = 'movie' AND s.target_id = m.id")}
        assert conn.execute("SELECT COUNT(*) FROM media WHERE kind = 'movie'").fetchone()[0] == 2           # Toy Story 4 once, not twice
        assert conn.execute("SELECT COUNT(*) FROM link WHERE entity_id = (SELECT id FROM media WHERE tmdb_id = '301528') AND entity_type = 'media'").fetchone()[0] == 3
    assert by_title == {"Toy Story 4": "in_library", "Wanted": "queued"}
    assert result["matching"]["unresolved"] == 1       # "Unknown Film" has no ID and no match: listed for review, not guessed
