"""Jellyfin as the DVR: where items live, which are recordings, Jellyfin's schedule, the numbers about both, and the library folder overlap check."""
import pytest
from tests.v2.test_tv import den, tv  # noqa: F401  (the TV the Record route is posted for)

from synchub import dvr, library_paths, settings, status
from synchub.ingest import jellyfin as jf
from tests.v2.test_ingest_jellyfin import FakeJellyfin, episode, season, series


def item(**extra):
    return {"Id": "m1", "Type": "Movie", "Name": "Film", "ProductionYear": 2020, "ProviderIds": {}, **extra}


def rows(conn):
    return {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM src_jellyfin_item")}


def test_paths_inside_a_recording_folder_are_recordings():
    folders = ["/DVR/Movies", "/DVR/TV Shows"]
    assert dvr.is_dvr_path("/DVR/Movies/Heat (1995)/Heat.mpg", folders)
    assert dvr.is_dvr_path("/DVR/TV Shows", folders)
    assert not dvr.is_dvr_path("/DVR/Movies2/x.mkv", folders)         # a folder that only starts with the same letters
    assert not dvr.is_dvr_path("/dvr/Movies/x.mkv", folders)          # Linux paths are case-sensitive
    assert not dvr.is_dvr_path(None, folders) and not dvr.is_dvr_path("/TV1/x.mkv", [])


def test_the_sync_saves_each_items_path_and_marks_recordings(conn):
    settings.set_value("dvr_path_prefixes", "/DVR")
    ser = series("ser1", "Show", tmdb="1")
    ser["Path"] = "/DVR/TV Shows/Show"
    ep = episode("ep1", "ser1", "sea1", 1, 1, Path="/DVR/TV Shows/Show/S01E01.mpg")
    keep = item(Id="m1", Path="/media/Film (2020)/Film.mkv")
    jf.sync(conn, FakeJellyfin([ser, season("sea1", "ser1", 1), ep, keep]), "all", now="2026-01-01 00:00:00")
    got = rows(conn)
    assert (got["ep1"]["path"], got["ep1"]["is_dvr"]) == ("/DVR/TV Shows/Show/S01E01.mpg", 1)
    assert got["ser1"]["is_dvr"] == 1 and got["m1"]["is_dvr"] == 0 and got["m1"]["path"].endswith("Film.mkv")


def test_changing_the_recording_folders_changes_the_flag_on_the_next_sync(conn):
    movie = item(Path="/DVR/Movies/Film/Film.mpg")
    settings.set_value("dvr_path_prefixes", "/DVR")
    jf.sync(conn, FakeJellyfin([movie]), "movies")
    conn.commit()
    assert rows(conn)["m1"]["is_dvr"] == 1
    settings.set_value("dvr_path_prefixes", "/Other")
    jf.sync(conn, FakeJellyfin([movie]), "movies")
    assert rows(conn)["m1"]["is_dvr"] == 0


def test_the_prefix_setting_is_checked():
    from settings_validation import validate_setting
    assert validate_setting("dvr_path_prefixes", "/DVR/Movies/, /DVR/TV Shows") == (True, "/DVR/Movies,/DVR/TV Shows")
    assert validate_setting("dvr_path_prefixes", "DVR")[0] is False
    assert validate_setting("dvr_enabled", "1") == (True, "1") and validate_setting("dvr_poll_seconds", "x")[0] is False


def timer(id_, status_, name="Cold Case", start="2099-01-01T07:00:00.0000000Z", **extra):
    return {"Id": id_, "Type": "Timer", "Name": name, "ChannelName": "TBS", "ProgramId": "p" + id_, "Status": status_, "StartDate": start, "EndDate": "2099-01-01T08:00:00.0000000Z", **extra}


def test_the_schedule_is_saved_and_timers_jellyfin_drops_are_marked_removed(conn):
    first = [timer("t1", "New"), timer("t2", "InProgress"), timer("t3", "Error"), timer("t4", "ConflictedOk")]
    rules = [{"Id": "s1", "Type": "SeriesTimer", "Name": "Cold Case", "ChannelName": "TBS"}]
    assert dvr.sync_timers(conn, first, rules, now="2026-01-01 00:00:00") == {"timers": 4, "series": 1}
    s = dvr.stats(conn, now="2026-01-01 00:00:00")
    assert (s["scheduled"], s["recording_now"], s["failed"], s["series_rules"]) == (2, 1, 1, 1)
    assert s["next"]["name"] == "Cold Case"
    dvr.sync_timers(conn, [timer("t1", "Completed")], [], now="2026-01-02 00:00:00")           # an empty rule list is a real answer
    s = dvr.stats(conn, now="2026-01-02 00:00:00")
    assert (s["scheduled"], s["recording_now"], s["failed"], s["series_rules"]) == (0, 0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM dvr_timer WHERE removed_at IS NOT NULL").fetchone()[0] == 4


def test_the_numbers_count_recordings_and_the_summary_reads_in_words(conn):
    settings.set_value("dvr_path_prefixes", "/DVR")
    items = [series("ser1", "Show", tmdb="1"), season("sea1", "ser1", 1),
             episode("ep1", "ser1", "sea1", 1, 1, Path="/DVR/TV Shows/Show/1.mpg"), episode("ep2", "ser1", "sea1", 1, 2, Path="/DVR/TV Shows/Show/2.mpg"),
             episode("ep3", "ser1", "sea1", 1, 3, Path="/TV1/Show/3.mkv"), item(Path="/DVR/Movies/Film/Film.mpg")]
    jf.sync(conn, FakeJellyfin(items), "all")
    dvr.sync_timers(conn, [timer("t1", "New")], [{"Id": "s1", "Name": "Show"}])
    s = dvr.stats(conn, now="2026-01-01 00:00:00")
    assert (s["episodes"], s["movies"], s["shows"]) == (2, 1, 1)
    line = dvr.summary(s)
    assert "1 scheduled" in line and "1 series rule" in line and "3 recordings in the library (1 show, 1 movie)" in line


def test_the_status_page_shows_the_dvr_only_when_it_is_switched_on(conn, v2):
    from synchub import db
    with db.connection() as c:
        assert not [i for i in status.snapshot(c)["items"] if i["label"] == "Jellyfin DVR"]
        settings.set_value("dvr_enabled", "1", conn=c)
        dvr.sync_timers(c, [timer("t1", "Error")], [])
        shown = [i for i in status.snapshot(c)["items"] if i["label"] == "Jellyfin DVR"]
    assert len(shown) == 1 and shown[0]["state"] == "warn" and "1 failed" in shown[0]["detail"]


def test_the_sync_task_reads_the_schedule_and_never_writes_to_jellyfin(v2, monkeypatch):
    from synchub import db, tasks

    class Fake:
        def timers(self):
            return [timer("t1", "New")]

        def series_timers(self):
            return []

    monkeypatch.setattr(tasks, "JellyfinClient", Fake)
    assert "1 timer(s), 0 series rule(s)" in tasks.sync_dvr()
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM dvr_timer").fetchone()[0] == 1


def test_the_schedule_job_runs_only_when_switched_on(v2):
    from synchub import scheduler
    settings.set_value("dvr_enabled", "0")
    scheduler.configure()
    assert scheduler.scheduler.get_job("job_dvr") is None
    settings.set_value("dvr_enabled", "1")
    settings.set_value("dvr_poll_seconds", "10")
    scheduler.configure()
    assert scheduler.scheduler.get_job("job_dvr").trigger.interval.total_seconds() == 60            # a typo cannot hammer Jellyfin
    scheduler.scheduler.remove_all_jobs()


# ---------------------------------------------------------------- overlapping library folders
LIBRARIES = [
    {"Name": "Movies", "CollectionType": "movies", "Locations": ["/KidsMovie", "/media"]},
    {"Name": "Kids Movies", "CollectionType": "movies", "Locations": ["/KidsMovie"]},
    {"Name": "TV Shows", "CollectionType": "tvshows", "Locations": ["/DVR/TV Shows", "/TV1"]},
    {"Name": "DVR Shows", "CollectionType": "tvshows", "Locations": ["/DVR/TV Shows"]},
    {"Name": "Recordings", "Locations": ["/DVR"]},
]


def test_overlapping_libraries_are_found():
    found = library_paths.find_overlaps(LIBRARIES)
    same = {f["path"]: f["libraries"] for f in found if f["kind"] == "same_path"}
    assert same == {"/KidsMovie": ["Kids Movies", "Movies"], "/DVR/TV Shows": ["DVR Shows", "TV Shows"]}
    mixed = [f for f in found if f["kind"] == "mixed_parent"]
    assert {(f["path"], tuple(f["libraries"])) for f in mixed} == {("/DVR/TV Shows", ("DVR Shows", "Recordings")), ("/DVR/TV Shows", ("Recordings", "TV Shows"))}


def test_folders_that_differ_only_in_case_and_clean_libraries():
    found = library_paths.find_overlaps([{"Name": "A", "CollectionType": "movies", "Locations": ["/mnt/tower/Media"]},
                                         {"Name": "B", "CollectionType": "movies", "Locations": ["/mnt/tower/media/"]}])
    assert [f["kind"] for f in found] == ["case_only"]
    assert library_paths.find_overlaps([{"Name": "A", "Locations": ["/media"]}, {"Name": "B", "Locations": ["/media2"]}]) == []
    assert library_paths.find_overlaps(None) == []


def test_the_check_endpoint_reads_jellyfin_and_reports(app, monkeypatch):
    from synchub.clients.jellyfin import JellyfinClient
    monkeypatch.setattr(JellyfinClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(JellyfinClient, "virtual_folders", lambda self: LIBRARIES)
    body = app.test_client().get("/api/tools/library-paths").get_json()
    assert body["ok"] and len(body["libraries"]) == 5 and body["overlaps"]


def test_the_check_endpoint_says_when_jellyfin_is_not_set_up(app):
    res = app.test_client().get("/api/tools/library-paths")
    assert res.status_code == 502 and "not configured" in res.get_json()["message"]


class FakeLiveClient:
    def __init__(self, sessions, channel):
        self._sessions, self._channel = sessions, channel

    def sessions(self):
        return self._sessions

    def channel(self, channel_id):
        assert channel_id == "ch1"
        return self._channel


def test_live_session_details_keep_the_useful_fields_and_drop_keys_and_addresses():
    live = {"UserName": "greg", "Client": "Jellyfin Web", "DeviceName": "Firefox", "RemoteEndPoint": "10.0.0.5", "AccessToken": "SECRET", "DeviceId": "dev1",
            "PlayState": {"PositionTicks": 5, "IsPaused": False, "PlayMethod": "Transcode", "MediaSourceId": "x"},
            "NowPlayingItem": {"Id": "ch1", "Name": "TNT", "Type": "TvChannel", "IsLive": True, "Path": "http://tuner/secret?token=1"}}
    idle = {"UserName": "ann", "NowPlayingItem": {"Id": "m1", "Name": "A film", "Type": "Movie"}}
    channel = {"Id": "ch1", "Name": "TNT", "Number": "10", "CurrentProgram": {"Id": "p1", "Name": "Cold Case", "EpisodeTitle": "Ep", "StartDate": "2026-11-01T15:00:00Z", "Overview": "x"}}
    out = dvr.live_session_details(FakeLiveClient([idle, live], channel))
    assert len(out) == 1 and out[0]["user"] == "greg"
    assert out[0]["on_air_now"] == {"Id": "p1", "Name": "Cold Case", "EpisodeTitle": "Ep", "StartDate": "2026-11-01T15:00:00Z"}
    text = str(out)
    assert "SECRET" not in text and "10.0.0.5" not in text and "secret?token" not in text and "dev1" not in text
    assert "Path" in out[0]["now_playing_all_fields"]                      # field names are listed, values are not


def test_live_session_details_survive_a_failed_channel_lookup():
    class Broken(FakeLiveClient):
        def channel(self, channel_id):
            raise RuntimeError("no channel")
    s = {"NowPlayingItem": {"Id": "ch1", "Type": "TvChannel"}}
    assert dvr.live_session_details(Broken([s], {}))[0]["on_air_now_error"] == "no channel"


# ---------------------------------------------------------------- the Record button
from datetime import datetime as _dt

NOW = _dt(2026, 11, 1, 17, 5, 0)


class FakeRecorder:
    def __init__(self, program, timers=None, rules=None):
        self.program, self._timers, self._rules, self.posted = program, timers or [], rules or [], []

    def channel(self, channel_id):
        return {"Id": channel_id, "Name": "TNT", "CurrentProgram": self.program}

    def timers(self):
        return list(self._timers)

    def series_timers(self):
        return list(self._rules)

    def timer_defaults(self, program_id):
        return {"ProgramId": program_id, "PrePaddingSeconds": 60}

    def create_timer(self, body):
        self.posted.append(("timer", body))
        self._timers.append({"Id": "t9", "ProgramId": body["ProgramId"], "Status": "New"})

    def create_series_timer(self, body):
        self.posted.append(("series", body))


FILM = {"Id": "p1", "Name": "Father of the Bride (1991)", "StartDate": "2026-11-01T17:00:00.0000000Z", "EndDate": "2026-11-01T19:15:00.0000000Z", "IsMovie": True}
SHOW = {"Id": "p2", "Name": "Cold Case", "IsSeries": True, "StartDate": "2026-11-01T17:00:00.0000000Z", "EndDate": "2026-11-01T18:00:00.0000000Z"}


def test_record_this_airing_schedules_it_and_says_when_only_the_rest_will_be_recorded():
    client = FakeRecorder(FILM)
    out = dvr.record(client, "ch1", now=NOW)
    assert out["result"] == "scheduled" and client.posted == [("timer", {"ProgramId": "p1", "PrePaddingSeconds": 60})]
    assert "only the rest" in out["message"] and out["late_minutes"] == 5


def test_record_does_not_schedule_the_same_program_twice():
    client = FakeRecorder(FILM, timers=[{"Id": "t1", "ProgramId": "p1", "Status": "New"}])
    out = dvr.record(client, "ch1", now=NOW)
    assert out["result"] == "already" and client.posted == []
    assert dvr.record(FakeRecorder({**FILM, "TimerId": "t1"}), "ch1", now=NOW)["result"] == "already"


def test_a_clash_is_reported_and_a_missing_timer_is_not_called_scheduled():
    class Clash(FakeRecorder):
        def create_timer(self, body):
            self._timers.append({"Id": "t9", "ProgramId": body["ProgramId"], "Status": "ConflictedNotOk"})
    assert dvr.record(Clash(FILM), "ch1", now=NOW)["result"] == "conflict"

    class Lost(FakeRecorder):
        def create_timer(self, body):
            pass
    assert dvr.record(Lost(FILM), "ch1", now=NOW)["result"] == "unconfirmed"


def test_record_series_makes_a_new_episodes_only_rule_and_refuses_a_film():
    client = FakeRecorder(SHOW)
    out = dvr.record(client, "ch1", series=True, now=NOW)
    assert out["result"] == "scheduled" and client.posted[0][0] == "series" and client.posted[0][1]["RecordNewOnly"] is True
    with pytest.raises(dvr.DvrError, match="no series to record"):
        dvr.record(FakeRecorder(FILM), "ch1", series=True, now=NOW)
    assert dvr.record(FakeRecorder(SHOW, rules=[{"Id": "r1", "Name": "cold case"}]), "ch1", series=True, now=NOW)["result"] == "already"


def test_record_says_so_when_the_guide_has_nothing_on_air():
    with pytest.raises(dvr.DvrError, match="nothing listed"):
        dvr.record(FakeRecorder({}), "ch1", now=NOW)


def test_the_record_route_answers_with_the_result_and_the_reason_when_it_fails(client, den, monkeypatch):
    from synchub.clients.jellyfin import JellyfinClient
    from synchub import settings as st
    st.set_value("jellyfin_url", "http://jf:8096")
    st.set_value("jellyfin_api_key", "k")
    fake = FakeRecorder(FILM)
    monkeypatch.setattr(dvr, "datetime", type("D", (), {"utcnow": staticmethod(lambda: NOW), "strptime": staticmethod(_dt.strptime)}))
    monkeypatch.setattr(JellyfinClient, "channel", lambda self, cid: fake.channel(cid))
    for name in ("timers", "series_timers", "timer_defaults", "create_timer", "create_series_timer"):
        monkeypatch.setattr(JellyfinClient, name, lambda self, *a, _n=name, **k: getattr(fake, _n)(*a, **k))
    body = client.post(f"/api/tv/devices/{den}/record", json={"channelId": "ch1"}).get_json()
    assert body["ok"] and body["result"] == "scheduled" and "Father of the Bride" in body["body"]
    body = client.post(f"/api/tv/devices/{den}/record", json={"channelId": "ch1", "series": True}).get_json()
    assert not body["ok"] and "no series to record" in body["body"]
    assert not client.post(f"/api/tv/devices/{den}/record", json={}).get_json()["ok"]


def test_cancelling_removes_the_timer_from_jellyfin_and_from_the_schedule_now(client, conn, monkeypatch):
    from synchub.clients.jellyfin import JellyfinClient
    from synchub import settings as st
    st.set_value("jellyfin_url", "http://jf:8096")
    st.set_value("jellyfin_api_key", "k")
    dvr.sync_timers(conn, [timer("t1", "New")], [], now="2026-11-01 00:00:00")
    conn.commit()
    cancelled = []
    monkeypatch.setattr(JellyfinClient, "cancel_timer", lambda self, tid: cancelled.append(tid))
    assert client.post("/api/tv/dvr/timers/t1/cancel", json={"kind": "timer"}).get_json()["ok"]
    assert cancelled == ["t1"]
    assert conn.execute("SELECT removed_at FROM dvr_timer WHERE timer_id = 't1'").fetchone()[0] is not None


# ---------------------------------------------------------------- recordings that went wrong
def _timer_row(conn, tid, name, start, end, removed, status="InProgress"):
    conn.execute("INSERT INTO dvr_timer (timer_id, kind, name, status, start_at, end_at, first_seen, last_seen, removed_at) VALUES (?, 'timer', ?, ?, ?, ?, ?, ?, ?)",
                 (tid, name, status, start, end, start, removed or end, removed))


def _recording_row(conn, item_id, name, created, runtime, seen):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, series_name, date_created, runtime_seconds, is_dvr, first_seen, last_seen) VALUES (?, 'episode', 'E', ?, ?, ?, 1, ?, ?)",
                 (item_id, name, created, runtime, seen, seen))


def test_a_finished_recording_that_never_appeared_is_reported_once(conn):
    _timer_row(conn, "t1", "Cold Case", "2026-11-01 07:00:00", "2026-11-01 08:00:00", "2026-11-01 08:01:00")
    _recording_row(conn, "other", "Other Show", "2026-11-01 05:00:00", 3600, "2026-11-01 09:00:00")        # a Jellyfin sync ran after the end
    found = dvr.recording_problems(conn, now="2026-11-01 09:30:00")
    assert [(p["kind"], p["name"]) for p in found] == [("missing", "Cold Case")]
    assert dvr.recording_problems(conn, now="2026-11-01 09:31:00") == []


def test_a_short_recording_is_reported_and_a_full_one_is_not(conn):
    _timer_row(conn, "t1", "Cold Case", "2026-11-01 07:00:00", "2026-11-01 08:00:00", "2026-11-01 08:01:00")
    _timer_row(conn, "t2", "Dateline", "2026-11-01 07:00:00", "2026-11-01 08:00:00", "2026-11-01 08:01:00")
    _recording_row(conn, "a", "Cold Case", "2026-11-01 06:59:00", 1500, "2026-11-01 09:00:00")           # 25 of 60 minutes
    _recording_row(conn, "b", "Dateline", "2026-11-01 06:59:00", 3700, "2026-11-01 09:00:00")
    found = dvr.recording_problems(conn, now="2026-11-01 09:30:00")
    assert [(p["kind"], p["name"]) for p in found] == [("short", "Cold Case")]


def test_nothing_is_reported_before_a_library_sync_has_seen_the_end_or_for_a_cancelled_timer(conn):
    _timer_row(conn, "t1", "Cold Case", "2026-11-01 07:00:00", "2026-11-01 08:00:00", "2026-11-01 08:01:00")
    _recording_row(conn, "other", "Other Show", "2026-11-01 05:00:00", 3600, "2026-11-01 07:30:00")       # the last sync was before it ended
    assert dvr.recording_problems(conn, now="2026-11-01 08:30:00") == []
    _timer_row(conn, "t2", "News", "2026-11-01 07:00:00", "2026-11-01 08:00:00", "2026-11-01 06:00:00")   # removed before it started: cancelled
    assert dvr.recording_problems(conn, now="2026-11-01 09:30:00") == []
    assert conn.execute("SELECT alerted FROM dvr_timer WHERE timer_id = 't2'").fetchone()[0] == "cancelled"


def test_a_clash_jellyfin_reports_is_raised_once(conn):
    _timer_row(conn, "t1", "Cold Case", "2026-11-02 07:00:00", "2026-11-02 08:00:00", None, status="ConflictedNotOk")
    assert [p["kind"] for p in dvr.recording_problems(conn, now="2026-11-01 09:30:00")] == ["conflict"]
    assert dvr.recording_problems(conn, now="2026-11-01 09:35:00") == []
