"""Recordings as their own state, full or short recordings, and the where-did-this-come-from lines."""
from synchub import automation, dvr, settings, sources, states
from synchub.ingest import jellyfin as jf
from tests.v2.test_dvr import timer
from tests.v2.test_ingest_jellyfin import FakeJellyfin, episode, season, series

TICKS = 10_000_000


def recording(conn, runtime_min, created="2026-10-28T06:59:00.0000000Z", name="Cold Case"):
    ser = series("ser1", name, tmdb="1")
    ser["Path"] = f"/DVR/TV Shows/{name}"
    ep = episode("ep1", "ser1", "sea1", 1, 4, Path=f"/DVR/TV Shows/{name}/S01E04.ts", DateCreated=created, RunTimeTicks=runtime_min * 60 * TICKS, SeriesName=name)
    jf.sync(conn, FakeJellyfin([ser, season("sea1", "ser1", 1), ep]), "all", now="2026-10-28 09:00:00")
    return conn.execute("SELECT * FROM src_jellyfin_item WHERE item_id = 'ep1'").fetchone()


def scheduled(conn, **kw):
    dvr.sync_timers(conn, [timer("t1", "New", name="Cold Case", start="2026-10-28T07:00:00.0000000Z", EndDate="2026-10-28T08:00:00.0000000Z", **kw)], [], now="2026-10-28 06:00:00")


def test_the_sync_saves_the_recorded_length(conn):
    assert recording(conn, 59)["runtime_seconds"] == 59 * 60


def test_a_recording_close_to_its_scheduled_length_is_full(conn):
    row = recording(conn, 61)
    scheduled(conn)
    assert dvr.completeness(conn, row) == ("full", 102)


def test_a_recording_under_90_percent_is_short(conn):
    row = recording(conn, 30)
    scheduled(conn)
    assert dvr.completeness(conn, row) == ("short", 50)
    assert dvr.completeness(conn, recording(conn, 54))[0] == "full"          # exactly 90% of 60 minutes is not short


def test_without_a_length_or_a_matching_timer_it_is_unknown(conn):
    row = recording(conn, 0)
    scheduled(conn)
    assert dvr.completeness(conn, row) == ("unknown", None)
    row = recording(conn, 60, created="2026-10-28T12:00:00.0000000Z")        # created long after the timer
    assert dvr.completeness(conn, row) == ("unknown", None)
    row = recording(conn, 60, name="Other Show")                            # no timer for this show
    assert dvr.completeness(conn, row) == ("unknown", None)


def link_episode(conn):
    """Links the recorded episode to a show's episode row and returns its id."""
    media = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'Cold Case', 1)").lastrowid
    eid = conn.execute("INSERT INTO episode (media_id, season_number, episode_number, title) VALUES (?, 1, 4, 'Churchgoing People')", (media,)).lastrowid
    conn.execute("INSERT INTO link (source, source_key, entity_type, entity_id, method, confidence) VALUES ('jellyfin_episode', 'ep1', 'episode', ?, 'test', 1)", (eid,))
    return media, eid


def test_a_recording_counts_as_in_library_by_default_and_is_recorded_when_the_setting_is_off(conn):
    recording(conn, 60)
    media, eid = link_episode(conn)
    states.compute(conn)
    assert states.season_episodes(conn, media, 1)[0]["state"] == "in_library"
    conn.commit()
    settings.set_value("dvr_counts_as_library", "0")
    states.compute(conn)
    assert states.season_episodes(conn, media, 1)[0]["state"] == "recorded"


def test_the_source_lines_name_each_source(conn):
    recording(conn, 60)
    media, eid = link_episode(conn)
    scheduled(conn)
    lines = sources.for_episodes(conn, [eid])[eid]
    assert lines == ["DVR recording in Jellyfin (full recording, recorded 2026-10-28)"]
    assert sources.for_episodes(conn, [eid + 99]) == {}


def test_an_episode_in_the_library_with_no_other_source_says_so(conn):
    ser = series("ser1", "Show", tmdb="1")
    ep = episode("ep1", "ser1", "sea1", 1, 4, Path="/media/TV/Show/S01E04.mkv")
    jf.sync(conn, FakeJellyfin([ser, season("sea1", "ser1", 1), ep]), "all", now="2026-10-28 09:00:00")
    media, eid = link_episode(conn)
    assert sources.for_episodes(conn, [eid])[eid] == ["In the Jellyfin library (source unknown)"]


def test_a_recording_alone_does_not_block_auto_add_when_it_does_not_count_as_library(conn):
    recording(conn, 60)
    media, eid = link_episode(conn)
    conn.execute("INSERT INTO link (source, source_key, entity_type, entity_id, method, confidence) VALUES ('jellyfin', 'ser1', 'media', ?, 'test', 1)", (media,))
    conn.commit()
    assert automation._ever_in_jellyfin(conn, media)
    settings.set_value("dvr_counts_as_library", "0")
    assert not automation._ever_in_jellyfin(conn, media)
