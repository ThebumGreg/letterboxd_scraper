"""UniFi Protect cameras on a TV: the Protect client, the relay the worker serves them from, and putting a camera on a TV and coming back."""
import pytest

from settings_validation import validate_setting
from synchub import protect, settings, storage, workers
from synchub.clients import ApiError, ConfigError
from synchub.tv import camera, runtime
from synchub.tv.devices import REGISTRY, DeviceError, Driver
from synchub.web import create_app

from .agent_module import agent

RTSPS = "rtsps://192.168.1.1:7441/AbC123?enableSrtp"


class Answer:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.content = body, status, b"x"

    def json(self):
        return self.body


class FakeProtect:
    """The Integration API as far as the client uses it. `streams` holds what Protect has already made."""
    def __init__(self, cameras, streams=None, status=200):
        self.cameras, self.streams, self.status, self.calls = cameras, streams or {}, status, []

    def __call__(self, service, event, method, url, **kw):
        self.calls.append((method, url.split("/integration/v1")[-1], kw.get("headers", {}).get("X-API-KEY"), kw.get("verify")))
        if self.status != 200:
            return Answer({}, self.status)
        path = url.split("/integration/v1")[-1]
        if path == "/meta/info":
            return Answer({"applicationVersion": "6.1.0"})
        if path == "/cameras":
            return Answer(self.cameras)
        cam = path.split("/")[2]
        if method == "POST":
            self.streams.setdefault(cam, {}).update({q: RTSPS.replace("AbC123", f"{cam}-{q}") for q in kw["json"]["qualities"]})
        return Answer(self.streams.get(cam, {}))


CAMERAS = [{"id": "c1", "name": "Front Door", "state": "CONNECTED", "modelKey": "camera"},
           {"id": "c2", "name": "Front Door", "state": "CONNECTED"},
           {"id": "c3", "name": "Garage", "state": "DISCONNECTED"},
           {"id": "c4", "name": "Backyard!", "state": "CONNECTED"}]


@pytest.fixture
def fake(v2, monkeypatch):
    f = FakeProtect(CAMERAS)
    monkeypatch.setattr(protect.http, "request", f)
    protect.forget()
    for key, value in (("protect_enabled", "1"), ("protect_host", "192.168.1.1"), ("protect_api_key", "key-1")):
        settings.set_value(key, value)
    yield f
    protect.forget()


# ================================================================ settings
def test_protect_settings_are_checked():
    assert validate_setting("protect_host", "https://192.168.1.1/") == (True, "192.168.1.1")
    assert validate_setting("protect_host", "udm.local:443") == (True, "udm.local:443")
    assert not validate_setting("protect_host", "not a host")[0]
    assert validate_setting("protect_relay_worker", "3") == (True, "3")
    assert not validate_setting("protect_relay_worker", "x")[0]
    assert validate_setting("protect_quality", "high") == (True, "high")
    assert not validate_setting("protect_quality", "ultra")[0]
    assert not validate_setting("protect_viewer_app", "vlc")[0]
    assert "protect_api_key" in settings.SECRET_FIELDS


# ================================================================ the Protect client
def test_cameras_come_back_connected_first_and_the_key_is_sent(v2, fake):
    cams = protect.ProtectClient().cameras()
    assert [c["name"] for c in cams] == ["Backyard!", "Front Door", "Front Door", "Garage"]
    assert fake.calls[0] == ("GET", "/cameras", "key-1", False)             # the console's own certificate is not checked unless asked for


@pytest.mark.parametrize("status,words", [(401, "rejected the API key"), (404, "no Integration API"), (500, "HTTP 500")])
def test_a_refusal_says_what_went_wrong(v2, fake, status, words):
    fake.status = status
    with pytest.raises(ApiError, match=words):
        protect.ProtectClient().cameras()


def test_a_tls_failure_is_a_certificate_problem_only_when_the_check_is_on(v2, monkeypatch):
    import requests

    def fail(text):
        def call(*a, **k):
            raise requests.exceptions.SSLError(text)
        return call
    monkeypatch.setattr(protect.http, "request", fail("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    with pytest.raises(ApiError, match="certificate is not trusted"):
        protect.ProtectClient("192.168.1.1", "k", verify=True).info()
    with pytest.raises(ApiError, match="secure \\(HTTPS\\) connection to 192.168.1.1"):          # with the check off it cannot be the certificate
        protect.ProtectClient("192.168.1.1", "k", verify=False).info()
    monkeypatch.setattr(protect.http, "request", fail("HTTPSConnectionPool: (Caused by SSLError(SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number')))"))
    with pytest.raises(ApiError, match="WRONG_VERSION_NUMBER"):
        protect.ProtectClient("192.168.1.1:80", "k", verify=True).info()


def test_no_address_or_key_is_a_configuration_error(v2):
    with pytest.raises(ConfigError):
        protect.ProtectClient("", "")


def test_a_stream_is_made_once_and_then_reused(v2, fake):
    client = protect.ProtectClient()
    first = client.rtsps("c1", "medium")
    assert first.startswith("rtsps://192.168.1.1:7441/c1-medium")
    assert client.rtsps("c1", "medium") == first
    assert [c[0] for c in fake.calls] == ["GET", "POST", "GET"]


def test_the_relay_reads_protects_address_as_rtspx_without_the_srtp_flag():
    assert protect.relay_source(RTSPS) == "rtspx://192.168.1.1:7441/AbC123"


def test_stream_names_are_plain_words_and_never_repeat_and_disconnected_cameras_are_left_out(v2, fake):
    found = protect.relay_streams(protect.ProtectClient(), "low")
    assert list(found) == ["backyard", "front-door", "front-door-2"]
    assert found["backyard"]["title"] == "Backyard!"
    assert found["backyard"]["src"] == "rtspx://192.168.1.1:7441/c4-low"


# ================================================================ the relay
def test_streams_are_asked_for_once_and_kept(v2, fake):
    protect.streams()
    n = len(fake.calls)
    protect.streams()
    assert len(fake.calls) == n


def test_a_failed_ask_keeps_the_last_good_list(v2, fake, monkeypatch):
    good = protect.streams()
    fake.status = 500
    assert protect.streams(force=True) == good


def test_only_the_chosen_worker_is_told_to_serve_cameras(v2, conn, fake):
    one, _ = workers.create(conn, "encoder-1")
    two, _ = workers.create(conn, "encoder-2")
    conn.commit()
    assert protect.for_worker(one) is None                                    # nobody chosen yet
    settings.set_value("protect_relay_worker", str(one))
    assert set(protect.for_worker(one)["streams"]) == {"backyard", "front-door", "front-door-2"}
    assert protect.for_worker(two) is None
    settings.set_value("protect_enabled", "0")
    assert protect.for_worker(one) is None


def test_the_relay_address_is_learned_from_the_workers_poll_unless_typed(v2, conn, fake):
    worker, _ = workers.create(conn, "encoder-1")
    conn.commit()
    settings.set_value("protect_relay_worker", str(worker))
    protect.note_worker_address(worker, "192.168.1.30")
    assert protect.play_url("backyard") == "rtsp://192.168.1.30:8554/backyard"
    settings.set_value("protect_relay_host", "10.0.0.9:80")
    assert protect.play_url("backyard", "hls") == "http://10.0.0.9:1984/api/stream.m3u8?src=backyard"
    protect.note_worker_address(worker + 1, "1.2.3.4")                         # another worker does not move the relay
    assert settings.get("protect_relay_addr") == "192.168.1.30"


def test_the_poll_answer_carries_the_streams_for_the_relay_worker_only(v2, conn, fake):
    worker, token = workers.create(conn, "encoder-1")
    conn.commit()
    settings.set_value("protect_relay_worker", str(worker))
    client = create_app(csrf=False).test_client()
    answer = client.post("/api/worker/v1/poll", json={}, headers={"Authorization": f"Bearer {token}"}).get_json()
    assert set(answer["relay"]["streams"]) == {"backyard", "front-door", "front-door-2"}
    settings.set_value("protect_relay_worker", "")
    assert "relay" not in client.post("/api/worker/v1/poll", json={}, headers={"Authorization": f"Bearer {token}"}).get_json()


def test_the_relay_workers_compose_file_has_go2rtc(v2, conn, fake):
    worker, _ = workers.create(conn, "encoder-1")
    conn.commit()
    assert "go2rtc" not in storage.compose_file("encoder-1", [])
    text = storage.compose_file("encoder-1", [], relay=True)
    assert "alexxit/go2rtc" in text and "GO2RTC_URL: http://go2rtc:1984" in text and '"8554:8554"' in text
    assert not protect.is_relay_name(conn, "encoder-1")
    settings.set_value("protect_relay_worker", str(worker))
    assert protect.is_relay_name(conn, "encoder-1") and not protect.is_relay_name(conn, "other")


# ================================================================ the agent gives go2rtc its cameras
class Relay:
    """A stand-in for go2rtc's web address, kept in place of urllib's urlopen."""
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, request, timeout=None):
        if self.fail:
            raise OSError("connection refused")
        self.calls.append((request.get_method(), request.full_url))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


class Stub:
    RELAY_RESEND = agent.Agent.RELAY_RESEND
    _relay_digest, _relay_at, _relay_names = None, 0.0, set()

    class log:
        messages = []
        warning = info = classmethod(lambda cls, text, **f: cls.messages.append(text))


def stub():
    s = Stub()
    s.log = type("L", (), {"messages": [], "warning": lambda self, t, **f: self.messages.append(t), "info": lambda self, t, **f: self.messages.append(t)})()
    return s


def test_the_agent_puts_each_stream_in_go2rtc_once_and_takes_away_ones_that_went(monkeypatch):
    relay = Relay()
    monkeypatch.setattr(agent.urllib.request, "urlopen", relay)
    monkeypatch.setenv("GO2RTC_URL", "http://go2rtc:1984")
    s = stub()
    agent.Agent.sync_relay(s, {"streams": {"front": "rtspx://c/1", "back": "rtspx://c/2"}})
    assert sorted(c[0] for c in relay.calls) == ["PUT", "PUT"]
    assert "name=front&src=rtspx%3A%2F%2Fc%2F1" in relay.calls[0][1] or "name=front&src=rtspx%3A%2F%2Fc%2F1" in relay.calls[1][1]
    agent.Agent.sync_relay(s, {"streams": {"front": "rtspx://c/1", "back": "rtspx://c/2"}})
    assert len(relay.calls) == 2                                              # unchanged: not sent again
    agent.Agent.sync_relay(s, {"streams": {"front": "rtspx://c/1"}})
    assert relay.calls[-1][0] == "DELETE" and "src=back" in relay.calls[-1][1]
    assert not any("rtspx" in m for m in s.log.messages)                      # the stream addresses hold Protect's keys: never logged


def test_the_agent_does_nothing_without_a_relay_address_and_tries_again_when_go2rtc_is_down(monkeypatch):
    relay = Relay(fail=True)
    monkeypatch.setattr(agent.urllib.request, "urlopen", relay)
    monkeypatch.delenv("GO2RTC_URL", raising=False)
    s = stub()
    agent.Agent.sync_relay(s, {"streams": {"front": "x"}})
    assert s._relay_digest is None and not s.log.messages
    monkeypatch.setenv("GO2RTC_URL", "http://go2rtc:1984")
    agent.Agent.sync_relay(s, {"streams": {"front": "x"}})
    agent.Agent.sync_relay(s, {"streams": {"front": "x"}})
    assert len(s.log.messages) == 1 and s._relay_digest is None               # said once, and the next poll tries again
    relay.fail = False
    agent.Agent.sync_relay(s, {"streams": {"front": "x"}})
    assert s._relay_digest and relay.calls


# ================================================================ a camera on a TV
class FakeJellyfin:
    def __init__(self, playing=None, paused=False, closes_app=False):
        self.paused, self.closes_app, self.commands = paused, closes_app, []
        self.item = playing
        self.logbook = None

    def sessions(self, quiet=True):
        now = {"Id": self.item, "Name": "Heat"} if self.item else None
        return [{"Id": "s1", "Client": "Android TV", "DeviceName": "tv", "SupportsRemoteControl": True, "NowPlayingItem": now,
                 "PlayState": {"IsPaused": self.paused, "PositionTicks": 600_000_000}}]

    def playstate(self, sid, command, params=None):
        self.commands.append(command)
        self.paused = command == "Pause"
        return type("R", (), {"status_code": 204, "ok": True})()

    def play(self, sid, params):
        self.commands.append(("play", params["itemIds"], params["startPositionTicks"]))
        self.item, self.paused = params["itemIds"], False
        return type("R", (), {"status_code": 204, "ok": True})()


class CameraTV(Driver):
    type = "cameratv"
    label = "Camera TV"
    capabilities = frozenset({"stream_view"})
    default_session_rule = {"client": "Android TV"}
    events = []

    def status(self, quiet=False):
        return {"online": True, "power": "on", "app": "jf"}

    def view_stream(self, url, package):
        self.events.append(("view", url, package))
        return "opened"

    def close_stream(self, package):
        self.events.append(("close", package))
        return "closed"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setitem(REGISTRY, "cameratv", CameraTV)
    rt = runtime.configure(tmp_path / "tv")
    rt.prefs["devices"] = [{"id": "den", "name": "Den", "type": "cameratv", "host": "10.0.0.5"}]
    rt.reload_devices()
    CameraTV.events = []
    monkeypatch.setattr(camera.time, "sleep", lambda s: None)
    monkeypatch.setattr("synchub.tv.orchestrator.time.sleep", lambda s: None)
    monkeypatch.setattr(camera, "_has_jellyfin", lambda: True)
    yield rt
    runtime._runtime = None


def use(rt, monkeypatch, **kw):
    jf = FakeJellyfin(**kw)
    monkeypatch.setattr(rt, "jf", lambda: jf)
    return jf


FRONT = {"name": "front", "title": "Front Door"}


def test_a_camera_pauses_what_is_playing_and_closing_it_carries_on(rig, monkeypatch):
    jf = use(rig, monkeypatch, playing="m1")
    den = rig.devices["den"]
    shown = camera.show(rig, den, FRONT, "rtsp://relay/front", "org.videolan.vlc")
    assert shown["ok"] and jf.commands == ["Pause"]
    assert CameraTV.events == [("view", "rtsp://relay/front", "org.videolan.vlc")]
    assert shown["state"] == {"camera": "front", "title": "Front Door", "resume": {"title": "Heat", "wasPlaying": True, "ticks": pytest.approx(600_000_000, abs=400_000_000)}}
    closed = camera.close(rig, den, "org.videolan.vlc")
    assert closed["ok"] and closed["state"] is None
    assert jf.commands == ["Pause", "Unpause"] and CameraTV.events[-1] == ("close", "org.videolan.vlc")


def test_a_title_that_was_already_paused_stays_paused(rig, monkeypatch):
    jf = use(rig, monkeypatch, playing="m1", paused=True)
    camera.show(rig, rig.devices["den"], FRONT, "rtsp://relay/front", "p.k")
    camera.close(rig, rig.devices["den"], "p.k")
    assert jf.commands == []


def test_switching_cameras_keeps_the_first_memory(rig, monkeypatch):
    jf = use(rig, monkeypatch, playing="m1")
    den = rig.devices["den"]
    camera.show(rig, den, FRONT, "rtsp://relay/front", "p.k")
    camera.show(rig, den, {"name": "back", "title": "Backyard"}, "rtsp://relay/back", "p.k")
    assert jf.commands == ["Pause"]                                           # not paused again, and the second camera does not forget the title
    assert [e[0] for e in CameraTV.events] == ["view", "close", "view"]
    assert camera.status(rig, "den")["resume"]["title"] == "Heat"


def test_when_jellyfin_closed_the_title_it_is_opened_again_at_the_saved_place(rig, monkeypatch):
    jf = use(rig, monkeypatch, playing="m1")
    den = rig.devices["den"]
    camera.show(rig, den, FRONT, "rtsp://relay/front", "p.k")
    jf.item = None                                                            # the app was closed while the camera was up
    monkeypatch.setattr("synchub.tv.orchestrator.prepare", lambda *a, **k: {"ok": True, "steps": ["ready"], "session": {"Id": "s1"}})
    monkeypatch.setattr("synchub.tv.orchestrator.confirm_playing", lambda *a, **k: (True, "Heat", 1))
    monkeypatch.setattr("synchub.tv.orchestrator._wait_for", lambda predicate, timeout, interval=2: predicate())
    result = camera.close(rig, den, "p.k")
    assert result["ok"] and jf.commands[-1][0] == "play" and jf.commands[-1][1] == "m1" and jf.commands[-1][2] > 0


def test_a_camera_with_nothing_playing_just_opens_and_closes(rig, monkeypatch):
    jf = use(rig, monkeypatch, playing=None)
    den = rig.devices["den"]
    shown = camera.show(rig, den, FRONT, "rtsp://relay/front", "p.k")
    assert shown["state"]["resume"] is None and jf.commands == []
    assert camera.close(rig, den, "p.k")["ok"]


def test_a_tv_that_cannot_show_a_stream_says_so(rig, monkeypatch):
    class Plain(CameraTV):
        capabilities = frozenset()
    with pytest.raises(DeviceError, match="cannot show a camera"):
        camera.show(rig, Plain({"id": "x", "host": "h"}, rig.logbook), FRONT, "rtsp://x", "p.k")


# ================================================================ the routes
@pytest.fixture
def browser(v2, rig, fake, monkeypatch):
    app = create_app(csrf=False)
    app.config["TESTING"] = True
    client = app.test_client()
    client.set_cookie("synchub_profile", "0")
    settings.set_value("protect_relay_host", "192.168.1.30")
    return client


def test_the_camera_list_and_a_camera_on_the_tv(browser, rig, monkeypatch):
    jf = use(rig, monkeypatch, playing="m1")
    listing = browser.get("/api/tv/cameras").get_json()
    assert listing["enabled"] and [c["name"] for c in listing["cameras"]] == ["backyard", "front-door", "front-door-2"]
    shown = browser.post("/api/tv/devices/den/camera", json={"camera": "front-door"}).get_json()
    assert shown["ok"] and shown["state"]["title"] == "Front Door"
    assert CameraTV.events[0] == ("view", "rtsp://192.168.1.30:8554/front-door", "org.videolan.vlc")
    assert browser.get("/api/tv/devices/den/camera").get_json()["state"]["camera"] == "front-door"
    closed = browser.post("/api/tv/devices/den/camera/close").get_json()
    assert closed["ok"] and closed["state"] is None and jf.commands == ["Pause", "Unpause"]


def test_an_unknown_camera_is_refused_and_a_switched_off_protect_lists_nothing(browser, rig, monkeypatch):
    use(rig, monkeypatch, playing=None)
    assert browser.post("/api/tv/devices/den/camera", json={"camera": "nope"}).status_code == 502
    settings.set_value("protect_enabled", "0")
    assert browser.get("/api/tv/cameras").get_json() == {"enabled": False, "cameras": []}


def test_the_settings_test_button_reports_the_console(browser, fake):
    answer = browser.post("/api/test-connection/protect", json={"fields": {"protect_host": "192.168.1.1", "protect_api_key": ""}}).get_json()
    assert answer["ok"] and "6.1.0" in answer["message"] and "3 of 4" in answer["message"]
    fake.status = 401
    assert "rejected the API key" in browser.post("/api/test-connection/protect", json={"fields": {}}).get_json()["message"]
