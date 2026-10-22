from synchub.ingest import jellyfin as jf


class FakeJellyfin:
    def __init__(self, items):
        self.items = items
        self.requested = None

    def iter_items(self, types):
        from synchub import db
        db.assert_not_in_write_transaction("fake Jellyfin items")
        self.requested = tuple(types)
        return iter([i for i in self.items if i["Type"] in types])


def series(id_, name, tvdb=None, tmdb=None):
    ids = {k: v for k, v in (("Tvdb", tvdb), ("Tmdb", tmdb)) if v}
    return {"Id": id_, "Type": "Series", "Name": name, "ProductionYear": 2010, "ProviderIds": ids}


def season(id_, series_id, number, via="SeriesId"):
    item = {"Id": id_, "Type": "Season", "Name": f"Season {number}", "IndexNumber": number, "ParentId": series_id}
    if via == "SeriesId":
        item["SeriesId"] = series_id
    return item


def episode(id_, series_id, season_id, s, e, name="Ep", tvdb=None, **extra):
    return {"Id": id_, "Type": "Episode", "Name": name, "SeriesName": "Show", "SeriesId": series_id, "SeasonId": season_id,
            "ParentIndexNumber": s, "IndexNumber": e, "ProviderIds": {"Tvdb": tvdb} if tvdb else {}, **extra}


def rows(conn, where="1=1"):
    return {r["item_id"]: dict(r) for r in conn.execute(f"SELECT * FROM src_jellyfin_item WHERE {where}")}


def test_episodes_link_to_their_series_even_with_their_own_provider_ids(conn):
    """The production bug: episode TVDB IDs are episode-level IDs that do not equal the series' TVDB ID."""
    client = FakeJellyfin([
        series("ser1", "Show", tvdb="269533", tmdb="60802"),
        season("sea1", "ser1", 3),
        episode("ep1", "ser1", "sea1", 3, 10, tvdb="5648401"),
        episode("ep2", "ser1", "sea1", 3, 11, tvdb="5648402"),
    ])
    result = jf.sync(conn, client, "tv", now="2026-01-01 00:00:00")
    got = rows(conn)
    assert result == {"seen": 4, "removed": 0}
    assert got["ep1"]["series_id"] == "ser1" and got["ep1"]["tvdb_id"] == "5648401"   # episode keeps its own ID...
    assert got["ep1"]["series_id"] == got["ser1"]["item_id"]                           # ...and still reaches the series
    assert (got["ep1"]["season_number"], got["ep1"]["episode_number"]) == (3, 10)
    assert got["sea1"]["season_number"] == 3 and got["sea1"]["series_id"] == "ser1"


def test_episode_without_seriesid_is_resolved_through_its_season(conn):
    ep = episode("ep1", None, None, 1, 1)
    ep.pop("SeriesId"); ep.pop("SeasonId"); ep["ParentId"] = "sea1"
    jf.sync(conn, FakeJellyfin([series("ser1", "Show"), season("sea1", "ser1", 1, via="ParentId"), ep]), "tv")
    assert rows(conn)["ep1"]["series_id"] == "ser1"


def test_episode_directly_under_series_when_there_is_no_season_folder(conn):
    ep = {"Id": "ep1", "Type": "Episode", "Name": "x", "ParentId": "ser1", "ParentIndexNumber": 1, "IndexNumber": 1}
    jf.sync(conn, FakeJellyfin([series("ser1", "Show"), ep]), "tv")
    assert rows(conn)["ep1"]["series_id"] == "ser1"


def test_specials_season_zero(conn):
    jf.sync(conn, FakeJellyfin([series("ser1", "Show"), season("sea0", "ser1", 0), episode("sp1", "ser1", "sea0", 0, 1)]), "tv")
    assert rows(conn)["sp1"]["season_number"] == 0 and rows(conn)["sea0"]["season_number"] == 0


def test_movies_and_provider_id_case_variants(conn):
    movie = {"Id": "m1", "Type": "Movie", "Name": "Heat", "ProductionYear": 1995, "PremiereDate": "1995-12-15T00:00:00.0000000Z",
             "ProviderIds": {"tmdb": "949", "IMDB": "tt0113277", "Tvdb": "0"}}
    jf.sync(conn, FakeJellyfin([movie]), "movies")
    m = rows(conn)["m1"]
    assert (m["tmdb_id"], m["imdb_id"], m["tvdb_id"], m["premiere_date"], m["year"]) == ("949", "tt0113277", None, "1995-12-15", 1995)


def test_scope_limits_what_is_fetched_and_removed(conn):
    movie = {"Id": "m1", "Type": "Movie", "Name": "Heat"}
    both = [movie, series("ser1", "Show")]
    jf.sync(conn, FakeJellyfin(both), "all", now="2026-01-01 00:00:00")
    client = FakeJellyfin([series("ser1", "Show")])
    result = jf.sync(conn, client, "tv", now="2026-01-02 00:00:00")
    assert client.requested == ("Series", "Season", "Episode")
    assert result["removed"] == 0
    assert rows(conn)["m1"]["removed_at"] is None          # movies were out of scope


def test_unreported_items_are_marked_removed_not_deleted_and_can_return(conn):
    jf.sync(conn, FakeJellyfin([series("a", "A"), series("b", "B")]), "tv", now="2026-01-01 00:00:00")
    result = jf.sync(conn, FakeJellyfin([series("a", "A")]), "tv", now="2026-01-02 00:00:00")
    got = rows(conn)
    assert result["removed"] == 1 and got["b"]["removed_at"] == "2026-01-02 00:00:00" and got["a"]["removed_at"] is None
    assert got["b"]["first_seen"] == "2026-01-01 00:00:00"
    jf.sync(conn, FakeJellyfin([series("a", "A"), series("b", "B")]), "tv", now="2026-01-03 00:00:00")
    assert rows(conn)["b"]["removed_at"] is None and rows(conn)["b"]["first_seen"] == "2026-01-01 00:00:00"


def test_empty_response_removes_nothing(conn):
    jf.sync(conn, FakeJellyfin([series("a", "A")]), "tv", now="2026-01-01 00:00:00")
    assert jf.sync(conn, FakeJellyfin([]), "tv", now="2026-01-02 00:00:00")["removed"] == 0
    assert rows(conn)["a"]["removed_at"] is None


def test_failed_fetch_leaves_database_untouched(conn):
    jf.sync(conn, FakeJellyfin([series("a", "A")]), "tv", now="2026-01-01 00:00:00")

    class Broken:
        def iter_items(self, types):
            yield series("a", "A renamed")
            raise RuntimeError("HTTP 500")

    try:
        jf.sync(conn, Broken(), "tv", now="2026-01-02 00:00:00")
    except RuntimeError:
        pass
    got = rows(conn)["a"]
    assert got["name"] == "A" and got["last_seen"] == "2026-01-01 00:00:00"


def test_resync_is_idempotent(conn):
    items = [series("ser1", "Show"), season("sea1", "ser1", 1), episode("ep1", "ser1", "sea1", 1, 1)]
    jf.sync(conn, FakeJellyfin(items), "tv", now="2026-01-01 00:00:00")
    before = rows(conn)
    jf.sync(conn, FakeJellyfin(items), "tv", now="2026-01-01 00:00:00")
    assert rows(conn) == before
