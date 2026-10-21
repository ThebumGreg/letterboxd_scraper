def _show(db, slug, title, tmdb_id=None, tvdb_id=None):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO media_catalog (tmdb_id, tvdb_id, movie_slug, title, media_type) VALUES (?,?,?,?, 'tv')", (tmdb_id, tvdb_id, slug, title))


def _cache_episode(db, tvdb_id, tmdb_id, season, episode, title):
    # Mirrors how get_season_episodes_lazy stores rows: a missing ID becomes the literal 'NONE'.
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO tv_episodes (tvdb_id, tmdb_id, season_number, episode_number, title) VALUES (?,?,?,?,?)",
            (tvdb_id or "NONE", tmdb_id or "NONE", season, episode, title),
        )


def _jf_episode(db, tvdb_id, tmdb_id, season, episode, status="in_library"):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO service_cache (service, tvdb_id, tmdb_id, media_type, season_number, episode_number, status, in_library, external_item_id) VALUES ('jellyfin',?,?,'episode',?,?,?,1,?)",
            (tvdb_id, tmdb_id, season, episode, status, f"jf-{tvdb_id}-{tmdb_id}-{season}-{episode}"),
        )


def test_season_lookup_does_not_leak_between_shows_without_tvdb_id(db):
    _show(db, "show-a", "Show A", tmdb_id="100")
    _show(db, "show-b", "Show B", tmdb_id="200")
    _cache_episode(db, None, "100", 1, 1, "A pilot")
    _cache_episode(db, None, "200", 1, 1, "B pilot")
    _cache_episode(db, None, "200", 1, 2, "B second")

    episodes = db.get_season_episodes_lazy("show-b", 1)

    assert [e["title"] for e in episodes] == ["B pilot", "B second"]


def test_season_summary_counts_only_this_show(db):
    _show(db, "show-a", "Show A", tmdb_id="100")
    _show(db, "show-b", "Show B", tmdb_id="200")
    _cache_episode(db, None, "100", 1, 1, "A1")
    _cache_episode(db, None, "100", 2, 1, "A2-season-b-should-not-appear")
    _cache_episode(db, None, "200", 1, 1, "B1")
    _cache_episode(db, None, "200", 1, 2, "B2")

    show, seasons = db.get_tv_show_details("show-b")

    assert [(s["season_number"], s["total"]) for s in seasons] == [(1, 2)]


def test_jellyfin_status_is_not_shared_between_shows(db):
    _show(db, "show-a", "Show A", tmdb_id="100")
    _show(db, "show-b", "Show B", tmdb_id="200")
    _cache_episode(db, None, "200", 1, 1, "B1")
    _jf_episode(db, None, "100", 1, 1)   # only show A's episode is in Jellyfin

    episodes = db.get_season_episodes_lazy("show-b", 1)

    assert episodes[0]["in_jellyfin"] is False


def test_show_with_tvdb_id_still_matches_by_either_id(db):
    _show(db, "show-c", "Show C", tmdb_id="300", tvdb_id="900")
    _cache_episode(db, "900", "300", 1, 1, "C1")
    _jf_episode(db, "900", None, 1, 1)

    episodes = db.get_season_episodes_lazy("show-c", 1)
    assert [(e["title"], e["in_jellyfin"]) for e in episodes] == [("C1", True)]


def test_episode_cached_under_old_and_new_id_pair_is_not_duplicated(db):
    _show(db, "show-d", "Show D", tmdb_id="400", tvdb_id="950")
    _cache_episode(db, None, "400", 1, 1, "old title")     # cached before the TVDB ID was known
    _cache_episode(db, "950", "400", 1, 1, "new title")
    _cache_episode(db, "950", "400", 1, 2, "second")

    episodes = db.get_season_episodes_lazy("show-d", 1)
    assert [e["episode_number"] for e in episodes] == [1, 2]
    assert episodes[0]["title"] == "new title"

    _, seasons = db.get_tv_show_details("show-d")
    assert seasons[0]["total"] == 2


def test_show_without_any_ids_matches_nothing_cached(db):
    _show(db, "show-e", "Show E")
    _cache_episode(db, None, None, 1, 1, "orphan")
    # no IDs -> no cache lookup; with TMDB/Sonarr unconfigured nothing can be fetched either
    assert db.get_season_episodes_lazy("show-e", 1) == []
