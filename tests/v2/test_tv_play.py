"""Playing a title through a TV's Jellyfin session (a Google TV): a leftover session, something already playing, and an app that takes the command but does nothing."""
import pytest

from synchub.tv import orchestrator, runtime
from synchub.tv.devices import REGISTRY, Driver
from synchub.web import create_app

APP = "org.jellyfin.androidtv"
SESSION = {"Id": "sess1", "Client": "Jellyfin Android TV", "DeviceName": "The Big One", "LastActivityDate": "2026-10-25T10:00:00Z"}


class World:
    """Everything the TV and its Jellyfin session do, in order, so a test can read what happened."""
    def __init__(self):
        self.events = []
        self.app = "launcher"
        self.app_open = False            # has the Jellyfin app registered a controllable session yet?
        self.now_playing = None
        self.activity = 0


class FakeTV(Driver):
    type = "sessiontv"
    label = "Session TV"
    capabilities = frozenset({"keys", "launch"})
    keymap = {"home": "KEYCODE_HOME"}
    default_session_rule = {"client": "Jellyfin Android TV", "device": "The Big One"}
    world = None

    def status(self, quiet=False):
        return {"online": True, "power": "on", "app": self.world.app}

    def jellyfin_app(self):
        return APP

    def launch(self, app_id, params=None):
        self.world.events.append(f"launch {app_id}")
        self.world.app, self.world.app_open = app_id, True
        self.world.activity += 1
        return "launched"

    def send_key(self, native, count=1):
        self.world.events.append(f"key {native}")
        if native == "KEYCODE_HOME":
            self.world.app = "launcher"


class FakeResponse:
    def __init__(self, status=204):
        self.status_code, self.ok = status, status < 400


class FakeJf:
    def __init__(self, world):
        self.world = world
        self.logbook = None

    def sessions(self, quiet=True):
        w = self.world
        s = {**SESSION, "SupportsRemoteControl": w.app_open, "LastActivityDate": f"2026-10-25T10:00:{w.activity:02d}Z"}
        if w.now_playing:
            s["NowPlayingItem"] = w.now_playing
        return [s]

    def item(self, item_id):
        return {"Id": item_id, "Type": "Movie", "Name": f"Item {item_id}"}

    def play(self, sid, params):
        self.world.events.append(f"play {params['itemIds']}")
        return FakeResponse()

    def playstate(self, sid, command, params=None):
        self.world.events.append(f"playstate {command}")
        if command == "Stop":
            self.world.now_playing = None
        return FakeResponse()


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World()
    FakeTV.world = w
    monkeypatch.setitem(REGISTRY, "sessiontv", FakeTV)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: None)      # no real waiting between steps
    rt = runtime.configure(tmp_path / "tv")
    rt.prefs["devices"] = [{"id": "front", "name": "Front Room", "type": "sessiontv", "host": "10.0.0.5"}]
    rt.reload_devices()
    monkeypatch.setattr(rt, "jf", lambda: FakeJf(w))
    w.confirmations = []
    monkeypatch.setattr(orchestrator, "confirm_playing", lambda jf, sid, item, timeout=15: w.confirmations.pop(0) if w.confirmations else (True, "Item", 1.0))
    yield w
    runtime._runtime = None


@pytest.fixture
def browser(v2):
    app = create_app(csrf=False)
    app.config["TESTING"] = True
    client = app.test_client()
    client.set_cookie("synchub_profile", "0")
    return client


def play(browser, item="m1"):
    return browser.post("/api/tv/devices/front/play", json={"itemIds": [item], "playCommand": "PlayNow", "prepare": True}).get_json()


def test_a_leftover_session_that_cannot_be_controlled_does_not_stop_the_app_being_opened(browser, world):
    """The app was closed (another app in front); Jellyfin still listed its old session as not remote controllable. That used to end the play at once."""
    answer = play(browser)
    assert answer["ok"] is True and answer["confirmed"] is True
    assert world.events == [f"launch {APP}", "play m1"]
    assert "cannot be controlled remotely" not in answer["body"]


def test_an_app_in_front_whose_session_cannot_be_controlled_still_explains_it(browser, world):
    world.app, world.app_open = APP, False
    answer = play(browser)
    assert answer["ok"] is False and "cannot be controlled remotely" in answer["body"]
    assert not [e for e in world.events if e.startswith("play")]


def test_something_already_playing_is_stopped_before_the_new_title_is_sent(browser, world):
    world.app, world.app_open, world.now_playing = APP, True, {"Id": "old", "Name": "Backrooms"}
    answer = play(browser, "new")
    assert answer["ok"] and answer["confirmed"]
    assert world.events == ["playstate Stop", "play new"]
    assert any('"Backrooms" is still open in the app: stopping it first' in step for step in answer["steps"])


def test_the_same_title_already_playing_is_not_stopped(browser, world):
    world.app, world.app_open, world.now_playing = APP, True, {"Id": "m1", "Name": "Item m1"}
    play(browser, "m1")
    assert "playstate Stop" not in world.events


def test_an_app_that_takes_the_command_and_does_nothing_gets_home_a_reopen_and_one_more_try(browser, world):
    world.app, world.app_open = APP, True
    world.confirmations = [(False, None, 15), (True, "Item m1", 2.0)]
    answer = play(browser)
    assert answer["ok"] and answer["confirmed"] is True
    assert world.events == ["play m1", "key KEYCODE_HOME", f"launch {APP}", "play m1"]
    assert any("going Home, reopening the Jellyfin app and trying once more" in step for step in answer["steps"])


def test_when_the_second_try_fails_too_the_answer_says_so_so_the_box_stays_open(browser, world):
    world.app, world.app_open = APP, True
    world.confirmations = [(False, None, 15), (False, None, 15)]
    answer = play(browser)
    assert answer["ok"] is True and answer["confirmed"] is False
    assert [e for e in world.events if e.startswith("play")] == ["play m1", "play m1"]          # exactly one retry, not a loop


def test_playing_at_once_needs_no_retry(browser, world):
    world.app, world.app_open = APP, True
    assert play(browser)["confirmed"] is True and world.events == ["play m1"]


def test_the_watch_box_closes_on_success_and_stays_for_a_problem():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[2] / "static/tv/common.js").read_text()
    assert "W.done = r.ok && r.confirmed ? 'good' : 'problem';" in script
    assert "$('#wClose').classList.toggle('primary', outcome === 'good')" in script and "$('#wRetry').hidden = outcome !== 'problem'" in script
    assert "W.done === 'good'" in script               # a tap outside closes it only after playback started
    assert "watchClose" in script and "data-act=\"watchRetry\"" in script
