import pytest

from synchub import states

NOW = "2026-01-01 00:00:00"
TODAY = "2026-06-15"


def movie(conn, tmdb="1"):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'M', ?)", (tmdb,)).lastrowid


def show(conn, tmdb="100"):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'S', ?)", (tmdb,)).lastrowid


def episode(conn, media_id, s, e, air="2020-01-01"):
    return conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date) VALUES (?,?,?,?)", (media_id, s, e, air)).lastrowid


def link(conn, entity_type, entity_id, source, key):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES (?,?,?,?, 'x', 1.0)", (entity_type, entity_id, source, key))


def jf_movie(conn, item_id, removed=None):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen, removed_at) VALUES (?, 'movie', ?, ?, ?)", (item_id, NOW, NOW, removed))


def radarr(conn, rid, monitored=1, has_file=0, removed=None):
    conn.execute("INSERT INTO src_radarr_movie (radarr_id, monitored, has_file, first_seen, last_seen, removed_at) VALUES (?,?,?,?,?,?)", (rid, monitored, has_file, NOW, NOW, removed))


def jf_ep(conn, item_id, removed=None):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen, removed_at) VALUES (?, 'episode', ?, ?, ?)", (item_id, NOW, NOW, removed))


def sonarr_ep(conn, eid, monitored=1, has_file=0):
    conn.execute("INSERT INTO src_sonarr_episode (sonarr_episode_id, sonarr_id, monitored, has_file, first_seen, last_seen) VALUES (?, '1', ?, ?, ?, ?)", (eid, monitored, has_file, NOW, NOW))


def queue(conn, service, qid, kind, target, status="downloading", progress=0.5, removed=None):
    conn.execute("INSERT INTO src_arr_queue (service, queue_id, kind, target_id, status, progress, title, first_seen, last_seen, removed_at) VALUES (?,?,?,?,?,?,'t',?,?,?)",
                 (service, qid, kind, target, status, progress, NOW, NOW, removed))


def state_of(conn, target_type, target_id):
    row = conn.execute("SELECT state_code, progress FROM item_state WHERE target_type = ? AND target_id = ?", (target_type, target_id)).fetchone()
    return (row["state_code"], row["progress"])


def test_movie_state_ladder(conn):
    m = {name: movie(conn, str(i)) for i, name in enumerate(["bare", "wanted", "file", "both", "gone"])}
    radarr(conn, "w", monitored=1, has_file=0); link(conn, "media", m["wanted"], "radarr", "w")
    radarr(conn, "f", monitored=1, has_file=1); link(conn, "media", m["file"], "radarr", "f")
    radarr(conn, "b", monitored=1, has_file=1); link(conn, "media", m["both"], "radarr", "b")
    jf_movie(conn, "jb"); link(conn, "media", m["both"], "jellyfin", "jb")
    jf_movie(conn, "jg", removed=NOW); link(conn, "media", m["gone"], "jellyfin", "jg")      # deleted from Jellyfin
    states.compute(conn, NOW)
    assert [state_of(conn, "movie", m[k])[0] for k in ("bare", "wanted", "file", "both", "gone")] == ["missing", "queued", "downloaded", "in_library", "missing"]


def test_removed_radarr_movie_no_longer_counts(conn):
    m = movie(conn)
    radarr(conn, "r", monitored=1, has_file=1, removed=NOW); link(conn, "media", m, "radarr", "r")
    states.compute(conn, NOW)
    assert state_of(conn, "movie", m)[0] == "missing"


def test_episode_state_from_jellyfin_and_sonarr(conn):
    sid = show(conn)
    e_lib, e_file, e_mon, e_none = (episode(conn, sid, 1, n) for n in (1, 2, 3, 4))
    jf_ep(conn, "j1"); link(conn, "episode", e_lib, "jellyfin_episode", "j1")
    sonarr_ep(conn, "s2", 1, 1); link(conn, "episode", e_file, "sonarr_episode", "s2")
    sonarr_ep(conn, "s3", 1, 0); link(conn, "episode", e_mon, "sonarr_episode", "s3")
    states.compute(conn, NOW)
    assert [state_of(conn, "episode", e)[0] for e in (e_lib, e_file, e_mon, e_none)] == ["in_library", "downloaded", "queued", "missing"]


def test_queue_entries_become_events_and_drive_state(conn):
    m = movie(conn)
    radarr(conn, "r1"); link(conn, "media", m, "radarr", "r1")
    queue(conn, "radarr", "q1", "movie", "r1", "downloading", 0.4)
    sid = show(conn); e = episode(conn, sid, 1, 1)
    sonarr_ep(conn, "s1"); link(conn, "episode", e, "sonarr_episode", "s1")
    queue(conn, "sonarr", "q9", "episode", "s1", "importPending", 1.0)
    assert states.sync_queue_events(conn, NOW) == {"opened": 2, "updated": 0, "closed": 0, "unlinked": 0, "ignored": 0}
    states.compute(conn, NOW)
    assert state_of(conn, "movie", m) == ("downloading", 0.4)
    assert state_of(conn, "episode", e) == ("downloaded", 1.0)


def test_event_progress_updates_and_closing_returns_state_to_baseline(conn):
    m = movie(conn)
    radarr(conn, "r1"); link(conn, "media", m, "radarr", "r1")
    queue(conn, "radarr", "q1", "movie", "r1", "downloading", 0.2)
    states.sync_queue_events(conn, NOW); states.compute(conn, NOW)
    conn.execute("UPDATE src_arr_queue SET progress = 0.9")
    assert states.sync_queue_events(conn, "2026-01-01 00:05:00") == {"opened": 0, "updated": 1, "closed": 0, "unlinked": 0, "ignored": 0}
    states.compute(conn, "2026-01-01 00:05:00")
    assert state_of(conn, "movie", m) == ("downloading", 0.9)
    conn.execute("UPDATE src_arr_queue SET removed_at = '2026-01-01 00:10:00'")
    assert states.sync_queue_events(conn, "2026-01-01 00:10:00")["closed"] == 1
    states.compute(conn, "2026-01-01 00:10:00")
    assert state_of(conn, "movie", m)[0] == "queued"      # still monitored in Radarr, no longer downloading
    assert conn.execute("SELECT COUNT(*) FROM processing_event WHERE ended_at IS NULL").fetchone()[0] == 0


def test_failed_and_unlinked_queue_entries_are_not_events(conn):
    m = movie(conn)
    radarr(conn, "r1"); link(conn, "media", m, "radarr", "r1")
    queue(conn, "radarr", "bad", "movie", "r1", "failed")
    queue(conn, "radarr", "orphan", "movie", "nobody", "downloading")
    assert states.sync_queue_events(conn, NOW) == {"opened": 0, "updated": 0, "closed": 0, "unlinked": 1, "ignored": 1}


def test_custom_providers_can_add_states_without_schema_changes(conn):
    """A future encode script writes an event; a brand-new state is just a state_def row."""
    m = movie(conn)
    jf_movie(conn, "j"); link(conn, "media", m, "jellyfin", "j")
    conn.execute("INSERT INTO processing_event (target_type, target_id, provider, state_code, progress, external_ref) VALUES ('movie', ?, 'encode_script', 'encoding', 0.3, 'job-1')", (m,))
    states.compute(conn, NOW)
    assert state_of(conn, "movie", m)[0] == "in_library"         # already in Jellyfin: library outranks in-flight work
    conn.execute("UPDATE src_jellyfin_item SET removed_at = ?", (NOW,))
    states.compute(conn, NOW)
    assert state_of(conn, "movie", m) == ("encoding", 0.3)
    conn.execute("INSERT INTO state_def (code, label, rank) VALUES ('verifying', 'Verifying', 55)")
    conn.execute("INSERT INTO processing_event (target_type, target_id, provider, state_code, external_ref) VALUES ('movie', ?, 'verify_script', 'verifying', 'v1')", (m,))
    states.compute(conn, NOW)
    assert state_of(conn, "movie", m)[0] == "verifying"          # outranks encoding by its rank alone
    assert states.definitions(conn)["verifying"]["label"] == "Verifying"


def test_strongest_of_several_events_wins(conn):
    m = movie(conn)
    for provider, code in (("a", "downloading"), ("b", "transferring")):
        conn.execute("INSERT INTO processing_event (target_type, target_id, provider, state_code) VALUES ('movie', ?, ?, ?)", (m, provider, code))
    states.compute(conn, NOW)
    assert state_of(conn, "movie", m)[0] == "transferring"


def test_compute_updates_in_place_and_only_touches_changed_rows(conn):
    m = movie(conn)
    radarr(conn, "r", 1, 0); link(conn, "media", m, "radarr", "r")
    first = states.compute(conn, "2026-01-01 00:00:00")
    again = states.compute(conn, "2026-01-02 00:00:00")
    assert first["changed"] == 1 and again["changed"] == 0
    assert conn.execute("SELECT updated_at FROM item_state").fetchone()[0] == "2026-01-01 00:00:00"
    conn.execute("UPDATE src_radarr_movie SET has_file = 1")
    assert states.compute(conn, "2026-01-03 00:00:00")["changed"] == 1
    assert conn.execute("SELECT updated_at FROM item_state").fetchone()[0] == "2026-01-03 00:00:00"
    assert conn.execute("SELECT COUNT(*) FROM item_state").fetchone()[0] == 1


# ---------------------------------------------------------------- rollups
def build_show(conn):
    sid = show(conn)
    spec = episode(conn, sid, 0, 1, "2010-01-01")
    spec2 = episode(conn, sid, 0, 2, "2010-02-01")
    s1 = [episode(conn, sid, 1, n, "2020-01-01") for n in (1, 2, 3)]
    s2 = [episode(conn, sid, 2, 1, "2021-01-01"), episode(conn, sid, 2, 2, "2099-01-01"), episode(conn, sid, 2, 3, None)]   # one unaired, one without a date
    for i, e in enumerate((s1[0], s1[1], spec)):
        jf_ep(conn, f"j{i}"); link(conn, "episode", e, "jellyfin_episode", f"j{i}")
    sonarr_ep(conn, "sn", 1, 0); link(conn, "episode", s1[2], "sonarr_episode", "sn")
    states.compute(conn, NOW)
    return sid


def test_show_summary_excludes_specials_and_unaired_from_totals(conn):
    sid = build_show(conn)
    summary = states.show_summary(conn, sid, TODAY)
    assert [s["label"] for s in summary["seasons"]] == ["Season 1", "Season 2", "Specials (Season 0)"]      # Specials last
    totals = summary["totals"]
    assert totals["episodes"] == 6 and totals["aired"] == 4 and totals["in_library"] == 2      # specials and 2 unaired left out
    assert totals["counts"] == {"in_library": 2, "queued": 1, "missing": 1, "not_aired": 2}
    assert totals["percent"] == 50 and totals["state"] == "partial"


def test_season_rollups(conn):
    sid = build_show(conn)
    s1, s2, specials = states.show_summary(conn, sid, TODAY)["seasons"]
    assert (s1["in_library"], s1["aired"], s1["state"]) == (2, 3, "partial")
    assert (s2["aired"], s2["in_library"], s2["state"], s2["counts"]) == (1, 0, "missing", {"missing": 1, "not_aired": 2})
    assert specials["is_special"] and specials["episodes"] == 2 and specials["in_library"] == 1


def test_unaired_depends_on_today(conn):
    sid = build_show(conn)
    later = states.show_summary(conn, sid, "2100-01-01")["totals"]
    assert later["aired"] == 5 and later["counts"]["missing"] == 2 and later["counts"]["not_aired"] == 1      # the undated one stays "not aired"


def test_whole_show_in_library_and_all_unaired(conn):
    sid = show(conn)
    es = [episode(conn, sid, 1, n) for n in (1, 2)]
    for i, e in enumerate(es):
        jf_ep(conn, f"x{i}"); link(conn, "episode", e, "jellyfin_episode", f"x{i}")
    states.compute(conn, NOW)
    assert states.show_summary(conn, sid, TODAY)["totals"]["state"] == "in_library"
    upcoming = show(conn, "200"); episode(conn, upcoming, 1, 1, "2099-01-01")
    states.compute(conn, NOW)
    assert states.show_summary(conn, upcoming, TODAY)["totals"]["state"] == "not_aired"


def test_in_flight_state_shows_as_the_rollup_when_nothing_is_in_library(conn):
    sid = show(conn)
    e = episode(conn, sid, 1, 1)
    sonarr_ep(conn, "s1"); link(conn, "episode", e, "sonarr_episode", "s1")
    queue(conn, "sonarr", "q", "episode", "s1", "downloading", 0.1)
    states.sync_queue_events(conn, NOW); states.compute(conn, NOW)
    assert states.show_summary(conn, sid, TODAY)["totals"]["state"] == "downloading"


def test_show_without_episodes_and_season_episode_listing(conn):
    assert states.show_summary(conn, show(conn, "900"), TODAY) == {"seasons": [], "totals": {"counts": {}, "episodes": 0, "aired": 0, "in_library": 0, "percent": 0, "state": "missing"}}
    sid = build_show(conn)
    listing = states.season_episodes(conn, sid, 2, TODAY)
    assert [(e["episode_number"], e["state"]) for e in listing] == [(1, "missing"), (2, "not_aired"), (3, "not_aired")]
    assert [e["state"] for e in states.season_episodes(conn, sid, 0, TODAY)] == ["in_library", "missing"]
    assert states.definitions(conn)["not_aired"]["label"] == "Not aired"


def test_episodes_missing_from_item_state_default_to_missing(conn):
    sid = show(conn)
    episode(conn, sid, 1, 1)         # compute() has never run
    assert states.show_summary(conn, sid, TODAY)["totals"]["counts"] == {"missing": 1}
