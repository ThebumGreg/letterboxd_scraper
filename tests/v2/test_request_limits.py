"""Requesting missing titles with limits (docs: Settings, Automation): a cap on titles downloading at once, a pace, a daily ceiling, one request per title, dry run first."""
from datetime import datetime, timedelta, timezone

import pytest

from synchub import automation, settings

from .test_automation import FakeRadarr, FakeSonarr, NOW, lb, link, movie, profile, show

T0 = "2026-02-01 12:00:00"


def later(minutes):
    return (datetime.strptime(T0, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def wanted_movies(conn, count, start=900):
    profile(conn, "A", "alice")
    ids = []
    for i in range(count):
        ids.append(movie(conn, str(start + i), f"Film {i}"))
        lb(conn, "alice", f"s{i}", ids[-1])
    conn.commit()
    return ids


def known(count, start=900):
    return tuple(str(start + i) for i in range(count))


def set_(conn, **values):
    for key, value in values.items():
        settings.set_value(key, str(value), conn=conn)
    conn.commit()


def on_queue(conn, media_id, radarr_id):
    """The film is in Radarr and downloading."""
    conn.execute("INSERT INTO src_radarr_movie (radarr_id, first_seen, last_seen) VALUES (?, ?, ?)", (radarr_id, NOW, NOW)); link(conn, "radarr", radarr_id, media_id)
    conn.execute("INSERT INTO src_arr_queue (service, queue_id, kind, target_id, first_seen, last_seen) VALUES ('radarr', ?, 'movie', ?, ?, ?)", (f"q{radarr_id}", radarr_id, NOW, NOW))
    conn.commit()


def test_a_new_install_starts_as_a_dry_run_with_the_chosen_numbers(v2):
    assert settings.get("radarr_dry_run") == "1" and settings.get("sonarr_dry_run") == "1"
    assert automation.max_in_progress("radarr") == 5 and automation.max_in_progress("sonarr") == 10


def test_titles_go_out_one_at_a_time_and_each_is_asked_for_once(conn):
    wanted_movies(conn, 3)
    set_(conn, request_gap_minutes=10)
    radarr = FakeRadarr(known=known(3))
    first = automation.run_radarr_additions(conn, radarr, now=T0)
    assert first["added"] == ["Movie 900"] and first["held"] is None
    held = automation.run_radarr_additions(conn, radarr, now=later(5))
    assert held["added"] == [] and "waiting 10 minutes" in held["held"]
    second = automation.run_radarr_additions(conn, radarr, now=later(11))
    assert second["added"] == ["Movie 901"]                                                      # never the first one again
    assert [a[0] for a in radarr.added] == ["Movie 900", "Movie 901"]


def test_no_more_than_the_cap_is_downloading_at_once(conn):
    ids = wanted_movies(conn, 8)
    set_(conn, request_gap_minutes=0, radarr_max_in_progress=3)
    radarr = FakeRadarr(known=known(8))
    assert automation.run_radarr_additions(conn, radarr, now=T0)["added"] == ["Movie 900", "Movie 901", "Movie 902"]
    assert automation.in_progress(conn, "radarr", now=T0) == 3
    result = automation.run_radarr_additions(conn, radarr, now=later(5))
    assert result["added"] == [] and result["held"] == "3 of 3 already downloading"
    # ten minutes on, the requests are no longer "just asked for": only the ones really downloading count
    for n, mid in enumerate(ids[:3]):
        on_queue(conn, mid, f"r{n}")
    assert automation.in_progress(conn, "radarr", now=later(120)) == 3
    assert automation.run_radarr_additions(conn, radarr, now=later(120))["held"] == "3 of 3 already downloading"
    conn.execute("UPDATE src_arr_queue SET removed_at = ? WHERE queue_id = 'qr0'", (NOW,)); conn.commit()          # one finished
    assert automation.in_progress(conn, "radarr", now=later(120)) == 2
    assert automation.run_radarr_additions(conn, radarr, now=later(120))["added"] == ["Movie 903"]


def test_titles_not_requested_by_synchub_do_not_count(conn):
    ids = wanted_movies(conn, 2)
    set_(conn, request_gap_minutes=0, radarr_max_in_progress=1)
    on_queue(conn, ids[0], "r0")                                                                  # downloading, but someone added it by hand
    assert automation.in_progress(conn, "radarr", now=T0) == 0
    assert automation.run_radarr_additions(conn, FakeRadarr(known=known(2)), now=T0)["added"] == ["Movie 901"]       # 900 is in Radarr already


def test_the_daily_ceiling_stops_requests_for_the_day(conn):
    wanted_movies(conn, 5)
    set_(conn, request_gap_minutes=0, radarr_max_in_progress=50, request_daily_max=2)
    radarr = FakeRadarr(known=known(5))
    assert len(automation.run_radarr_additions(conn, radarr, now=T0)["added"]) == 2
    held = automation.run_radarr_additions(conn, radarr, now=later(60))
    assert held["added"] == [] and "daily ceiling of 2" in held["held"]
    assert len(automation.run_radarr_additions(conn, radarr, now=later(25 * 60))["added"]) == 2


def test_the_most_wanted_come_first_then_the_oldest(conn):
    profile(conn, "A", "alice"); profile(conn, "B", "bob")
    old, popular, newer = movie(conn, "900", "Old"), movie(conn, "901", "Popular"), movie(conn, "902", "Newer")
    lb(conn, "alice", "a", old); lb(conn, "alice", "b", popular); lb(conn, "bob", "b", popular); lb(conn, "alice", "c", newer)
    conn.commit()
    set_(conn, request_gap_minutes=0, radarr_max_in_progress=3)
    assert automation.run_radarr_additions(conn, FakeRadarr(known=known(3)), now=T0)["added"] == ["Movie 901", "Movie 900", "Movie 902"]


def test_a_dry_run_lists_what_it_would_request_and_changes_nothing(conn):
    wanted_movies(conn, 4)
    set_(conn, request_gap_minutes=10, radarr_max_in_progress=5)
    radarr = FakeRadarr(known=known(4))
    result = automation.run_radarr_additions(conn, radarr, dry_run=True, now=T0)
    assert result["would_add"] == ["Film 0"]                                                       # one at a time, like the real thing
    assert radarr.added == [] and conn.execute("SELECT COUNT(*) FROM auto_request").fetchone()[0] == 0
    assert automation.run_radarr_additions(conn, radarr, dry_run=True, now=T0)["would_add"] == result["would_add"]


def test_a_title_the_service_cannot_find_is_left_alone_for_a_day_so_it_does_not_block_the_rest(conn):
    wanted_movies(conn, 3)
    set_(conn, request_gap_minutes=10)
    radarr = FakeRadarr(known=("901", "902"))                                                      # 900 is not known to Radarr
    first = automation.run_radarr_additions(conn, radarr, now=T0)
    assert first["added"] == ["Movie 901"] and len(first["errors"]) == 1
    radarr.lookups.clear()
    assert automation.run_radarr_additions(conn, radarr, now=later(11))["added"] == ["Movie 902"] and "900" not in radarr.lookups


def test_the_status_line(conn):
    wanted_movies(conn, 1)
    set_(conn, request_gap_minutes=10)
    automation.run_radarr_additions(conn, FakeRadarr(known=known(1)), now=T0)
    status = automation.request_status(conn, now=T0)
    assert status["text"] == "Requested: 1 of 5 movies, 0 of 10 shows downloading" and status["radarr"]["today"] == 1


def test_shows_have_their_own_cap_and_a_show_downloading_an_episode_counts(conn):
    profile(conn, "A", sz="alice_sz")
    ids = []
    for i in range(3):
        mid = show(conn, str(i), str(100 + i), f"Show {i}")
        conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, first_seen, last_seen) VALUES ('alice_sz', ?, ?, ?)", (f"z{i}", NOW, NOW)); link(conn, "serializd", f"z{i}", mid)
        ids.append(mid)
    conn.commit()
    set_(conn, request_gap_minutes=0, sonarr_max_in_progress=2)
    sonarr = FakeSonarr(known=("100", "101", "102"))
    assert automation.run_sonarr_additions(conn, sonarr, now=T0)["added"] == ["Show 0", "Show 1"]
    assert automation.run_sonarr_additions(conn, sonarr, now=later(5))["held"] == "2 of 2 already downloading"
    eid = conn.execute("INSERT INTO episode (media_id, season_number, episode_number) VALUES (?, 1, 1)", (ids[0],)).lastrowid
    link(conn, "sonarr_episode", "e1", eid, entity="episode")
    conn.execute("INSERT INTO src_arr_queue (service, queue_id, kind, target_id, first_seen, last_seen) VALUES ('sonarr', 'q1', 'episode', 'e1', ?, ?)", (NOW, NOW)); conn.commit()
    assert automation.in_progress(conn, "sonarr", now=later(120)) == 1                             # Show 0 is downloading; Show 1 has nothing in the queue yet
    assert automation.run_sonarr_additions(conn, sonarr, now=later(120))["added"] == ["Show 2"]


def test_a_title_removed_from_radarr_on_purpose_is_not_asked_for_again(conn):
    ids = wanted_movies(conn, 1)
    set_(conn, request_gap_minutes=0)
    radarr = FakeRadarr(known=known(1))
    assert automation.run_radarr_additions(conn, radarr, now=T0)["added"] == ["Movie 900"]
    assert automation.run_radarr_additions(conn, radarr, now=later(24 * 60))["added"] == [] and len(radarr.added) == 1
