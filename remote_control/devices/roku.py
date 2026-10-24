"""Roku / Roku TV over ECP, the External Control Protocol (plain HTTP on port 8060).

The Roku must allow it: Settings > System > Advanced system settings >
Control by mobile apps > Network access = Default or Permissive.
"""
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from . import Driver, DeviceError, register

PLAY_ON_ROKU = "15985"   # the Roku system channel that plays a video from a URL (what the Roku phone app casts through)
SECRET = re.compile(r"(api_key(?:=|%3D))[^&%]+", re.I)
KEY_RE = re.compile(r"^[A-Za-z0-9_%]{1,40}$")
APP_RE = re.compile(r"^[\w.\-]+$")
TEXT_RE = re.compile(r"^[\x20-\x7e]{1,100}$")


@register
class RokuDriver(Driver):
    type = "roku"
    log_kind = "roku"
    label = "Roku / Roku TV (ECP)"
    capabilities = frozenset({"keys", "apps", "text", "power_on", "power_off", "launch", "icons", "url_play"})
    keymap = {
        "home": "Home", "back": "Back",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right", "ok": "Select",
        "options": "Info", "info": None, "search": "Search",
        "play_pause": "Play", "stop": None, "rewind": "Rev", "ff": "Fwd",
        "prev": None, "next": None, "replay": "InstantReplay",
        "vol_up": "VolumeUp", "vol_down": "VolumeDown", "mute": "VolumeMute",
        "ch_up": "ChannelUp", "ch_down": "ChannelDown",
        "backspace": "Backspace", "enter": "Enter",
    }
    extra_keys = (
        ("Input: Tuner", "InputTuner"), ("Input: HDMI 1", "InputHDMI1"), ("Input: HDMI 2", "InputHDMI2"),
        ("Input: HDMI 3", "InputHDMI3"), ("Input: HDMI 4", "InputHDMI4"), ("Input: AV", "InputAV1"),
        ("Power toggle", "Power"), ("Find remote", "FindRemote"),
    )
    default_session_rule = {"client": "Roku"}
    default_wake = ("key", "wol")
    probe_ports = {8060: "ECP"}

    def __init__(self, cfg, logbook, options=None):
        super().__init__(cfg, logbook, options)
        self.base = f"http://{self.host}:{int(cfg.get('port', 8060))}"
        self._cast_title, self._cast_at = None, 0.0

    # --- ECP plumbing -------------------------------------------------------------------
    def _ecp(self, method, path, action=None, quiet=False, params=None, timeout=5):
        t0 = time.perf_counter()
        shown = f"{method} {path}" + (f" params={params}" if params else "")
        try:
            r = requests.request(method, f"{self.base}{path}", params=params, timeout=timeout)
        except requests.RequestException as e:
            if not quiet:
                self.log(action or path, shown, 0, False, (time.perf_counter() - t0) * 1000, str(e))
            raise DeviceError(f"Roku unreachable: {e}")
        ms = (time.perf_counter() - t0) * 1000
        if not r.ok:
            hint = ""
            if r.status_code == 403:
                hint = (" - enable Settings > System > Advanced system settings > "
                        "Control by mobile apps > Network access (Default or Permissive)")
            if not quiet:
                self.log(action or path, shown, r.status_code, False, ms, r.text[:500] + hint)
            raise DeviceError(f"Roku returned HTTP {r.status_code}{hint}")
        if not quiet:
            is_xml = r.text.lstrip().startswith("<?xml")
            self.log(action or path, shown, r.status_code, True, ms, r.text[:1500] if is_xml else "")
        return r

    @staticmethod
    def _xml(text):
        try:
            return ET.fromstring(text)
        except ET.ParseError as e:
            raise DeviceError(f"unexpected response from Roku: {e}")

    # --- Driver interface ---------------------------------------------------------------
    def status(self, quiet=False):
        try:
            info = self._xml(self._ecp("GET", "/query/device-info", action="device-info", quiet=quiet).text)
            active = self._xml(self._ecp("GET", "/query/active-app", action="active-app", quiet=quiet).text)
        except DeviceError as e:
            return {"online": False, "power": None, "app": None, "error": str(e)}
        mode = info.findtext("power-mode")
        app = active.find("app")
        if app is None and active.find("screensaver") is not None:
            app = active.find("screensaver")
            app_id, name = "screensaver", f"Screensaver ({app.text or ''})".strip()
        elif app is not None:
            app_id, name = app.get("id") or "home", (app.text or "").strip()
        else:
            app_id = name = None
        if not self.mac:
            self.mac = info.findtext("wifi-mac") or info.findtext("ethernet-mac")
        power = {"PowerOn": "on", "Headless": "on", "Ready": "standby", "DisplayOff": "standby"}.get(mode, mode)
        out = {
            "online": True, "power": power, "app": app_id, "appName": name,
            "detail": {k: info.findtext(k) for k in ("friendly-device-name", "model-name", "software-version", "is-tv", "power-mode")},
        }
        if power == "on" and app_id not in (None, "home", "screensaver"):
            out["media"] = self._media(quiet, app_id)
        return out

    def _media(self, quiet, app_id=None):
        """Playback state from the active channel (apps vary in how much they report)."""
        try:
            player = self._xml(self._ecp("GET", "/query/media-player", action="media-player", quiet=True).text)
        except DeviceError:
            return None
        state = player.get("state")
        if state in (None, "none", "close"):
            return None
        ms = lambda tag: int(m.group(1)) if (m := re.match(r"\s*(\d+)", player.findtext(tag) or "")) else None
        # Roku does not report a title; for something we cast ourselves we know it
        ours = self._cast_title if (app_id == PLAY_ON_ROKU or time.time() - self._cast_at < 7200) else None
        return {"state": {"play": "playing", "pause": "paused", "buffer": "buffering", "startup": "starting"}.get(state, state),
                "title": ours, "positionMs": ms("position"), "durationMs": ms("duration"), "source": "Roku media player"}

    def play_url(self, url, title, fmt="mp4"):
        """Start a video from a URL with the "Play on Roku" channel (no Jellyfin app or session needed)."""
        query = f"t=v&u={quote(url, safe='')}&k=(null)&videoName={quote(title or 'Video', safe='')}&videoFormat={fmt}"
        shown = f"POST /input/{PLAY_ON_ROKU}?" + SECRET.sub(r"\1***", query)   # the API key must never reach the log
        t0 = time.perf_counter()
        try:
            r = requests.post(f"{self.base}/input/{PLAY_ON_ROKU}?{query}", timeout=25)
        except requests.RequestException as e:
            self.log("play on roku", shown, 0, False, (time.perf_counter() - t0) * 1000, SECRET.sub(r"\1***", str(e)))
            raise DeviceError(f"Roku unreachable: {SECRET.sub(r'api_key=***', str(e))}")
        ok = r.ok
        self.log("play on roku", shown, r.status_code, ok, (time.perf_counter() - t0) * 1000, r.text[:300])
        if not ok:
            raise DeviceError(f"The Roku refused the stream (HTTP {r.status_code})")
        self._cast_title, self._cast_at = title, time.time()
        return "stream sent to Play on Roku"

    def detect_mac(self):
        self.status(quiet=True)
        if not self.mac:
            raise DeviceError("The Roku did not report a MAC address")
        self.log("detect mac", "", None, True, None, self.mac)
        return self.mac

    def icon(self, app_id):
        if not APP_RE.match(app_id):
            raise DeviceError("bad app id")
        r = self._ecp("GET", f"/query/icon/{app_id}", quiet=True)
        return r.content, r.headers.get("Content-Type", "image/png")

    def valid_native_key(self, key):
        return bool(KEY_RE.match(key))

    def send_key(self, native, count=1):
        for _ in range(count - 1):
            self._ecp("POST", f"/keypress/{native}", quiet=True)
            time.sleep(0.04)
        self._ecp("POST", f"/keypress/{native}", action=f"key {native}" + (f" x{count}" if count > 1 else ""))

    def power(self, on):
        self.send_key("PowerOn" if on else "PowerOff")

    def apps(self):
        root = self._xml(self._ecp("GET", "/query/apps", action="list apps").text)
        return [{"id": a.get("id"), "name": (a.text or a.get("id") or "").strip(), "type": a.get("type") or "appl"}
                for a in root.findall("app")]

    def launch(self, app_id, params=None):
        if not APP_RE.match(app_id):
            raise DeviceError("bad app id")
        clean = {k: v for k, v in (params or {}).items() if k in ("contentId", "mediaType") and v}
        shown = f"POST /launch/{app_id}" + (f" params={clean}" if clean else "")
        t0 = time.perf_counter()
        ms = lambda: (time.perf_counter() - t0) * 1000
        try:
            # Roku answers a launch only once the channel is up, which can take well over the usual 5 s
            self._ecp("POST", f"/launch/{app_id}", quiet=True, params=clean or None, timeout=25)
            self.log(f"launch {app_id}", shown, 200, True, ms(), "")
            return "launched"
        except DeviceError as e:
            if "timed out" not in str(e).lower():
                self.log(f"launch {app_id}", shown, None, False, ms(), str(e))
                raise
        for _ in range(12):   # no answer in time: the channel may still have started, so look
            time.sleep(1)
            if self.status(quiet=True).get("app") == app_id:
                self.log(f"launch {app_id}", shown, None, True, ms(), "the Roku was slow to answer, but the app is now running")
                return "launched (the Roku was slow to answer)"
        self.log(f"launch {app_id}", shown, None, False, ms(), "no answer, and the app never came to the front")
        raise DeviceError(f"The Roku did not start app {app_id}")

    def text(self, text):
        if not TEXT_RE.match(text):
            raise DeviceError("text must be printable ASCII (max 100 chars)")
        t0 = time.perf_counter()
        for ch in text:
            self._ecp("POST", f"/keypress/Lit_{quote(ch, safe='')}", quiet=True)
            time.sleep(0.05)
        self.log("text", f"POST /keypress/Lit_<char> x{len(text)}", 200, True, (time.perf_counter() - t0) * 1000, text)

    def jellyfin_app(self):
        wanted = (self.jellyfin.get("app") or "Jellyfin").strip()
        if APP_RE.match(wanted) and wanted.isdigit():
            return wanted
        for a in self.apps():
            if wanted.lower() in a["name"].lower() and a["type"] != "tvin":
                return a["id"]
        raise DeviceError(f'No installed Roku app matching "{wanted}". Install the Jellyfin channel or set jellyfin.app.')
