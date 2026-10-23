"""Google TV / Android TV over ADB (needs the `adb` binary and Network debugging on the TV)."""
import re
import subprocess
import time

from . import Driver, DeviceError, register

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
}


@register
class AndroidTVDriver(Driver):
    type = "androidtv"
    log_kind = "adb"
    label = "Google TV / Android TV (ADB)"
    capabilities = frozenset({"keys", "apps", "text", "screenshot", "power_on", "power_off", "launch"})
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

    def __init__(self, cfg, logbook, options=None):
        super().__init__(cfg, logbook, options)
        self.target = f"{self.host}:{int(cfg.get('port', 5555))}"
        self.adb_path = self.options.get("adb", "adb")

    # --- adb plumbing -------------------------------------------------------------------
    def _adb(self, *args, action=None, quiet=False, timeout=15, raw=False, scoped=True, _retry=True):
        cmd = [self.adb_path] + (["-s", self.target] if scoped else []) + list(args)
        shown = "adb " + " ".join(cmd[1:])
        t0 = time.perf_counter()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
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
                self._adb("connect", self.target, action="reconnect", scoped=False, quiet=quiet, _retry=False)
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
        try:
            self._adb("get-state", action="get-state", quiet=quiet)
            out = self._adb(
                "shell", "dumpsys power | grep -m1 mWakefulness=; dumpsys window | grep -m1 mCurrentFocus",
                action="status", quiet=quiet)
        except DeviceError as e:
            return {"online": False, "power": None, "app": None, "error": str(e)}
        wake = re.search(r"mWakefulness=(\w+)", out)
        focus = re.search(r"mCurrentFocus=.*?([A-Za-z][\w]*(?:\.\w+)+)/", out)
        wakefulness = wake.group(1) if wake else None
        pkg = focus.group(1) if focus else None
        return {
            "online": True,
            "power": {"Awake": "on", "Asleep": "off", "Dozing": "off", "Dreaming": "dreaming"}.get(wakefulness, wakefulness),
            "app": pkg,
            "appName": FRIENDLY.get(pkg, pkg),
        }

    def valid_native_key(self, key):
        return bool(KEY_RE.match(key))

    def send_key(self, native):
        self._adb("shell", "input", "keyevent", native, action=f"key {native}")

    def power(self, on):
        self.send_key("KEYCODE_WAKEUP" if on else "KEYCODE_SLEEP")

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
        apps = [{"id": p, "name": FRIENDLY.get(p, p), "type": "app"} for p in packages]
        return sorted(apps, key=lambda a: a["name"].lower())

    def launch(self, app_id, params=None):
        if not PACKAGE_RE.match(app_id):
            raise DeviceError("bad package name")
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
