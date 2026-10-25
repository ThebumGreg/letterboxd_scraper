"""The TV menu: its pages, who may open them, the TVs and app choices it saves, finding TVs, and the two reliability fixes
(leave the screensaver before opening an app; go Home before opening another title on a Roku)."""
import json
import re
import socket
import threading

import pytest

from synchub import settings
from synchub.tv import discovery, runtime
from synchub.tv.devices import roku
from synchub.tv.devices.googletv import GoogleTVDriver
from synchub.tv.devices.roku import RokuDriver
from synchub.tv.logbook import LogBook
from synchub.web import create_app


@pytest.fixture
def tv(tmp_path):
    """The TV remote's state, in a folder of its own."""
    rt = runtime.configure(tmp_path / "tv")
    yield rt
    runtime._runtime = None


@pytest.fixture
def csrf_app(v2):
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def post(client, path, body=None, method="post"):
    return getattr(client, method)(path, json=body or {})


# ---------------------------------------------------------------- pages and menu
def test_the_tv_menu_has_four_pages_and_marks_the_current_one(client, tv):
    assert client.get("/tv").headers["Location"].endswith("/tv/remote")
    for path in ("/tv/remote", "/tv/settings", "/tv/devices", "/tv/advanced"):
        assert client.get(path).status_code == 200, path
    page = client.get("/tv/settings").get_data(as_text=True)
    assert re.search(r'dropdown-toggle active"[^>]*>System', page)                         # TV apps lives under System, in the TV section
    assert re.search(r'dropdown-item active" href="/tv/settings" aria-current="page">TV apps', page)
    assert not re.search(r'nav-link active" href="/tv/remote"', page)
    assert re.search(r'nav-link active" href="/tv/remote"', client.get("/tv/remote").get_data(as_text=True))   # Remote is a main menu item


def test_the_pages_load_scripts_and_styles_that_exist(client, tv):
    from config import BASE_DIR
    for path in ("/tv/remote", "/tv/settings", "/tv/devices", "/tv/advanced"):
        page = client.get(path).get_data(as_text=True)
        assert 'class="tvr' in page
        for asset in re.findall(r'(?:src|href)="(/static/tv/[^"]+)"', page):
            assert (BASE_DIR / asset.removeprefix("/")).is_file(), asset


def test_every_api_address_the_scripts_call_exists(app):
    """A renamed route must not leave a script calling an address that is gone."""
    from config import BASE_DIR
    adapter = app.url_map.bind("localhost")
    missing = []
    for script in (BASE_DIR / "static" / "tv").glob("*.js"):
        for found in re.findall(r"""['`](/api/tv/[^'`?]*)""", script.read_text()):
            if "SETUP_PATH" in found:
                continue                                           # devPath(): checked by the next test
            path = re.sub(r"\$\{[^}]*\}", "x", found) + ("x" if found.endswith("/") else "")   # an id is appended to a trailing /
            if not any(_matches(adapter, path, method) for method in ("GET", "POST", "PUT", "DELETE")):
                missing.append(f"{script.name}: {found}")
    assert missing == []


def _matches(adapter, path, method):
    from werkzeug.exceptions import HTTPException
    for candidate in (path, path.rstrip("/")):
        try:
            adapter.match(candidate, method=method)
            return True
        except HTTPException:
            continue
    return False


def test_device_calls_go_to_the_setup_or_everyday_address_the_server_expects(app):
    from config import BASE_DIR
    setup_re = re.search(r"const SETUP_PATH = /\^\\/\(([^)]*)\)/", (BASE_DIR / "static" / "tv" / "common.js").read_text()).group(1).split("|")
    for rule in app.url_map.iter_rules():
        m = re.match(r"/api/tv/(setup/)?devices/<dev_id>/([\w-]+)", rule.rule)
        if m and m.group(2) != "icon":
            assert (m.group(1) is not None) == (m.group(2) in setup_re), rule.rule


def test_the_scripts_send_the_csrf_token(client, tv):
    from config import BASE_DIR
    assert "X-CSRFToken" in (BASE_DIR / "static" / "tv" / "common.js").read_text()


def test_a_script_call_without_the_csrf_token_is_refused(csrf_app, tv):
    browser = csrf_app.test_client()
    browser.set_cookie("synchub_profile", "0")
    assert post(browser, "/api/tv/setup/tvs", {"type": "roku", "host": "10.0.0.9"}).status_code == 400
    assert browser.get("/tv/remote").status_code == 200


# ---------------------------------------------------------------- permissions
def test_the_tv_permission_opens_the_remote_but_not_the_setup_pages(client, tv, v2):
    settings.set_value("access_guest_permissions", "tv")
    assert client.get("/tv/remote").status_code == 200 and client.get("/tv/settings").status_code == 200
    assert client.get("/api/tv/config").status_code == 200
    for path in ("/tv/devices", "/tv/advanced", "/api/tv/setup/tvs", "/api/tv/setup/discovery/ranges"):
        assert client.get(path).status_code == 403, path
    denied = post(client, "/api/tv/setup/tvs", {"type": "roku", "host": "10.0.0.9"})
    assert denied.status_code == 403 and denied.get_json()["ok"] is False and "TV setup" in denied.get_json()["message"]
    menu = client.get("/tv/remote").get_data(as_text=True)
    assert ">Remote<" in menu and ">TV apps<" in menu and 'href="/tv/devices"' not in menu.split('<div class="tvr')[0]


def test_without_the_tv_permission_nothing_is_open(client, tv, v2):
    settings.set_value("access_guest_permissions", "dashboard")
    for path in ("/tv/remote", "/tv/settings", "/api/tv/config", "/api/tv/log"):
        assert client.get(path).status_code == 403, path


# ---------------------------------------------------------------- TVs saved from the page
def test_a_tv_can_be_added_edited_and_removed_and_survives_a_restart(client, tv, tmp_path):
    added = post(client, "/api/tv/setup/tvs", {"name": "Den TV", "type": "roku", "host": "10.0.0.9"}).get_json()
    assert added["ok"] and added["tv"]["id"] == "den-tv" and added["tv"]["managed"]
    assert [t["name"] for t in client.get("/api/tv/setup/tvs").get_json()["tvs"]] == ["Den TV"]
    assert post(client, "/api/tv/setup/tvs/den-tv", {"name": "Den", "type": "roku", "host": "10.0.0.9"}, "put").get_json()["ok"]
    assert runtime.configure(tmp_path / "tv").devices["den-tv"].name == "Den"      # a new process reads the same folder
    assert post(client, "/api/tv/setup/tvs/den-tv", method="delete").get_json()["ok"]
    assert client.get("/api/tv/setup/tvs").get_json()["tvs"] == []


@pytest.mark.parametrize("body, message", [
    ({"type": "fridge", "host": "10.0.0.9"}, "choose a type"),
    ({"type": "roku", "host": "10.0.0.9; reboot"}, "IP address"),
    ({"type": "roku", "host": "10.0.0.9", "mac": "zz"}, "MAC address"),
    ({"type": "googletv", "host": "10.0.0.9", "sleep": "screensaver"}, "needs ADB"),
    ({"type": "roku", "host": "10.0.0.9", "sleep": "nap"}, "standby or screensaver"),
])
def test_a_tv_with_bad_details_is_refused_with_the_reason(client, tv, body, message):
    answer = post(client, "/api/tv/setup/tvs", body).get_json()
    assert answer["ok"] is False and message in answer["body"]
    assert tv.prefs["devices"] == []


def test_the_same_address_cannot_be_added_twice_and_ids_stay_unique(client, tv):
    assert post(client, "/api/tv/setup/tvs", {"name": "Den", "type": "roku", "host": "10.0.0.9"}).get_json()["ok"]
    assert "already added as Den" in post(client, "/api/tv/setup/tvs", {"name": "Other", "type": "roku", "host": "10.0.0.9"}).get_json()["body"]
    second = post(client, "/api/tv/setup/tvs", {"name": "Den", "type": "roku", "host": "10.0.0.10"}).get_json()
    assert second["tv"]["id"] == "den-2"


def test_a_google_tv_keeps_its_adb_and_mac_choices(client, tv):
    added = post(client, "/api/tv/setup/tvs", {"name": "Lounge", "type": "googletv", "host": "10.0.0.5", "adb": True, "sleep": "screensaver", "mac": "aa-bb-cc-dd-ee-0f"}).get_json()
    assert added["tv"]["adb"] and added["tv"]["sleep"] == "screensaver" and added["tv"]["mac"] == "AA:BB:CC:DD:EE:0F"
    assert tv.devices["lounge"].cfg["adb"] is True


def test_tvs_from_the_device_file_are_listed_but_not_editable(client, tmp_path):
    folder = tmp_path / "tv"
    folder.mkdir()
    (folder / "devices.json").write_text(json.dumps({"devices": [{"id": "hand", "name": "Hand written", "type": "roku", "host": "10.0.0.2"}]}))
    rt = runtime.configure(folder)
    try:
        listed = client.get("/api/tv/setup/tvs").get_json()["tvs"]
        assert listed[0]["managed"] is False and listed[0]["source"] == "devices.json"
        assert "devices.json" in post(client, "/api/tv/setup/tvs/hand", {"type": "roku", "host": "10.0.0.2"}, "put").get_json()["body"]
        assert "devices.json" in post(client, "/api/tv/setup/tvs/hand", method="delete").get_json()["body"]
        assert rt.config_error is None
    finally:
        runtime._runtime = None


def test_removing_a_tv_forgets_its_app_choices(client, tv):
    post(client, "/api/tv/setup/tvs", {"name": "Den", "type": "roku", "host": "10.0.0.9"})
    post(client, "/api/tv/prefs/apps/den", {"apps": [{"id": "12", "name": "Netflix"}]}, "put")
    post(client, "/api/tv/setup/tvs/den", method="delete")
    assert tv.prefs["apps"] == {} and tv.prefs["quick"] == {}


def test_a_broken_device_file_is_reported_but_saved_tvs_still_load(client, tmp_path):
    folder = tmp_path / "tv"
    folder.mkdir()
    (folder / "devices.json").write_text("{not json")
    (folder / "prefs.json").write_text(json.dumps({"devices": [{"id": "den", "name": "Den", "type": "roku", "host": "10.0.0.9"}]}))
    rt = runtime.configure(folder)
    try:
        assert "Could not read" in rt.config_error and list(rt.devices) == ["den"]
    finally:
        runtime._runtime = None


# ---------------------------------------------------------------- the apps a remote shows
@pytest.fixture
def den(client, tv):
    post(client, "/api/tv/setup/tvs", {"name": "Den", "type": "roku", "host": "10.0.0.9"})
    return "den"


def test_the_apps_a_remote_shows_are_saved_per_tv_and_can_be_reset(client, tv, den, tmp_path):
    apps = [{"id": "12", "name": "Netflix"}, {"id": "2285", "name": "Jellyfin"}]
    assert post(client, f"/api/tv/prefs/apps/{den}", {"apps": apps}, "put").get_json()["ok"]
    assert client.get("/api/tv/prefs").get_json()["prefs"]["apps"][den] == apps
    assert runtime.configure(tmp_path / "tv").prefs["apps"][den] == apps         # saved on disk
    assert post(client, f"/api/tv/prefs/apps/{den}", method="delete").get_json()["ok"]
    assert den not in client.get("/api/tv/prefs").get_json()["prefs"]["apps"]


def test_app_choices_are_checked(client, tv, den):
    assert "unknown device" in post(client, "/api/tv/prefs/apps/nope", {"apps": []}, "put").get_json()["body"]
    assert "bad app id" in post(client, f"/api/tv/prefs/apps/{den}", {"apps": [{"id": "../x"}]}, "put").get_json()["body"]
    assert post(client, f"/api/tv/prefs/apps/{den}", {"apps": "all"}, "put").get_json()["ok"] is False
    assert len(post(client, f"/api/tv/prefs/apps/{den}", {"apps": [{"id": "a", "name": "A"}, {"id": "a", "name": "again"}]}, "put").get_json()["apps"]) == 1


def test_quick_launch_holds_four_apps(client, tv, den):
    five = [{"id": f"app{i}", "name": f"App {i}"} for i in range(5)]
    assert post(client, f"/api/tv/prefs/quick/{den}", {"apps": five}, "put").get_json()["ok"] is False
    assert post(client, f"/api/tv/prefs/quick/{den}", {"apps": five[:4]}, "put").get_json()["ok"]


def test_a_damaged_prefs_file_loses_only_the_damaged_part(tmp_path):
    folder = tmp_path / "tv"
    folder.mkdir()
    (folder / "prefs.json").write_text(json.dumps({"apps": {"a": [{"id": "ok", "name": "Ok"}], "b": "junk"}, "quick": {"a": [{"id": "../"}]}}))
    rt = runtime.Runtime(folder)
    assert rt.prefs["apps"] == {"a": [{"id": "ok", "name": "Ok"}]} and rt.prefs["quick"] == {}
    (folder / "prefs.json").write_text("not json at all")
    assert runtime.Runtime(folder).prefs["devices"] == []


# ---------------------------------------------------------------- notices and Jellyfin
def test_notices_point_at_pages_that_exist(client, tv):
    notices = client.get("/api/tv/notices").get_json()["notices"]
    assert len({n["id"] for n in notices}) == len(notices) >= 2
    for n in notices:
        assert client.get(n["href"]).status_code == 200


def test_without_jellyfin_settings_the_tv_calls_say_what_to_do(client, tv, den):
    answer = client.get("/api/tv/jf/sessions").get_json()
    assert answer["ok"] is False and "Settings" in answer["body"]


def test_the_log_is_served_and_cleared(client, tv):
    tv.logbook.add("system", None, "hello", response="x")
    got = client.get("/api/tv/log").get_json()
    assert any(e["action"] == "hello" for e in got["entries"]) and got["build"]
    assert post(client, "/api/tv/setup/log", method="delete").get_json()["ok"]
    assert client.get("/api/tv/log").get_json()["entries"] == []


# ---------------------------------------------------------------- finding TVs
def test_only_private_ranges_of_a_sane_size_can_be_scanned():
    assert [str(n) for n in discovery.parse_ranges(["192.168.1.0/24", "10.0.0.7"])] == ["192.168.1.0/24", "10.0.0.0/24"]
    for bad, why in (("8.8.8.0/24", "private"), ("10.0.0.0/8", "too large"), ("banana", "not an address range")):
        with pytest.raises(discovery.DiscoveryError, match=why):
            discovery.parse_ranges([bad])


def test_the_range_to_scan_is_suggested_from_the_tvs_already_known():
    assert discovery.suggest_ranges(["192.168.128.156", "192.168.128.26", "not an address"]) == ["192.168.128.0/24"]


def serve_port():
    """Listens on a free local port. -> (listener, port)"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)

    def accept():
        while True:
            try:
                listener.accept()[0].close()
            except OSError:
                return
    threading.Thread(target=accept, daemon=True).start()
    return listener, listener.getsockname()[1]


def test_the_range_scan_finds_a_google_tv_by_its_control_port_and_marks_known_ones(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "_mdns", lambda timeout: ({}, None))
    monkeypatch.setattr(discovery, "_ssdp", lambda timeout, target=None: ({}, None))
    monkeypatch.setattr(discovery, "android_identity", lambda host, cert_dir: {"name": "Lounge TV", "mac": "AA:BB:CC:00:11:22"})
    listener, port = serve_port()
    monkeypatch.setattr(discovery, "ANDROID_PORT", port)      # stands in for the TV's control port 6466
    try:
        found = discovery.discover(["127.0.0.1/32"], known={}, cert_dir=str(tmp_path), ports=(port,))["found"]
        assert [(f["type"], f["name"], f["via"], f["added"]) for f in found] == [("googletv", "Lounge TV", ["range scan"], None)]
        assert discovery.discover(["127.0.0.1/32"], known={"127.0.0.1": "lounge"}, cert_dir=str(tmp_path), ports=(port,))["found"][0]["added"] == "lounge"
    finally:
        listener.close()


def test_a_roku_is_found_by_ssdp_and_named_from_its_own_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "_mdns", lambda timeout: ({}, None))
    monkeypatch.setattr(discovery, "_ssdp", lambda timeout, target=None: ({"127.0.0.1": {"type": "roku", "via": "SSDP"}}, None))
    xml = ('<?xml version="1.0"?><device-info><user-device-name>Bedroom</user-device-name><model-name>50S435</model-name>'
           "<wifi-mac>aa:bb:cc:dd:ee:ff</wifi-mac><is-tv>true</is-tv></device-info>")

    class Answer:
        text = xml
    monkeypatch.setattr(discovery.requests, "get", lambda url, timeout: Answer())
    found = discovery.discover([], cert_dir=str(tmp_path))["found"]
    assert found[0]["name"] == "Bedroom" and found[0]["model"] == "50S435" and found[0]["mac"] == "AA:BB:CC:DD:EE:FF"


def test_ssdp_reads_the_address_a_roku_answers_with():
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))

    def answer():
        data, addr = responder.recvfrom(1024)
        responder.sendto(b"HTTP/1.1 200 OK\r\nST: roku:ecp\r\nLOCATION: http://192.168.1.44:8060/\r\n\r\n", addr)
    threading.Thread(target=answer, daemon=True).start()
    found, note = discovery._ssdp(1.0, target=responder.getsockname())
    responder.close()
    assert found == {"192.168.1.44": {"type": "roku", "via": "SSDP"}} and note is None


def test_scanning_goes_through_the_page_and_a_bad_range_is_explained(client, tv, monkeypatch):
    monkeypatch.setattr(discovery, "_mdns", lambda timeout: ({}, None))
    monkeypatch.setattr(discovery, "_ssdp", lambda timeout, target=None: ({}, None))
    answer = post(client, "/api/tv/setup/discovery/scan", {"ranges": ["8.8.8.0/24"]}).get_json()
    assert answer["ok"] is False and "private" in answer["body"]
    empty = post(client, "/api/tv/setup/discovery/scan", {"ranges": []}).get_json()
    assert empty["ok"] and empty["found"] == [] and "No TVs were found" in " ".join(empty["notes"])


# ---------------------------------------------------------------- Google TV: leave the screensaver before opening an app
class FakeTV(GoogleTVDriver):
    """A Google TV that records what it is told, with a screensaver we can switch on."""
    def __init__(self, dreaming):
        super().__init__({"id": "gtv", "name": "Gtv", "type": "googletv", "host": "127.0.0.1"}, LogBook(), {"cert_dir": "data/tv/certs"})
        self.dreaming, self.events = dreaming, []

    def status(self, quiet=False):
        return {"online": True, "power": "dreaming" if self.dreaming else "on", "app": "com.google.android.apps.tv.dreamx" if self.dreaming else "x"}

    def send_key(self, native, count=1):
        self.events.append(("key", native))
        self.dreaming = False

    def _launch_link(self, link, package):
        self.events.append(("launch", package))
        return True, ""


def test_an_app_opened_from_the_screensaver_goes_home_waits_and_then_launches(monkeypatch):
    sleeps = []
    monkeypatch.setattr("synchub.tv.devices.time.sleep", sleeps.append)
    tvd = FakeTV(dreaming=True)
    tvd.launch("com.netflix.ninja")
    assert tvd.events == [("key", "KEYCODE_HOME"), ("launch", "com.netflix.ninja")]
    assert sleeps == [2.0]                                              # the wait between Home and the launch


def test_an_app_opened_while_awake_is_launched_straight_away(monkeypatch):
    monkeypatch.setattr("synchub.tv.devices.time.sleep", lambda s: pytest.fail("no wait expected"))
    tvd = FakeTV(dreaming=False)
    tvd.launch("com.netflix.ninja")
    assert tvd.events == [("launch", "com.netflix.ninja")]


def test_a_tv_that_cannot_be_reached_is_just_launched_to(monkeypatch):
    tvd = FakeTV(dreaming=True)
    tvd.status = lambda quiet=False: {"online": False, "power": None, "app": None}
    tvd.launch("com.netflix.ninja")
    assert tvd.events == [("launch", "com.netflix.ninja")]


# ---------------------------------------------------------------- Roku: go Home before opening another title
class FakeRoku(RokuDriver):
    def __init__(self, app_on_screen):
        super().__init__({"id": "r", "name": "Roku", "type": "roku", "host": "127.0.0.1"}, LogBook(), {})
        self.app, self.events = app_on_screen, []

    def send_key(self, native, count=1):
        self.events.append(("key", native))
        if native == "Home":
            self.app = "home"

    def status(self, quiet=False):
        return {"online": True, "power": "on", "app": self.app}

    def launch(self, app_id, params=None):
        self.events.append(("launch", app_id, params))
        self.app = app_id
        return "launched"


def test_a_roku_already_in_jellyfin_goes_home_before_the_new_title_is_opened(monkeypatch):
    monkeypatch.setattr(roku.time, "sleep", lambda s: None)
    r = FakeRoku("2285")
    r.deeplink("2285", "movie", "m2", running=True)
    assert r.events == [("key", "Home"), ("launch", "2285", {"contentId": "m2", "mediaType": "movie"})]


def test_a_roku_that_is_somewhere_else_is_not_sent_home(monkeypatch):
    monkeypatch.setattr(roku.time, "sleep", lambda s: None)
    r = FakeRoku("home")
    r.deeplink("2285", "season", "s1", series_id="show1", running=False)
    assert r.events == [("launch", "2285", {"contentId": "s1", "mediaType": "season", "seriesID": "show1"})]


def test_a_roku_that_does_not_leave_the_app_is_still_sent_the_link(monkeypatch):
    clock = iter(range(1000))
    monkeypatch.setattr(roku.time, "sleep", lambda s: None)
    monkeypatch.setattr(roku.time, "time", lambda: next(clock))        # time moves on so the wait ends
    r = FakeRoku("2285")
    r.send_key = lambda native, count=1: r.events.append(("key", native))   # Home does nothing
    r.deeplink("2285", "movie", "m2", running=True)
    assert [e[0] for e in r.events] == ["key", "launch"]
    assert any("still on screen" in e["response"] for e in r.logbook.dump())
