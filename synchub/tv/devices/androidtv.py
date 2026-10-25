"""Google TV / Android TV over ADB (needs the `adb` binary and Network debugging on the TV)."""
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import Driver, DeviceError, register

DREAM_APPS = {"com.google.android.apps.tv.dreamx"}   # the Google TV screensaver ("Ambient mode")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
KEY_RE = re.compile(r"^(KEYCODE_[A-Z0-9_]+|\d{1,3})$")
TEXT_RE = re.compile(r"^[A-Za-z0-9 ._@-]{1,100}$")

FRIENDLY = {
    "org.jellyfin.androidtv": "Jellyfin",
    "com.netflix.ninja": "Netflix",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvunplugged": "YouTube TV",
    "com.disney.disneyplus": "Disney+",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.hulu.livingroomplus": "Hulu",
    "com.wbd.stream": "Max",
    "com.plexapp.android": "Plex",
    "com.spotify.tv.android": "Spotify",
    "com.apple.atve.androidtv.appletv": "Apple TV",
    "com.peacocktv.peacockandroid": "Peacock",
    "com.cbs.ott": "Paramount+",
    "com.google.android.apps.tv.launcherx": "Home",
    "com.google.android.tvlauncher": "Home",
    "com.android.vending": "Play Store",
    "com.google.android.videos": "Google TV",
    "com.google.android.youtube.tvmusic": "YouTube Music",
    "com.tubitv": "Tubi",
    "com.att.tv": "AT&T TV",
    "com.tcl.tv": "TCL Live TV",
    "com.tcl.tv.plus": "TCL TV+",
    "com.tcl.suspension": "TV menu",
    "com.google.android.apps.tv.dreamx": "Screensaver",
}
# Vendor and platform packages that clutter the launcher grid (hidden unless asked for).
SYSTEM_PREFIXES = ("com.tcl.", "com.android.", "com.google.android.tvlauncher", "com.google.android.apps.tv.",
                   "com.google.android.play.games")
GENERIC_WORDS = {"com", "org", "net", "android", "androidtv", "tv", "app", "apps", "ott", "main", "livingroom",
                 "ninja", "stream", "google", "mobile", "client"}


def friendly_name(package):
    if package in FRIENDLY:
        return FRIENDLY[package]
    parts = [p for p in package.split(".") if p.lower() not in GENERIC_WORDS]
    return " ".join(w.capitalize() for w in (parts[-2:] or package.split(".")[-1:]))


def is_system(package):
    return package.startswith(SYSTEM_PREFIXES) and package != "com.tcl.tv.plus"   # TCL TV+ is a streaming app


PLAYBACK_STATES = {1: "stopped", 2: "paused", 3: "playing", 4: "fast-forwarding", 5: "rewinding", 6: "buffering"}
MAC_RE = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", re.I)


def parse_media_session(text, prefer_package=None):
    """Best-effort read of `dumpsys media_session`: -> {state, title, package, positionMs} or None.

    Picks the active session that is playing or buffering, then paused, preferring the
    focused app. Output differs between Android versions, so unknown shapes give None.
    """
    sessions, cur = [], None
    for line in text.splitlines():
        if re.match(r"^\s*\S.*\(userId=\d+\)\s*$", line):
            cur = {}
            sessions.append(cur)
        elif cur is not None:
            m = re.search(r"\bpackage=(\S+)", line)
            if m and "package" not in cur:
                cur["package"] = m.group(1)
            m = re.match(r"^\s*active=(true|false)", line)
            if m:
                cur["active"] = m.group(1) == "true"
            m = re.search(r"state=PlaybackState \{state=(\d+), position=(-?\d+)", line)
            if m:
                cur["state"], cur["position"] = int(m.group(1)), int(m.group(2))
            m = re.search(r"metadata:.*?description=(.*)$", line)
            if m:
                parts = [p.strip() for p in m.group(1).split(", ") if p.strip() and p.strip() != "null"]
                cur["title"] = " - ".join(parts[:2]) or None
    live = [s for s in sessions if s.get("active") and s.get("state") in PLAYBACK_STATES]
    if not live:
        return None
    rank = lambda s: ({3: 0, 6: 0, 4: 0, 5: 0, 2: 1, 1: 2}[s["state"]], 0 if s.get("package") == prefer_package else 1)
    best = min(live, key=rank)
    if best["state"] == 1:
        return None
    return {"state": PLAYBACK_STATES[best["state"]], "title": best.get("title"), "package": best.get("package"),
            "positionMs": best.get("position"), "source": "media session"}


@register
class AndroidTVDriver(Driver):
    type = "androidtv"
    log_kind = "adb"
    label = "Google TV / Android TV (ADB)"
    capabilities = frozenset({"keys", "apps", "text", "screenshot", "power_on", "power_off", "launch", "screensaver", "diagnose"})
    keymap = {
        "home": "KEYCODE_HOME", "back": "KEYCODE_BACK",
        "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN", "left": "KEYCODE_DPAD_LEFT",
        "right": "KEYCODE_DPAD_RIGHT", "ok": "KEYCODE_DPAD_CENTER",
        "options": "KEYCODE_MENU", "info": "KEYCODE_INFO", "search": "KEYCODE_SEARCH",
        "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "stop": "KEYCODE_MEDIA_STOP",
        "rewind": "KEYCODE_MEDIA_REWIND", "ff": "KEYCODE_MEDIA_FAST_FORWARD",
        "prev": "KEYCODE_MEDIA_PREVIOUS", "next": "KEYCODE_MEDIA_NEXT", "replay": None,
        "vol_up": "KEYCODE_VOLUME_UP", "vol_down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE",
        "ch_up": "KEYCODE_CHANNEL_UP", "ch_down": "KEYCODE_CHANNEL_DOWN",
        "backspace": "KEYCODE_DEL", "enter": "KEYCODE_ENTER",
    }
    extra_keys = (
        ("Settings", "KEYCODE_SETTINGS"), ("Recent apps", "KEYCODE_APP_SWITCH"), ("Guide", "KEYCODE_GUIDE"),
        ("TV input", "KEYCODE_TV_INPUT"), ("Power toggle", "KEYCODE_POWER"),
        ("Wake", "KEYCODE_WAKEUP"), ("Sleep", "KEYCODE_SLEEP"),
    )
    default_session_rule = {"client": "Android TV"}
    default_wake = ("adb", "wol", "cast")

    def __init__(self, cfg, logbook, options=None):
        super().__init__(cfg, logbook, options)
        self.port = int(cfg.get("port", 5555))
        self.target = f"{self.host}:{self.port}"
        self.adb_path = self.options.get("adb", "adb")
        self.probe_ports = {self.port: "ADB", 6466: "Remote protocol", 6467: "Remote pairing", 8009: "Cast", 8008: "Cast HTTP"}
        self._media = (0.0, None)
        self._status_lock = threading.Lock()
        self._last_status = {"online": False, "power": None, "app": None, "error": "checking…"}

    # --- adb plumbing -------------------------------------------------------------------
    def _adb(self, *args, action=None, quiet=False, timeout=15, raw=False, scoped=True, _retry=True):
        cmd = [self.adb_path] + (["-s", self.target] if scoped else []) + list(args)
        shown = "adb " + " ".join(cmd[1:])
        t0 = time.perf_counter()
        env = None
        if self.options.get("adb_home"):
            Path(self.options["adb_home"]).mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "HOME": self.options["adb_home"]}      # adb looks for (and makes) its key under $HOME/.android
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
        except FileNotFoundError:
            msg = f'adb not found ("{self.adb_path}"). Install it (sudo apt install adb) or pass --adb PATH.'
            self._fail(action, shown, None, msg, quiet)
            raise DeviceError(msg)
        except subprocess.TimeoutExpired:
            self._fail(action, shown, None, "adb timed out", quiet, t0)
            raise DeviceError("adb timed out")
        ms = (time.perf_counter() - t0) * 1000
        text = (p.stdout + p.stderr).decode(errors="replace").strip()
        if p.returncode != 0:
            if scoped and _retry and re.search(r"not found|offline|no devices", text):
                self._adb("connect", self.target, action="reconnect", scoped=False, quiet=quiet, _retry=False, timeout=6)
                return self._adb(*args, action=action, quiet=quiet, timeout=timeout, raw=raw, _retry=False)
            if "unauthorized" in text:
                text += " - accept the 'Allow USB debugging' prompt on the TV"
            self._fail(action, shown, p.returncode, text, quiet, t0)
            raise DeviceError(text or f"adb exited with {p.returncode}")
        if not quiet:
            self.log(action or args[0], shown, p.returncode, True, ms, "<binary>" if raw else text)
        return p.stdout if raw else text

    def _fail(self, action, shown, code, text, quiet, t0=None):
        if not quiet:
            ms = None if t0 is None else (time.perf_counter() - t0) * 1000
            self.log(action or "adb", shown, code, False, ms, text)

    # --- Driver interface ---------------------------------------------------------------
    def connect(self):
        out = self._adb("connect", self.target, action="connect", scoped=False)
        if not out.startswith(("connected", "already connected")):
            raise DeviceError(out)
        return out

    def status(self, quiet=False):
        """Polls are single-flight: a hung adbd must not stack up one blocked adb process per poll."""
        if quiet:
            if not self._status_lock.acquire(blocking=False):
                return self._last_status
            try:
                self._last_status = self._read_state(quiet=True)
                return self._last_status
            finally:
                self._status_lock.release()
        with self._status_lock:
            self._last_status = self._read_state(quiet=False)
            return self._last_status

    def _read_state(self, quiet, timeout=4):
        # `adb get-state` only reports the adb server's cached view, so test the port directly first.
        try:
            socket.create_connection((self.host, self.port), timeout=1.5).close()
        except OSError as e:
            return {"online": False, "power": None, "app": None, "reachable": False,
                    "error": f"{self.target} is not reachable ({e.strerror or type(e).__name__}): the TV is off, in deep "
                             "standby, or network debugging is disabled"}
        try:
            out = self._adb("shell", "dumpsys power | grep -m1 mWakefulness=; dumpsys window | grep -m1 mCurrentFocus",
                            action="status", quiet=quiet, timeout=timeout)
        except DeviceError as e:
            if "timed out" in str(e):
                return {"online": False, "power": "standby", "app": None, "reachable": True, "unresponsive": True,
                        "error": "Connected, but ADB is not answering: the TV is most likely asleep"}
            return {"online": False, "power": None, "app": None, "error": str(e)}
        wake = re.search(r"mWakefulness=(\w+)", out)
        focus = re.search(r"mCurrentFocus=.*?([A-Za-z][\w]*(?:\.\w+)+)/", out)
        wakefulness = wake.group(1) if wake else None
        pkg = focus.group(1) if focus else None
        power = {"Awake": "on", "Asleep": "off", "Dozing": "off", "Dreaming": "dreaming"}.get(wakefulness, wakefulness)
        result = {"online": True, "power": power, "app": pkg, "appName": FRIENDLY.get(pkg, pkg)}
        if power == "on":
            result["media"] = self.media(pkg)
        return result

    def media(self, prefer_package=None):
        """What is playing, from the Android media session (cached for a few seconds)."""
        now = time.time()
        if now - self._media[0] < 8:
            return self._media[1]
        try:
            text = self._adb("shell", "dumpsys", "media_session", action="media session", quiet=True, timeout=5)
            value = parse_media_session(text, prefer_package)
        except DeviceError:
            value = None
        self._media = (now, value)
        return value

    def detect_mac(self):
        for iface in ("wlan0", "eth0"):
            try:
                out = self._adb("shell", "cat", f"/sys/class/net/{iface}/address", action=f"read {iface} MAC")
            except DeviceError:
                continue
            m = MAC_RE.search(out)
            if m and m.group(1) != "00:00:00:00:00:00":
                self.mac = m.group(1).upper()
                self.log("detect mac", "", None, True, None, f"{iface}: {self.mac}")
                return self.mac
        raise DeviceError("Could not read a MAC address over ADB")

    def _in_screensaver(self, status):
        return status.get("power") == "dreaming" or status.get("app") in DREAM_APPS

    def valid_native_key(self, key):
        return bool(KEY_RE.match(key))

    def send_key(self, native, count=1):
        # `input keyevent` takes several keycodes, so a burst costs one slow adb call instead of many
        self._adb("shell", "input", "keyevent", *([native] * count), action=f"key {native}" + (f" x{count}" if count > 1 else ""))

    def power(self, on):
        """Wake, or put to standby. (Standby makes ADB unreachable on many TVs; see screensaver().)"""
        key = "KEYCODE_WAKEUP" if on else "KEYCODE_SLEEP"
        self._adb("shell", "input", "keyevent", key, action=f"key {key}", timeout=20 if on else 15)

    def _wake_adb(self):
        # A sleeping TV may take a long time to answer, so don't block on it: the command is delivered
        # whenever the network wakes, and the caller polls for the result.
        def go():
            try:
                self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", action="wake (adb)", timeout=90)
            except DeviceError:
                pass  # already logged
        threading.Thread(target=go, daemon=True, name=f"adb-wake-{self.id}").start()
        return "queued KEYCODE_WAKEUP over ADB (delivered when the TV's network wakes)"

    def screensaver(self):
        """Soft sleep: start the screensaver. The TV stays awake and reachable, and any key wakes it."""
        out = self._adb("shell", "am", "start", "-n", "com.android.systemui/.Somnambulator", action="screensaver", timeout=10)
        if re.search(r"Error|does not exist|Exception", out):
            raise DeviceError(f"The TV has no screensaver component: {out.splitlines()[-1] if out else 'unknown error'}")
        for _ in range(4):
            time.sleep(1)
            if self._read_state(quiet=True).get("power") == "dreaming":
                return "screensaver running"
        raise DeviceError("The command ran, but the TV is not showing a screensaver. Turn one on in Settings > System > "
                          "Ambient mode (or Screen saver), then run Diagnose to see the current settings.")

    def diagnose(self):
        sections = [
            ("device", "getprop ro.product.manufacturer; getprop ro.product.model; getprop ro.build.version.release"),
            ("power", "dumpsys power | grep -E 'mWakefulness=|mWakeLockSummary|Display Power: state='"),
            ("screensaver", "for k in screensaver_enabled screensaver_components screensaver_activate_on_sleep "
                            "screensaver_activate_on_dock; do echo $k=$(settings get secure $k); done"),
            ("screen timeout", "echo screen_off_timeout=$(settings get system screen_off_timeout)"),
            ("network", "for i in wlan0 eth0; do echo $i mac=$(cat /sys/class/net/$i/address 2>/dev/null) "
                        "$(ip -o -4 addr show $i 2>/dev/null | awk '{print $4}'); done"),
        ]
        parts = []
        for title, command in sections:
            try:
                out = self._adb("shell", command, action=f"diagnose {title}", quiet=True, timeout=8)
            except DeviceError as e:
                out = f"(failed: {e})"
            parts.append(f"[{title}]\n{out}")
        text = "\n".join(parts)
        self.log("diagnose", "", None, True, None, text)
        return text

    def apps(self):
        packages = []
        try:
            out = self._adb("shell", "cmd", "package", "query-activities", "--brief",
                            "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LEANBACK_LAUNCHER",
                            action="list apps")
            packages = sorted({m.group(1) for l in out.splitlines()
                               if (m := re.match(r"^\s*([A-Za-z][\w.]*)/\S+\s*$", l))})
        except DeviceError:
            pass
        if not packages:  # older Android: third-party apps only
            out = self._adb("shell", "pm", "list", "packages", "-3", action="list apps (fallback)")
            packages = sorted(l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:"))
        apps = [{"id": p, "name": friendly_name(p), "type": "app", "system": is_system(p)} for p in packages]
        return sorted(apps, key=lambda a: (a["system"], a["id"] not in FRIENDLY, a["name"].lower()))

    def launch(self, app_id, params=None):
        if not PACKAGE_RE.match(app_id):
            raise DeviceError("bad package name")
        self.leave_screensaver()
        for category in ("android.intent.category.LEANBACK_LAUNCHER", "android.intent.category.LAUNCHER"):
            out = self._adb("shell", "monkey", "-p", app_id, "-c", category, "1", action=f"launch {app_id}")
            if "No activities found" not in out and "monkey aborted" not in out:
                return out
        raise DeviceError(f"No launchable activity found for {app_id}")

    def text(self, text):
        if not TEXT_RE.match(text):
            raise DeviceError("text may only use letters, digits, space . _ @ -")
        self._adb("shell", "input", "text", text.replace(" ", "%s"), action="text")

    def screenshot(self):
        return self._adb("exec-out", "screencap", "-p", action="screenshot", timeout=20, raw=True)

    def jellyfin_app(self):
        return self.jellyfin.get("app") or "org.jellyfin.androidtv"
