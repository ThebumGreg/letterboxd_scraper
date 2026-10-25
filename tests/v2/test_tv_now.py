"""The "Playing now" strip on the dashboard: what each TV is playing through Jellyfin, from one call to Jellyfin and none to the TVs."""
from datetime import datetime, timedelta, timezone

import pytest

from config import BASE_DIR
from synchub import settings
from synchub.tv import runtime
from synchub.tv.jellyfin import JellyfinError, is_stale, item_label
from synchub.web import create_app


def stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def session(client, device, now_playing=None, paused=False, checked=5, activity="2026-10-25T10:00:00Z"):
    return {"Id": f"{client}-{device}", "Client": client, "DeviceName": device, "SupportsRemoteControl": True, "LastActivityDate": activity,
            "NowPlayingItem": now_playing, "PlayState": {"IsPaused": paused, "PositionTicks": 0}, "LastPlaybackCheckIn": stamp(checked)}


HEAT = {"Id": "m1", "Type": "Movie", "Name": "Heat", "ProductionYear": 1995}
EPISODE = {"Id": "e1", "Type": "Episode", "Name": "Who Is Alive?", "SeriesName": "Severance", "ParentIndexNumber": 1, "IndexNumber": 7}


class FakeJellyfin:
    def __init__(self, sessions):
        self._sessions = sessions

    def sessions(self, quiet=True):
        return self._sessions


@pytest.fixture
def tv(tmp_path):
    rt = runtime.configure(tmp_path / "tv")
    rt.prefs["devices"] = [{"id": "bed", "name": "Bed Room Roku", "type": "roku", "host": "10.0.0.5"},
                           {"id": "den", "name": "Den", "type": "roku", "host": "10.0.0.6", "jellyfin": {"session": {"device": "Den Roku"}}}]
    rt.reload_devices()
    yield rt
    runtime._runtime = None


@pytest.fixture
def browser(v2):
    app = create_app(csrf=False)
    app.config["TESTING"] = True
    client = app.test_client()
    client.set_cookie("synchub_profile", "0")
    return client


def now(browser):
    return browser.get("/api/tv/now").get_json()


def test_each_tv_playing_something_is_listed_with_what_it_plays(browser, tv, monkeypatch):
    monkeypatch.setattr(tv, "jf", lambda: FakeJellyfin([
        session("Jellyfin Roku", "Bed Room Roku", HEAT),
        session("Jellyfin Roku", "Den Roku", EPISODE, paused=True),
        session("Swiftfin", "Alice's iPhone", HEAT)]))                               # not a TV SyncHub controls
    got = {t["id"]: t for t in now(browser)["tvs"]}
    assert got["bed"] == {"id": "bed", "name": "Bed Room Roku", "title": "Heat (1995)", "paused": False}
    assert got["den"]["title"] == "Severance S1E7 - Who Is Alive?" and got["den"]["paused"] is True


def test_a_tv_with_nothing_playing_or_only_a_leftover_session_is_not_listed(browser, tv, monkeypatch):
    monkeypatch.setattr(tv, "jf", lambda: FakeJellyfin([
        session("Jellyfin Roku", "Bed Room Roku", None),                              # the app is open, nothing playing
        session("Jellyfin Roku", "Den Roku", HEAT, checked=600)]))                    # said "playing" ten minutes ago: the TV was switched off
    assert now(browser)["tvs"] == []


def test_a_session_is_shown_on_one_tv_only(browser, tv, monkeypatch):
    tv.prefs["devices"].append({"id": "garage", "name": "Garage", "type": "roku", "host": "10.0.0.7"})      # same default rule as the others
    tv.reload_devices()
    monkeypatch.setattr(tv, "jf", lambda: FakeJellyfin([session("Jellyfin Roku", "Bed Room Roku", HEAT)]))
    assert [t["id"] for t in now(browser)["tvs"]] == ["bed"]


def test_the_newest_session_of_a_tv_is_the_one_shown(browser, tv, monkeypatch):
    monkeypatch.setattr(tv, "jf", lambda: FakeJellyfin([
        session("Jellyfin Roku", "Bed Room Roku", EPISODE, activity="2026-10-25T09:00:00Z"),
        session("Jellyfin Roku", "Bed Room Roku", HEAT, activity="2026-10-25T11:00:00Z")]))
    assert [t["title"] for t in now(browser)["tvs"] if t["id"] == "bed"] == ["Heat (1995)"]


def test_without_jellyfin_the_strip_just_has_nothing_to_show(browser, tv, monkeypatch):
    def fail():
        raise JellyfinError("Jellyfin is not set up")
    monkeypatch.setattr(tv, "jf", fail)
    assert now(browser) == {"ok": True, "tvs": []}


def test_the_labels_and_the_staleness_rule():
    assert item_label(HEAT) == "Heat (1995)" and item_label({"Name": "Untitled"}) == "Untitled" and item_label(EPISODE) == "Severance S1E7 - Who Is Alive?"
    assert is_stale({"LastPlaybackCheckIn": stamp(600)}) and not is_stale({"LastPlaybackCheckIn": stamp(30)}) and not is_stale({})


def test_the_strip_belongs_to_the_tv_permission_and_the_dashboard_carries_it(browser, tv, v2):
    page = browser.get("/").get_data(as_text=True)
    assert 'id="tvNow"' in page and "js/tv-now.js" in page
    settings.set_value("access_guest_permissions", "dashboard")
    assert browser.get("/api/tv/now").status_code == 403
    assert '"tv": false' in browser.get("/").get_data(as_text=True)


def test_the_strip_script_links_each_tv_to_its_remote_and_stops_when_not_allowed():
    script = (BASE_DIR / "static/js/tv-now.js").read_text()
    assert "/tv/remote?tv=" in script and "[401, 403, 404]" in script and "if (!cfg.tv) return;" in script
