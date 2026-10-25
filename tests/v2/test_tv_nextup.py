"""Watch now on a show plays the episode its viewer should watch next, for the profile chosen on the Who-is-watching page."""
import pytest

from synchub.tv import runtime
from synchub.tv.devices import REGISTRY, Driver
from synchub.web import create_app


class FakeJellyfin:
    """Just what the play route asks of Jellyfin."""
    def __init__(self, items, next_up=None, first=None):
        self.items, self._next, self._first, self.asked = items, next_up, first, []

    def item(self, item_id):
        return self.items.get(item_id, {})

    def user_data(self, item_id, user_id):
        self.asked.append(("user_data", item_id, user_id))
        return {}                                          # nobody has started anything here (test_tv_resume has the viewers who have)

    def next_up(self, series_id, user_id=None):
        self.asked.append(("next_up", series_id, user_id))
        return self._next

    def first_episode(self, series_id, user_id=None):
        self.asked.append(("first", series_id, user_id))
        return self._first


class RecordingTV(Driver):
    """A TV that records the title it was asked to play (through the deep-link route, which skips the Jellyfin session)."""
    type = "recordingtv"
    label = "Recording TV"
    capabilities = frozenset({"deeplink"})
    deeplink_types = frozenset({"movie", "episode", "series"})
    played = []

    def status(self, quiet=False):
        return {"online": True, "power": "on", "app": "jf"}


@pytest.fixture
def tv(tmp_path, monkeypatch):
    monkeypatch.setitem(REGISTRY, "recordingtv", RecordingTV)             # only for these tests
    rt = runtime.configure(tmp_path / "tv")
    rt.prefs["devices"] = [{"id": "den", "name": "Den", "type": "recordingtv", "host": "10.0.0.5"}]
    rt.reload_devices()
    RecordingTV.played = []
    from synchub.tv import orchestrator

    def fake_play_deeplink(driver, jf, logbook, item_id, start_ticks=None, timeout=45):
        RecordingTV.played.append((item_id, start_ticks))
        return {"ok": True, "steps": [f"Asked the Jellyfin app to play {item_id}"], "confirmed": True, "retryable": False}
    monkeypatch.setattr(orchestrator, "play_deeplink", fake_play_deeplink)
    yield rt
    runtime._runtime = None


@pytest.fixture
def browser(v2):
    app = create_app(csrf=False)
    app.config["TESTING"] = True
    client = app.test_client()
    client.set_cookie("synchub_profile", "0")
    return client


def play(browser, item_id, **extra):
    return browser.post("/api/tv/devices/den/play", json={"itemIds": [item_id], "playCommand": "PlayNow", **extra}).get_json()


SHOW = {"Id": "show1", "Type": "Series", "Name": "Severance"}
EPISODE = {"Id": "ep7", "Type": "Episode", "Name": "Who Is Alive?", "ParentIndexNumber": 1, "IndexNumber": 7}


def test_a_show_is_played_from_its_next_episode(browser, tv, monkeypatch):
    jf = FakeJellyfin({"show1": SHOW}, next_up=EPISODE)
    monkeypatch.setattr(tv, "jf", lambda: jf)
    answer = play(browser, "show1", startPositionTicks=123)
    assert answer["ok"] and RecordingTV.played == [("ep7", None)]                  # the episode, and no resume point carried over from the show
    assert 'playing next up' in answer["body"] and "S1E7 - Who Is Alive?" in answer["steps"][0] and answer["steps"][1].startswith("Asked the Jellyfin app")
    assert jf.asked == [("next_up", "show1", None)]


def test_the_profile_chosen_on_the_who_is_watching_page_decides_whose_next_up_it_is(browser, tv, monkeypatch, conn):
    conn.execute("INSERT INTO profile (id, name, jellyfin_user_id) VALUES (1, 'Alice', 'aaa111')")
    conn.commit()
    browser.set_cookie("synchub_profile", "1")
    jf = FakeJellyfin({"show1": SHOW}, next_up=EPISODE)
    monkeypatch.setattr(tv, "jf", lambda: jf)
    answer = play(browser, "show1")
    assert jf.asked == [("next_up", "show1", "aaa111"), ("user_data", "ep7", "aaa111")] and "for Alice" in answer["steps"][0]


def test_a_show_that_is_all_watched_starts_again_from_its_first_episode(browser, tv, monkeypatch):
    first = {**EPISODE, "Id": "ep1", "IndexNumber": 1, "Name": "Good News About Hell"}
    jf = FakeJellyfin({"show1": SHOW}, next_up=None, first=first)
    monkeypatch.setattr(tv, "jf", lambda: jf)
    answer = play(browser, "show1")
    assert RecordingTV.played == [("ep1", None)] and "from the first episode" in answer["steps"][0]


def test_a_show_with_no_episodes_says_so_and_plays_nothing(browser, tv, monkeypatch):
    monkeypatch.setattr(tv, "jf", lambda: FakeJellyfin({"show1": SHOW}))
    answer = play(browser, "show1")
    assert answer["ok"] is False and "no episodes of \"Severance\"" in answer["body"] and RecordingTV.played == []


def test_a_movie_or_an_episode_is_played_as_asked(browser, tv, monkeypatch):
    jf = FakeJellyfin({"m1": {"Id": "m1", "Type": "Movie", "Name": "Heat"}, "ep7": EPISODE})
    monkeypatch.setattr(tv, "jf", lambda: jf)
    assert play(browser, "m1", startPositionTicks=5)["ok"] and play(browser, "ep7")["ok"]
    assert RecordingTV.played == [("m1", 5), ("ep7", None)] and jf.asked == []
    assert "steps" in play(browser, "m1") and "is a show" not in str(play(browser, "m1")["steps"])
