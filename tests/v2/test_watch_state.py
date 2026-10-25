"""What each user has watched and where they stopped: copying Jellyfin's per-user data, reading it back as status, and keeping it current."""
import pytest

from synchub import tasks, watch
from synchub.ingest import jellyfin_userdata as ingest

HOUR = 36_000_000_000                    # ticks in an hour
ALICE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
BOB = "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"
T0 = "2026-11-02 10:00:00"


def dashed(hex32):
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:]}"


def item(item_id, position=0, played=False, count=0, last=None, runtime=2 * HOUR, kind="Movie", **extra):
    data = {"PlaybackPositionTicks": position, "Played": played, "PlayCount": count}
    if last:
        data["LastPlayedDate"] = last
    return {"Id": item_id, "Type": kind, "RunTimeTicks": runtime, "UserData": data, **extra}


class FakeJellyfin:
    """Jellyfin's answers about one or more users' watch data."""
    def __init__(self, played=(), resumable=(), single=None, fail=None):
        self.played, self.resumable, self.single, self.fail, self.asked = list(played), list(resumable), single or {}, fail, []

    def iter_user_data(self, user_id, filters):
        self.asked.append(("list", user_id, filters))
        if self.fail:
            raise RuntimeError(self.fail)
        yield from (self.played if filters == "IsPlayed" else self.resumable)

    def user_item(self, user_id, item_id):
        self.asked.append(("item", user_id, item_id))
        return self.single.get(item_id)


def rows(conn, user=ALICE):
    return {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM src_jellyfin_userdata WHERE jellyfin_user_id = ? ORDER BY item_id", (user,))}


# ================================================================ copying Jellyfin's data
def test_what_a_user_played_and_started_is_copied_and_untouched_items_are_not(v2, conn):
    jf = FakeJellyfin(played=[item("m1", played=True, count=2, last="2026-10-20T19:03:11.5120000Z"), item("m3")],
                      resumable=[item("m2", position=HOUR // 2, last="2026-10-21T20:00:00.0000000Z")])
    with v2.connection() as c:
        assert ingest.sync_user(c, jf, ALICE, now=T0) == {"seen": 2, "removed": 0}      # m3 was neither played nor started
    got = rows(conn)
    assert set(got) == {"m1", "m2"}
    assert (got["m1"]["played"], got["m1"]["play_count"], got["m1"]["position_ticks"], got["m1"]["last_played_at"]) == (1, 2, 0, "2026-10-20 19:03:11")
    assert (got["m2"]["played"], got["m2"]["position_ticks"], got["m2"]["runtime_ticks"]) == (0, HOUR // 2, 2 * HOUR)
    assert {("list", ALICE, "IsPlayed"), ("list", ALICE, "IsResumable")} <= set(jf.asked)


def test_ids_are_kept_lower_case_without_dashes_whatever_jellyfin_or_the_profile_says(v2, conn):
    jf = FakeJellyfin(played=[item(dashed("ABCDEF0123456789ABCDEF0123456789"), played=True, count=1)])
    with v2.connection() as c:
        ingest.sync_user(c, jf, dashed(ALICE).upper(), now=T0)
    assert list(rows(conn)) == ["abcdef0123456789abcdef0123456789"]
    assert jf.asked[0][1] == dashed(ALICE).upper()                                  # Jellyfin is asked with the id as the profile has it


def test_reading_again_updates_and_forgets_what_jellyfin_no_longer_reports_for_that_user_only(v2, conn):
    with v2.connection() as c:
        ingest.sync_user(c, FakeJellyfin(played=[item("m1", played=True, count=1), item("m2", played=True, count=1)]), ALICE, now=T0)
        ingest.sync_user(c, FakeJellyfin(played=[item("m1", played=True, count=1)]), BOB, now=T0)
    with v2.connection() as c:
        got = ingest.sync_user(c, FakeJellyfin(played=[item("m1", played=True, count=3)]), ALICE, now="2026-11-02 11:00:00")      # m2 was marked unwatched
    assert got == {"seen": 1, "removed": 1}
    mine = rows(conn)
    assert mine["m1"]["play_count"] == 3 and mine["m1"]["removed_at"] is None and mine["m2"]["removed_at"] == "2026-11-02 11:00:00"
    assert rows(conn, BOB)["m1"]["removed_at"] is None                              # someone else's record is not touched


def test_a_user_who_has_watched_nothing_clears_their_record(v2, conn):
    with v2.connection() as c:
        ingest.sync_user(c, FakeJellyfin(played=[item("m1", played=True, count=1)]), ALICE, now=T0)
        assert ingest.sync_user(c, FakeJellyfin(), ALICE, now="2026-11-02 11:00:00") == {"seen": 0, "removed": 1}


def test_a_failed_read_changes_nothing(v2, conn):
    with v2.connection() as c:
        ingest.sync_user(c, FakeJellyfin(played=[item("m1", played=True, count=1)]), ALICE, now=T0)
    with pytest.raises(RuntimeError, match="down"):
        with v2.connection() as c:
            ingest.sync_user(c, FakeJellyfin(fail="down"), ALICE, now="2026-11-02 11:00:00")
    assert rows(conn)["m1"]["removed_at"] is None


def test_one_item_is_read_again_after_someone_stops_playing_it(v2, conn):
    jf = FakeJellyfin(single={"m1": item("m1", position=HOUR, last="2026-11-02T09:59:00.0000000Z")})
    with v2.connection() as c:
        assert ingest.refresh_item(c, jf, dashed(ALICE), dashed("m1".ljust(32, "0")).upper(), now=T0) is False      # not an id Jellyfin knows
        assert ingest.refresh_item(c, jf, ALICE, "m1", now=T0) is True
    assert rows(conn)["m1"]["position_ticks"] == HOUR
    jf.single["m1"] = item("m1", played=True, count=1, last="2026-11-02T10:30:00.0000000Z")                       # they finished it
    with v2.connection() as c:
        ingest.refresh_item(c, jf, ALICE, "m1", now="2026-11-02 10:31:00")
    got = rows(conn)["m1"]
    assert (got["position_ticks"], got["played"], got["play_count"], got["last_played_at"]) == (0, 1, 1, "2026-11-02 10:30:00")
    jf.single["m1"] = item("m1")                                                                                   # marked unwatched
    with v2.connection() as c:
        assert ingest.refresh_item(c, jf, ALICE, "m1", now="2026-11-02 10:32:00") is False
    assert rows(conn)["m1"]["removed_at"] == "2026-11-02 10:32:00"


# ================================================================ reading it back
def test_ticks_and_dates_read_as_a_person_would_say_them():
    assert [watch.fmt_ticks(t) for t in (0, None, 59 * 10_000_000, 42 * 600_000_000 + 10 * 10_000_000, HOUR + 12 * 600_000_000 + 30 * 10_000_000)] == ["0:00", "0:00", "0:59", "42:10", "1:12:30"]
    assert watch.date_label("2026-10-05 19:03:11") == "Oct 5, 2026" and watch.date_label(None) is None and watch.date_label("nonsense") is None
    assert watch.resume_ticks({"PlaybackPositionTicks": 123}) == 123 and watch.resume_ticks({}) == 0 and watch.resume_ticks(None) == 0 and watch.resume_ticks({"PlaybackPositionTicks": "x"}) == 0


def test_an_item_is_not_watched_in_progress_or_watched():
    assert watch.describe(None)["state"] == "unwatched" and watch.describe(None)["state_label"] == "Not watched" and watch.describe(None)["resume_label"] is None
    mid = watch.describe({"position_ticks": 42 * 600_000_000, "runtime_ticks": 2 * HOUR, "played": 0, "play_count": 0, "last_played_at": "2026-10-20 19:03:11"})
    assert (mid["state"], mid["state_label"], mid["percent"], mid["position_label"], mid["runtime_label"], mid["resume_label"], mid["last_played_label"]) == \
           ("in_progress", "In progress", 35, "42:00", "2:00:00", "42:00", "Oct 20, 2026")
    done = watch.describe({"position_ticks": 0, "runtime_ticks": 2 * HOUR, "played": 1, "play_count": 2, "last_played_at": "2026-10-20 19:03:11"})
    assert (done["state"], done["state_label"], done["percent"], done["play_count"], done["resume_label"]) == ("watched", "Watched", 100, 2, None)
    again = watch.describe({"position_ticks": HOUR, "runtime_ticks": 2 * HOUR, "played": 1, "play_count": 1, "last_played_at": None})
    assert (again["state"], again["state_label"], again["watched_before"], again["percent"]) == ("in_progress", "Watching again", True, 50)
    assert watch.describe({"position_ticks": 1, "runtime_ticks": 2 * HOUR, "played": 0, "play_count": 0})["percent"] == 1      # never 0% once started
    assert watch.describe({"position_ticks": 2 * HOUR - 1, "runtime_ticks": 2 * HOUR, "played": 0, "play_count": 0})["percent"] == 99


@pytest.fixture
def heat(v2, conn):
    media = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Heat', '949')").lastrowid
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES ('m1', 'movie', 'a', 'a')")
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', 'm1', 'x', 1.0)", (media,))
    conn.commit()
    return media


def give(conn, user, item_id, position=0, played=0, count=0, last=None, runtime=2 * HOUR, removed=None):
    conn.execute("INSERT OR REPLACE INTO src_jellyfin_userdata (jellyfin_user_id, item_id, position_ticks, runtime_ticks, played, play_count, last_played_at, first_seen, last_seen, removed_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, 'a', 'a', ?)", (user, item_id, position, runtime, played, count, last, removed))
    conn.commit()


def test_a_movie_is_unwatched_until_the_user_has_a_record_of_it(heat, conn):
    assert watch.movie_status(conn, ALICE, heat)["state"] == "unwatched" and watch.movie_status(conn, ALICE, heat)["item_id"] == "m1"
    give(conn, ALICE, "m1", position=HOUR, last="2026-10-20 19:03:11")
    got = watch.movie_status(conn, ALICE, heat)
    assert (got["state"], got["position_label"], got["last_played_label"]) == ("in_progress", "1:00:00", "Oct 20, 2026")
    assert watch.movie_status(conn, BOB, heat)["state"] == "unwatched"                          # somebody else's progress is not yours
    give(conn, ALICE, "m1", position=HOUR, removed="2026-11-01 00:00:00")
    assert watch.movie_status(conn, ALICE, heat)["state"] == "unwatched"                        # Jellyfin stopped reporting it
    assert watch.movie_status(conn, None, heat)["state"] == "unwatched"


def test_a_movie_that_is_not_in_the_jellyfin_library_has_no_status(v2, conn):
    media = conn.execute("INSERT INTO media (kind, title) VALUES ('movie', 'Elsewhere')").lastrowid
    conn.commit()
    assert watch.movie_status(conn, ALICE, media) is None


@pytest.fixture
def severance(v2, conn):
    """A show with a special, five episodes in the library (two seasons) and a sixth that is only wanted."""
    media = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'Severance', '95396')").lastrowid
    episodes = {}
    for season, number in ((0, 1), (1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)):
        eid = conn.execute("INSERT INTO episode (media_id, season_number, episode_number, title) VALUES (?, ?, ?, ?)", (media, season, number, f"Ep {season}.{number}")).lastrowid
        episodes[(season, number)] = eid
        if (season, number) != (2, 3):                                    # S2E3 is not in the library
            jf = f"e{season}{number}"
            conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES (?, 'episode', 'a', 'a')", (jf,))
            conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('episode', ?, 'jellyfin_episode', ?, 'x', 1.0)", (eid, jf))
    conn.commit()
    return media


def test_a_show_nobody_has_started(severance, conn):
    got = watch.show_status(conn, ALICE, severance)
    assert (got["total"], got["watched"], got["state"], got["last"]) == (5, 0, "not_started", None)          # the special is not counted; S2E3 is not in the library
    assert got["continue"]["kind"] == "next" and got["continue"]["code"] == "S1E1"


def test_a_show_part_way_through_continues_from_the_next_unwatched_episode(severance, conn):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    give(conn, ALICE, "e12", played=1, count=1, last="2026-10-02 20:00:00")
    got = watch.show_status(conn, ALICE, severance)
    assert (got["watched"], got["total"], got["percent"], got["state"]) == (2, 5, 40, "in_progress")
    assert got["last"] == {"code": "S1E2", "title": "Ep 1.2", "date_label": "Oct 2, 2026", "last_played_at": "2026-10-02 20:00:00"}
    assert got["continue"]["kind"] == "next" and got["continue"]["code"] == "S1E3" and got["continue"]["item_id"] == "e13" and got["continue"]["resume_label"] is None


def test_an_episode_they_are_in_the_middle_of_is_the_one_to_continue_with(severance, conn):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    give(conn, ALICE, "e12", position=HOUR // 2, last="2026-10-02 20:00:00", runtime=HOUR)
    got = watch.show_status(conn, ALICE, severance)
    assert got["continue"]["kind"] == "resume" and got["continue"]["code"] == "S1E2" and got["continue"]["resume_label"] == "30:00" and got["continue"]["percent"] == 50
    assert got["watched"] == 1


def test_a_rewatch_does_not_beat_an_episode_they_have_not_finished_for_the_first_time(severance, conn):
    give(conn, ALICE, "e11", position=HOUR // 4, played=1, count=1, last="2026-10-03 20:00:00")           # watching it again, later than the other
    give(conn, ALICE, "e12", position=HOUR // 2, last="2026-10-02 20:00:00")
    assert watch.show_status(conn, ALICE, severance)["continue"]["code"] == "S1E2"


def test_the_next_episode_follows_the_one_watched_last_even_when_they_went_back(severance, conn):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-05 20:00:00")                               # the most recent
    give(conn, ALICE, "e12", played=1, count=1, last="2026-10-02 20:00:00")
    give(conn, ALICE, "e13", played=1, count=1, last="2026-10-03 20:00:00")
    assert watch.show_status(conn, ALICE, severance)["continue"]["code"] == "S2E1"                      # after S1E1, the first one they have not seen
    give(conn, ALICE, "e21", played=1, count=1, last="2026-10-06 20:00:00")
    give(conn, ALICE, "e22", played=1, count=1, last="2026-10-07 20:00:00")
    got = watch.show_status(conn, ALICE, severance)
    assert (got["state"], got["watched"], got["total"], got["continue"]) == ("caught_up", 5, 5, None)


def test_the_status_is_for_the_person_asking_and_an_unknown_user_sees_nothing_started(severance, conn):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    assert watch.show_status(conn, BOB, severance)["watched"] == 0 and watch.show_status(conn, None, severance)["watched"] == 0


def test_an_episode_with_two_files_counts_once(severance, conn):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES ('e11b', 'episode', 'a', 'a')")
    eid = conn.execute("SELECT entity_id FROM link WHERE source_key = 'e11'").fetchone()[0]
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('episode', ?, 'jellyfin_episode', 'e11b', 'x', 1.0)", (eid,))
    give(conn, ALICE, "e11b", played=1, count=1, last="2026-10-01 20:00:00")
    got = watch.show_status(conn, ALICE, severance)
    assert got["total"] == 5 and got["watched"] == 1


def test_a_season_of_episodes_is_described_by_jellyfin_item(severance, conn):
    give(conn, ALICE, "e11", played=1, count=1, last="2026-10-01 20:00:00")
    give(conn, ALICE, "e12", position=HOUR // 2, last="2026-10-02 20:00:00")
    got = watch.item_states(conn, ALICE, ["e11", "e12", "e13", None])
    assert {k: v["state"] for k, v in got.items()} == {"e11": "watched", "e12": "in_progress", "e13": "unwatched"}
    assert watch.item_states(conn, None, ["e11"]) == {} and watch.item_states(conn, ALICE, []) == {}


# ================================================================ keeping it current
@pytest.fixture
def people(v2, conn):
    conn.execute("INSERT INTO profile (name, jellyfin_user_id) VALUES ('Alice', ?)", (ALICE,))
    conn.execute("INSERT INTO profile (name, jellyfin_user_id) VALUES ('Bob', ?)", (BOB,))
    conn.execute("INSERT INTO profile (name, jellyfin_user_id, is_active) VALUES ('Old', 'c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0', 0)")
    conn.execute("INSERT INTO profile (name) VALUES ('NoJellyfin')")
    conn.commit()


def test_the_task_reads_every_active_profile_that_has_a_jellyfin_user(people, conn, monkeypatch):
    jf = FakeJellyfin(played=[item("m1", played=True, count=1)])
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: jf)
    assert tasks.sync_watch_state() == "Watch history: 2 item(s) for 2 profile(s)."
    assert {u for kind, u, *_ in jf.asked if kind == "list"} == {ALICE, BOB}                 # not the inactive one, not the one without Jellyfin
    assert set(rows(conn)) == {"m1"} and set(rows(conn, BOB)) == {"m1"}
    assert conn.execute("SELECT status FROM task_status WHERE task_name = 'watch_state'").fetchone()[0] == "completed"


def test_one_profile_failing_does_not_stop_the_others_but_everyone_failing_is_a_failure(people, conn, monkeypatch):
    class Selective(FakeJellyfin):
        def iter_user_data(self, user_id, filters):
            if user_id == BOB:
                raise RuntimeError("Bob's data is unreadable")
            yield from super().iter_user_data(user_id, filters)
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: Selective(played=[item("m1", played=True, count=1)]))
    message = tasks.sync_watch_state()
    assert "for 1 profile(s)" in message and "Failed: Bob: Bob's data is unreadable" in message and set(rows(conn)) == {"m1"}
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: FakeJellyfin(fail="Jellyfin is down"))
    assert tasks.sync_watch_state() is None                                                  # run() records the failure and returns nothing
    status = conn.execute("SELECT status, message FROM task_status WHERE task_name = 'watch_state'").fetchone()
    assert status["status"] == "failed" and "Could not read anyone's watch history" in status["message"]


def test_the_task_has_nothing_to_do_without_profiles_or_with_jellyfin_switched_off(v2, conn, monkeypatch):
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: FakeJellyfin())
    assert tasks.sync_watch_state() == "No active profile has a Jellyfin user."
    from synchub import settings
    settings.set_value("disable_jellyfin_connection", "1")
    assert tasks.sync_watch_state() == "Jellyfin integration is disabled in Settings."


def test_the_task_is_listed_scheduled_and_runs_in_the_full_sequence(client):
    from synchub import scheduler
    assert "watch_state" in tasks.TASK_NAMES
    assert "sync_watch_state" in dict((job_id, fn.__name__) for job_id, _, _, fn in scheduler.JOBS)["job_jellyfin"]
    page = client.get("/tools").get_data(as_text=True)
    assert "Read watch history now" in page and 'data-task-btn="watch_state"' in page
    assert client.post("/tools/trigger/watch").status_code == 302


@pytest.fixture
def refreshes(monkeypatch):
    """refresh_soon, run on the spot and written down."""
    got = []
    monkeypatch.setattr(tasks, "refresh_soon", lambda key, job, **kw: (got.append(key), job())[0] is None or True)
    monkeypatch.setattr(tasks, "WATCH_SETTLE_SECONDS", 0)
    monkeypatch.setattr(tasks, "_WATCH_LAST", {})
    return got


def stop_event(user=ALICE, item_id="m1", kind="Movie", event="PlaybackStop"):
    return event, {"NotificationType": event, "UserId": dashed(user), "ItemId": dashed(item_id.ljust(32, "0")), "ItemType": kind}


def test_when_someone_stops_playing_something_their_record_of_it_is_read_again(people, conn, refreshes, monkeypatch):
    jf = FakeJellyfin(single={"m1" + "0" * 30: item("m1" + "0" * 30, position=HOUR, last="2026-11-02T09:00:00.0000000Z")})
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: jf)
    assert tasks.watch_event(*stop_event()) is True
    assert refreshes == [f"watch:{ALICE}:m1{'0' * 30}"]                                  # the dashes are gone: the same id the sync uses
    assert rows(conn)["m1" + "0" * 30]["position_ticks"] == HOUR
    assert jf.asked == [("item", ALICE, "m1" + "0" * 30)]


def test_events_that_say_nothing_about_a_profiles_viewing_are_ignored(people, refreshes, monkeypatch):
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: pytest.fail("Jellyfin must not be asked"))
    assert tasks.watch_event(*stop_event(event="PlaybackStart")) is False and tasks.watch_event(*stop_event(event="PlaybackProgress")) is False
    assert tasks.watch_event(*stop_event(user="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0")) is False       # an inactive profile
    assert tasks.watch_event(*stop_event(user="d" * 32)) is False                                  # a Jellyfin user nobody has a profile for
    assert tasks.watch_event(*stop_event(kind="Audio")) is False and tasks.watch_event(*stop_event(kind="Series")) is False
    assert tasks.watch_event("PlaybackStop", {"NotificationType": "PlaybackStop"}) is False
    assert refreshes == []


def test_a_user_data_event_is_acted_on_at_most_once_in_a_while_but_a_stop_always_is(people, conn, refreshes, monkeypatch):
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: FakeJellyfin())
    saved = stop_event(event="UserDataSaved")
    assert tasks.watch_event(*saved, now=100.0) is True
    assert tasks.watch_event(*saved, now=105.0) is False and tasks.watch_event(*saved, now=119.0) is False
    assert tasks.watch_event(*saved, now=121.0) is True
    assert tasks.watch_event(*stop_event(), now=122.0) is True and tasks.watch_event(*stop_event(), now=123.0) is True      # stopping is never held back


def test_a_jellyfin_that_cannot_be_asked_does_not_break_the_event(people, refreshes, monkeypatch):
    def broken():
        raise RuntimeError("Jellyfin is down")
    monkeypatch.setattr(tasks, "JellyfinClient", broken)
    assert tasks.watch_event(*stop_event()) is True                                       # logged, not raised


def test_the_webhook_hands_playback_stops_to_the_watch_record_and_survives_it_failing(v2, client, monkeypatch):
    seen = []
    monkeypatch.setattr(tasks, "watch_event", lambda event, payload: seen.append((event, payload.get("ItemId"))))
    r = client.post("/api/webhook/jellyfin", json={"NotificationType": "PlaybackStop", "ItemId": "m1", "UserId": ALICE})
    assert r.status_code == 200 and seen == [("PlaybackStop", "m1")]
    monkeypatch.setattr(tasks, "watch_event", lambda event, payload: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.post("/api/webhook/jellyfin", json={"NotificationType": "PlaybackStop", "ItemId": "m1", "UserId": ALICE}).status_code == 200
    monkeypatch.setattr(tasks, "watch_event", lambda event, payload: seen.append("radarr"))
    client.post("/api/webhook/radarr", json={"eventType": "Test"})
    assert "radarr" not in seen                                                            # only Jellyfin's events are about viewing
