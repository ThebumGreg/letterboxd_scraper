"""Playing a title on a TV picks up where the viewer left off: the route's decision, the jump a deep link needs, and what a stream can and cannot do."""
import pytest

from synchub.tv import orchestrator
from synchub.tv.jellyfin import JellyfinError

from .test_tv_nextup import EPISODE, SHOW, RecordingTV, browser, play, tv      # noqa: F401  (fixtures and helpers shared with the next-up tests)

HOUR = 36_000_000_000
ALICE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
MOVIE = {"Id": "m1", "Type": "Movie", "Name": "Heat"}


class FakeJellyfin:
    """What the play route asks of Jellyfin, with each user's data."""
    def __init__(self, items, data=None, next_up=None, first=None, down=False):
        self.items, self.data, self._next, self._first, self.down, self.asked = items, data or {}, next_up, first, down, []

    def item(self, item_id):
        return self.items.get(item_id, {})

    def user_data(self, item_id, user_id):
        self.asked.append(("user_data", item_id, user_id))
        if self.down:
            raise JellyfinError("Could not reach Jellyfin")
        return self.data.get(item_id, {})

    def next_up(self, series_id, user_id=None):
        self.asked.append(("next_up", series_id, user_id))
        return self._next

    def first_episode(self, series_id, user_id=None):
        return self._first

    def search(self, q, user_id=None):
        self.asked.append(("search", q, user_id))
        return []

    def episodes(self, series_id, user_id=None):
        self.asked.append(("episodes", series_id, user_id))
        return []


@pytest.fixture
def alice(browser, conn):
    conn.execute("INSERT INTO profile (id, name, jellyfin_user_id) VALUES (1, 'Alice', ?)", (ALICE,))
    conn.commit()
    browser.set_cookie("synchub_profile", "1")
    return browser


def use(tv, monkeypatch, **kw):
    jf = FakeJellyfin(**kw)
    monkeypatch.setattr(tv, "jf", lambda: jf)
    return jf


# ================================================================ the route
def test_a_movie_is_played_from_where_the_viewer_stopped(alice, tv, monkeypatch):
    jf = use(tv, monkeypatch, items={"m1": MOVIE}, data={"m1": {"PlaybackPositionTicks": HOUR + 12 * 600_000_000}})
    answer = play(alice, "m1")
    assert answer["ok"] and RecordingTV.played == [("m1", HOUR + 12 * 600_000_000)]
    assert answer["steps"][0] == "Picking up where Alice left off, at 1:12:00" and answer["body"].startswith("Picking up where Alice left off")
    assert jf.asked == [("user_data", "m1", ALICE)]


def test_nothing_is_looked_up_when_the_request_says_where_to_start(alice, tv, monkeypatch):
    jf = use(tv, monkeypatch, items={"m1": MOVIE}, data={"m1": {"PlaybackPositionTicks": HOUR}})
    assert play(alice, "m1", startPositionTicks=0)["ok"]                                       # start over
    assert play(alice, "m1", startPositionTicks=5)["ok"]                                       # exactly there
    assert RecordingTV.played == [("m1", 0), ("m1", 5)] and jf.asked == []
    assert "Picking up" not in str(play(alice, "m1", startPositionTicks=0)["steps"])


def test_a_viewer_who_has_not_started_it_starts_it_from_the_beginning(alice, tv, monkeypatch):
    use(tv, monkeypatch, items={"m1": MOVIE}, data={"m1": {"PlaybackPositionTicks": 0, "Played": True}})
    answer = play(alice, "m1")
    assert RecordingTV.played == [("m1", None)] and "Picking up" not in str(answer["steps"])
    use(tv, monkeypatch, items={"m1": MOVIE})                                                  # Jellyfin has no data for it at all
    play(alice, "m1")
    assert RecordingTV.played[-1] == ("m1", None)


def test_a_guest_and_a_profile_without_a_jellyfin_user_have_nothing_to_pick_up(browser, tv, monkeypatch, conn):
    jf = use(tv, monkeypatch, items={"m1": MOVIE}, data={"m1": {"PlaybackPositionTicks": HOUR}})
    assert play(browser, "m1")["ok"]                                                          # "just looking"
    conn.execute("INSERT INTO profile (id, name) VALUES (2, 'NoJellyfin')")
    conn.commit()
    browser.set_cookie("synchub_profile", "2")
    assert play(browser, "m1")["ok"]
    assert RecordingTV.played == [("m1", None), ("m1", None)] and jf.asked == []


def test_only_playing_now_picks_up_where_they_left_off_not_queueing(alice, tv, monkeypatch):
    jf = use(tv, monkeypatch, items={"m1": MOVIE}, data={"m1": {"PlaybackPositionTicks": HOUR}})
    answer = alice.post("/api/tv/devices/den/play", json={"itemIds": ["m1"], "playCommand": "PlayNext"}).get_json()      # a queue request; this TV only plays now
    assert jf.asked == [] and "Picking up" not in str(answer)


def test_a_show_plays_the_episode_they_are_part_way_through_from_where_they_stopped(alice, tv, monkeypatch):
    jf = use(tv, monkeypatch, items={"show1": SHOW}, next_up=EPISODE, data={"ep7": {"PlaybackPositionTicks": HOUR // 2}})
    answer = play(alice, "show1", startPositionTicks=None)
    assert RecordingTV.played == [("ep7", HOUR // 2)]
    assert answer["steps"][0].startswith('"Severance" is a show: playing next up for Alice') and answer["steps"][1] == "Picking up where Alice left off, at 30:00"
    assert jf.asked == [("next_up", "show1", ALICE), ("user_data", "ep7", ALICE)]


def test_the_position_given_for_a_show_is_not_the_episodes_but_zero_means_start_the_episode_over(alice, tv, monkeypatch):
    jf = use(tv, monkeypatch, items={"show1": SHOW}, next_up=EPISODE, data={"ep7": {"PlaybackPositionTicks": HOUR // 2}})
    play(alice, "show1", startPositionTicks=123)                                              # meaningless for a show: the episode's own position is used
    assert RecordingTV.played == [("ep7", HOUR // 2)]
    play(alice, "show1", startPositionTicks=0)
    assert RecordingTV.played[-1] == ("ep7", 0) and jf.asked.count(("user_data", "ep7", ALICE)) == 1


def test_when_jellyfin_cannot_be_asked_what_synchub_copied_last_is_used(alice, tv, monkeypatch, conn):
    use(tv, monkeypatch, items={"m1": MOVIE}, down=True)
    assert play(alice, "m1")["ok"] and RecordingTV.played == [("m1", None)]                  # nothing copied: it still plays
    conn.execute("INSERT INTO src_jellyfin_userdata (jellyfin_user_id, item_id, position_ticks, first_seen, last_seen) VALUES (?, 'm1', ?, 'a', 'a')", (ALICE, HOUR))
    conn.commit()
    answer = play(alice, "m1")
    assert RecordingTV.played[-1] == ("m1", HOUR) and "left off, at 1:00:00" in answer["steps"][0]


def test_a_lookup_that_fails_in_any_way_never_stops_the_title_from_playing(alice, tv, monkeypatch, caplog):
    jf = use(tv, monkeypatch, items={"m1": MOVIE})
    jf.user_data = lambda item_id, user_id: (_ for _ in ()).throw(ValueError("Jellyfin answered with something that is not JSON"))
    answer = play(alice, "m1")
    assert answer["ok"] and RecordingTV.played == [("m1", None)] and "ValueError" in caplog.text


def test_the_copy_is_found_whatever_the_profile_or_jellyfin_writes_the_ids_like(browser, tv, monkeypatch, conn):
    conn.execute("INSERT INTO profile (id, name, jellyfin_user_id) VALUES (1, 'Alice', ?)", (f"{ALICE[:8]}-{ALICE[8:12]}-{ALICE[12:16]}-{ALICE[16:20]}-{ALICE[20:]}".upper(),))
    conn.execute("INSERT INTO src_jellyfin_userdata (jellyfin_user_id, item_id, position_ticks, first_seen, last_seen, removed_at) VALUES (?, 'm1', ?, 'a', 'a', NULL)", (ALICE, HOUR))
    conn.commit()
    browser.set_cookie("synchub_profile", "1")
    use(tv, monkeypatch, items={"m1": MOVIE}, down=True)
    play(browser, "m1")
    assert RecordingTV.played == [("m1", HOUR)]


def test_the_search_and_episode_lists_are_the_viewers_own(alice, tv, monkeypatch):
    jf = use(tv, monkeypatch, items={"show1": SHOW})
    alice.get("/api/tv/jf/search?q=heat")
    alice.get("/api/tv/jf/episodes/show1")
    assert jf.asked == [("search", "heat", ALICE), ("episodes", "show1", ALICE)]


def test_the_lists_of_a_guest_are_not_for_any_one_users_data(browser, tv, monkeypatch):
    jf = use(tv, monkeypatch, items={"show1": SHOW})
    browser.get("/api/tv/jf/search?q=heat")
    assert jf.asked == [("search", "heat", None)]


# ================================================================ jumping to the saved position after a deep link
class Resp:
    def __init__(self, ok=True, status=204):
        self.ok, self.status_code = ok, status


class FakeSessions:
    """Jellyfin's session list and Seek command, with an app that does, or does not, move when told."""
    def __init__(self, obeys=True, seek_ok=True, position=0):
        self.obeys, self.seek_ok, self.position, self.seeks = obeys, seek_ok, position, []

    def sessions(self, quiet=True):
        return [{"Id": "s1", "PlayState": {"PositionTicks": self.position}}]

    def playstate(self, sid, command, params=None):
        self.seeks.append((sid, command, params))
        if self.seek_ok and self.obeys:
            self.position = params["seekPositionTicks"]
        return Resp(self.seek_ok, 204 if self.seek_ok else 400)


@pytest.fixture
def steps():
    got = []
    return type("Steps", (), {"got": got, "__call__": lambda self, message, ok=True: got.append((message, ok))})()


@pytest.fixture
def found_session(monkeypatch):
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf, driver, item_id: {"Id": "s1", "NowPlayingItem": {"Id": item_id, "Name": "Heat"}})


def test_a_deep_link_is_moved_to_where_the_viewer_stopped_and_it_checks_the_app_did_it(found_session, steps):
    jf = FakeSessions()
    assert orchestrator.jump_to(jf, None, "m1", HOUR, steps) is True
    assert jf.seeks == [("s1", "Seek", {"seekPositionTicks": HOUR})] and steps.got == [("Jumped to 1:00:00, where they left off", True)]


def test_a_position_a_little_short_of_the_saved_one_still_counts(found_session, steps):
    assert orchestrator.jump_to(FakeSessions(obeys=False, position=HOUR - 20 * 10_000_000), None, "m1", HOUR, steps) is True


def test_an_app_that_ignores_the_command_is_said_to_have_ignored_it(found_session, steps):
    assert orchestrator.jump_to(FakeSessions(obeys=False), None, "m1", HOUR + 30 * 10_000_000, steps, timeout=0) is False
    message, ok = steps.got[0]
    assert ok is False and "still reports an earlier position" in message and "1:00:30" in message


def test_an_app_that_refuses_the_command_is_said_to_have_refused_it(found_session, steps):
    assert orchestrator.jump_to(FakeSessions(seek_ok=False), None, "m1", HOUR, steps) is False
    assert steps.got == [("The Jellyfin app would not jump to 1:00:00 (HTTP 400); it plays from the beginning", False)]


def test_an_app_that_takes_no_remote_commands_is_not_sent_one_and_the_person_is_told_why(monkeypatch, steps):
    """The official Roku app: a Seek would be ignored, and waiting for it to arrive would only hold up the progress box."""
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf, driver, item_id: {"Id": "s1", "SupportsRemoteControl": False, "NowPlayingItem": {"Id": item_id}})
    jf = FakeSessions()
    assert orchestrator.jump_to(jf, None, "m1", HOUR, steps) is False and jf.seeks == []
    message, ok = steps.got[0]
    assert ok is False and "cannot be remote controlled" in message and "1:00:00" in message and "plays from the beginning" in message and "stream URL" in message


def test_an_app_that_says_it_can_be_remote_controlled_is_sent_the_jump(monkeypatch, steps):
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf, driver, item_id: {"Id": "s1", "SupportsRemoteControl": True, "NowPlayingItem": {"Id": item_id}})
    jf = FakeSessions()
    assert orchestrator.jump_to(jf, None, "m1", HOUR, steps) is True and len(jf.seeks) == 1


def test_no_session_means_no_jump(monkeypatch, steps):
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf, driver, item_id: None)
    jf = FakeSessions()
    assert orchestrator.jump_to(jf, None, "m1", HOUR, steps) is False and jf.seeks == [] and "plays from the beginning" in steps.got[0][0]


class Driver:
    id, name, deeplink_types = "den", "Den", frozenset({"movie"})

    def jellyfin_app(self):
        return "jf"

    def deeplink(self, app, media_type, item_id, series_id, running=False):
        return "sent"


class Book:
    def add(self, *a, **k):
        pass


@pytest.fixture
def deeplink(monkeypatch):
    """play_deeplink with the device and the waiting taken out: Jellyfin reports the item playing at once (or never)."""
    monkeypatch.setattr(orchestrator, "ensure_on", lambda driver, step: {"app": "jf"})
    monkeypatch.setattr(orchestrator, "_wait_for", lambda fn, timeout, interval=2: fn())
    jf = type("JF", (), {"item": lambda self, i: {"Id": i, "Name": "Heat", "Type": "Movie"}})()
    return jf


def test_play_deeplink_jumps_once_it_is_playing(deeplink, monkeypatch):
    jumps = []
    monkeypatch.setattr(orchestrator, "_playing_item", lambda jf, driver, item_id: {"Name": "Heat"})
    monkeypatch.setattr(orchestrator, "jump_to", lambda jf, driver, item_id, ticks, step, **kw: jumps.append((item_id, ticks)))
    out = orchestrator.play_deeplink(Driver(), deeplink, Book(), "m1", HOUR)
    assert out["ok"] and out["confirmed"] is True and jumps == [("m1", HOUR)]
    assert not any("always start from the beginning" in s for s in out["steps"])


def test_play_deeplink_does_not_jump_when_there_is_nothing_to_pick_up(deeplink, monkeypatch):
    jumps = []
    monkeypatch.setattr(orchestrator, "_playing_item", lambda jf, driver, item_id: {"Name": "Heat"})
    monkeypatch.setattr(orchestrator, "jump_to", lambda *a, **k: jumps.append(a))
    for ticks in (None, 0):
        assert orchestrator.play_deeplink(Driver(), deeplink, Book(), "m1", ticks)["ok"]
    assert jumps == []


def test_play_deeplink_that_never_sees_it_playing_says_it_could_not_move_it_either(deeplink, monkeypatch):
    monkeypatch.setattr(orchestrator, "_playing_item", lambda jf, driver, item_id: None)
    monkeypatch.setattr(orchestrator, "jump_to", lambda *a, **k: pytest.fail("there is no session to move"))
    out = orchestrator.play_deeplink(Driver(), deeplink, Book(), "m1", HOUR)
    assert out["ok"] and out["confirmed"] is False and any("could not be moved to 1:00:00 either" in s for s in out["steps"])


# ================================================================ streaming
class StreamDriver:
    id, name, cfg = "den", "Den", {}

    def __init__(self):
        self.played = []

    def play_url(self, url, title, fmt):
        self.played.append(url)

    def status(self, quiet=False):
        return {"media": {"state": "playing"}}


@pytest.fixture
def stream(monkeypatch):
    monkeypatch.setattr(orchestrator, "ensure_on", lambda driver, step: {"app": "x"})
    monkeypatch.setattr(orchestrator, "_wait_for", lambda fn, timeout, interval=2: fn())
    jf = type("JF", (), {"item": lambda self, i: {"Id": i, "Name": "Heat"}})()
    return jf


def test_a_direct_stream_says_it_cannot_start_part_way(stream, monkeypatch):
    monkeypatch.setattr(orchestrator.casting, "stream_url", lambda jf, item, base, mode, start: ("http://jf/Videos/m1/stream?static=true", "mkv", "direct play"))
    out = orchestrator.play_url(StreamDriver(), stream, Book(), "m1", HOUR)
    note = next(s for s in out["steps"] if "part-way" in s)
    assert "starts from the beginning" in note and "1:00:00" in note and "not recorded by Jellyfin" in note


def test_a_transcoded_stream_starts_where_it_was_told(stream, monkeypatch):
    monkeypatch.setattr(orchestrator.casting, "stream_url", lambda jf, item, base, mode, start: (f"http://jf/Videos/m1/master.m3u8?StartTimeTicks={start}", "hls", "HLS transcode"))
    out = orchestrator.play_url(StreamDriver(), stream, Book(), "m1", HOUR)
    assert "Starting at 1:00:00" in out["steps"] and not any("part-way" in s for s in out["steps"])


def test_a_stream_from_the_beginning_has_no_note(stream, monkeypatch):
    monkeypatch.setattr(orchestrator.casting, "stream_url", lambda jf, item, base, mode, start: ("http://jf/Videos/m1/stream?static=true", "mkv", "direct play"))
    out = orchestrator.play_url(StreamDriver(), stream, Book(), "m1", None)
    assert not any("part-way" in s or "Starting at" in s for s in out["steps"])


# ================================================================ how a deep-link-only TV resumes (Remote Apps, "When a title is resumed")
class SavingJellyfin:
    """Jellyfin as option e sees it: it can be told where the viewer stopped, and says where the app is."""
    def __init__(self, saved_ok=True):
        self.saved, self.saved_ok = [], saved_ok

    def item(self, item_id):
        return {"Id": item_id, "Name": "Heat", "Type": "Movie"}

    def set_position(self, user_id, item_id, ticks):
        self.saved.append((user_id, item_id, ticks))
        return self.saved_ok


def test_option_e_saves_the_place_first_and_reports_that_the_app_picked_it_up(deeplink, monkeypatch):
    jf = SavingJellyfin()
    monkeypatch.setattr(orchestrator, "_playing_item", lambda jf_, driver, item_id: {"Name": "Heat"})
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf_, driver, item_id: {"Id": "s1", "PlayState": {"PositionTicks": HOUR}})
    monkeypatch.setattr(orchestrator, "jump_to", lambda *a, **k: pytest.fail("option e does not send a Seek"))
    out = orchestrator.play_deeplink(Driver(), jf, Book(), "m1", HOUR, resume_mode="e", user_id=ALICE)
    assert jf.saved == [(ALICE, "m1", HOUR)] and out["ok"]
    assert out["steps"][0].startswith("Saved 1:00:00 as the place in Jellyfin") and "The app picked up at 1:00:00 by itself" in out["steps"]


def test_option_e_says_plainly_when_the_app_ignored_the_saved_place(deeplink, monkeypatch):
    jf = SavingJellyfin()
    monkeypatch.setattr(orchestrator, "_playing_item", lambda jf_, driver, item_id: {"Name": "Heat"})
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf_, driver, item_id: {"Id": "s1", "PlayState": {"PositionTicks": 10_000_000}})
    out = orchestrator.play_deeplink(Driver(), jf, Book(), "m1", HOUR, resume_mode="e", user_id=ALICE)
    assert out["ok"] and any("started from the beginning even though 1:00:00 was saved" in s for s in out["steps"])


def test_option_e_starting_over_clears_the_saved_place_and_a_missing_user_is_said(deeplink, monkeypatch):
    jf = SavingJellyfin()
    monkeypatch.setattr(orchestrator, "_playing_item", lambda jf_, driver, item_id: {"Name": "Heat"})
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf_, driver, item_id: {"PlayState": {"PositionTicks": 0}})
    assert orchestrator.play_deeplink(Driver(), jf, Book(), "m1", 0, resume_mode="e", user_id=ALICE)["ok"] and jf.saved == [(ALICE, "m1", 0)]
    nobody = SavingJellyfin()
    out = orchestrator.play_deeplink(Driver(), nobody, Book(), "m1", HOUR, resume_mode="e", user_id=None)
    assert nobody.saved == [] and any("No Jellyfin user is set for this profile" in s for s in out["steps"])
    nothing = SavingJellyfin()
    orchestrator.play_deeplink(Driver(), nothing, Book(), "m1", None, resume_mode="e", user_id=ALICE)
    assert nothing.saved == []                                                                   # no place asked for: nothing to save


class KeyDriver(Driver):
    cfg = {}
    keymap = {"right": "Right"}

    def __init__(self):
        self.keys = []

    def send_key(self, native, count=1):
        self.keys.append((native, count))


def test_option_c_presses_right_about_as_often_as_the_place_needs_and_checks_where_it_landed(monkeypatch, steps):
    monkeypatch.setattr(orchestrator, "_wait_for", lambda fn, timeout, interval=2: fn())
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf, driver, item_id: {"PlayState": {"PositionTicks": 5 * 600_000_000}})
    driver = KeyDriver()
    assert orchestrator.press_to(None, driver, "m1", 5 * 600_000_000, steps) is True
    assert driver.keys == [("Right", 30)] and "now near 5:00" in steps.got[-1][0]
    driver.keys.clear()
    driver.cfg = {"resume_key_seconds": 30}
    orchestrator.press_to(None, driver, "m1", 5 * 600_000_000, steps)
    assert driver.keys == [("Right", 10)]
    driver.keys.clear()
    orchestrator.press_to(None, driver, "m1", 5 * HOUR, steps)
    assert driver.keys == [("Right", orchestrator.MAX_SKIP_PRESSES)]                              # never an endless run of key presses


def test_option_c_reports_when_the_app_is_not_where_it_should_be(monkeypatch, steps):
    monkeypatch.setattr(orchestrator, "_wait_for", lambda fn, timeout, interval=2: fn())
    monkeypatch.setattr(orchestrator, "_playing_session", lambda jf, driver, item_id: {"PlayState": {"PositionTicks": 0}})
    assert orchestrator.press_to(None, KeyDriver(), "m1", 5 * 600_000_000, steps) is False and not steps.got[-1][1] and "resume_key_seconds" in steps.got[-1][0]


def test_the_choice_is_saved_per_tv_checked_and_handed_to_the_play_route(alice, tv, monkeypatch):
    assert alice.get("/api/tv/config").get_json()["devices"][0]["resumeMode"] == "e"                 # a deep-link TV starts on option e
    assert alice.put("/api/tv/prefs/resume/den", json={"mode": "c"}).get_json()["ok"]
    assert alice.get("/api/tv/config").get_json()["devices"][0]["resumeMode"] == "c"
    assert not alice.put("/api/tv/prefs/resume/den", json={"mode": "z"}).get_json()["ok"]
    use(tv, monkeypatch, items={"m1": MOVIE}, data={"m1": {"PlaybackPositionTicks": HOUR}})
    assert play(alice, "m1")["ok"] and RecordingTV.how == {"resume_mode": "c", "user_id": ALICE}
    runtime_prefs = tv.prefs["resume"]
    assert runtime_prefs == {"den": "c"}


def test_option_a_streams_a_resumed_title_from_the_saved_place_but_a_fresh_one_still_uses_the_link(alice, tv, monkeypatch):
    from synchub.tv.devices import REGISTRY

    class Streamer(RecordingTV):
        type = "streamer"
        capabilities = frozenset({"deeplink", "url_play"})
        streamed = []

    monkeypatch.setitem(REGISTRY, "streamer", Streamer)
    tv.prefs["devices"] = [{"id": "den", "name": "Den", "type": "streamer", "host": "10.0.0.5"}]
    tv.reload_devices()
    monkeypatch.setattr(orchestrator, "play_url", lambda driver, jf, logbook, item_id, start_ticks=None: Streamer.streamed.append((item_id, start_ticks)) or {"ok": True, "steps": ["streamed"], "confirmed": None})
    use(tv, monkeypatch, items={"m1": MOVIE}, data={"m1": {"PlaybackPositionTicks": HOUR}})
    assert alice.put("/api/tv/prefs/resume/den", json={"mode": "a"}).get_json()["ok"]
    assert play(alice, "m1")["ok"] and Streamer.streamed == [("m1", HOUR)] and RecordingTV.played == []
    use(tv, monkeypatch, items={"m1": MOVIE})                                                   # nothing to pick up: the normal link
    assert play(alice, "m1")["ok"] and RecordingTV.played == [("m1", None)]
