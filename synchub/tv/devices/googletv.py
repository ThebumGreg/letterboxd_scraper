"""Google TV / Android TV over the Android TV Remote protocol v2.

This is the protocol the Google TV phone app and Home Assistant use. It needs no developer mode, can
usually wake a TV that is in standby, and reports power, current app and volume. It cannot list installed
apps, take screenshots or read what is playing, so those come from a configured app list and, if you set
`"adb": true` on the device, from ADB as well.

Setup: `pip install androidtvremote2`, then press "Start pairing" in the UI and enter the code the TV shows.
"""
import asyncio
import collections
import concurrent.futures
import logging
import re
import threading
import time
from pathlib import Path

from . import Driver, DeviceError, register
from .androidtv import DREAM_APPS, FRIENDLY, PACKAGE_RE, AndroidTVDriver

LINK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://\S{1,300}$")
KEY_RE = re.compile(r"^(KEYCODE_)?[A-Z0-9_]{1,40}$|^\d{1,3}$")
TEXT_RE = re.compile(r"^[\x20-\x7e]{1,100}$")
CODE_RE = re.compile(r"^[0-9A-F]{6}$")
LAUNCHERS = {"com.google.android.apps.tv.launcherx", "com.google.android.tvlauncher"}
DEFAULT_APPS = [{"id": pkg, "name": name, "type": "app"} for pkg, name in FRIENDLY.items() if pkg not in LAUNCHERS]


class _DeviceErrors(logging.Handler):
    """The library only logs errors a TV sends back (such as a rejected app launch), so capture them."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.recent = collections.deque(maxlen=50)
        self.logbook = None

    def emit(self, record):
        message = record.getMessage()
        self.recent.append((time.time(), message))
        if self.logbook is not None and "error from the device" in message:
            self.logbook.add("atvremote", None, "device error", "", None, False, None, message)

    def since(self, t):
        return [m for ts, m in self.recent if ts >= t and "error from the device" in m]


_ERRORS = _DeviceErrors()
logging.getLogger("androidtvremote2").addHandler(_ERRORS)


class _Loop:
    """One background asyncio loop shared by every Remote-protocol device."""
    _lock = threading.Lock()
    _loop = None

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, daemon=True, name="atvremote-loop").start()
                cls._loop = loop
            return cls._loop

    @classmethod
    def run(cls, coro, timeout=15):
        future = asyncio.run_coroutine_threadsafe(coro, cls.get())
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise DeviceError("timed out talking to the TV")


@register
class GoogleTVDriver(Driver):
    type = "googletv"
    log_kind = "atvremote"
    label = "Google TV / Android TV (Remote protocol)"
    keymap = AndroidTVDriver.keymap
    extra_keys = AndroidTVDriver.extra_keys + (
        ("HDMI 1", "KEYCODE_TV_INPUT_HDMI_1"), ("HDMI 2", "KEYCODE_TV_INPUT_HDMI_2"),
        ("HDMI 3", "KEYCODE_TV_INPUT_HDMI_3"), ("HDMI 4", "KEYCODE_TV_INPUT_HDMI_4"),
    )
    default_session_rule = {"client": "Android TV"}
    default_wake = ("remote", "wol", "cast")

    def __init__(self, cfg, logbook, options=None):
        super().__init__(cfg, logbook, options)
        certdir = Path(self.options.get("cert_dir") or "data/tv/certs")
        certdir.mkdir(parents=True, exist_ok=True)
        # One client identity shared by every TV; each TV remembers it after pairing.
        self.certfile = str(certdir / "client-cert.pem")
        self.keyfile = str(certdir / "client-key.pem")
        self.api_port = int(cfg.get("port", 6466))
        self.pair_port = int(cfg.get("pair_port", 6467))
        caps = {"keys", "apps", "text", "launch", "power_on", "power_off", "pairing"}
        self.probe_ports = {self.api_port: "Remote protocol", self.pair_port: "Remote pairing", 8009: "Cast", 8008: "Cast HTTP"}
        self.adb = None
        if cfg.get("adb"):
            self.adb = AndroidTVDriver({**cfg, "type": "androidtv", "port": cfg.get("adb_port", 5555)}, logbook, self.options)
            caps |= {"screenshot", "screensaver", "diagnose"}
            self.probe_ports[self.adb.port] = "ADB"
            self.default_wake = ("remote", "adb", "wol", "cast")
        self.capabilities = frozenset(caps)
        _ERRORS.logbook = logbook
        self.needs_pairing = False
        self._remote = None
        self._pairing = None
        self._state = {}
        self._lock = threading.RLock()
        self._next_try = 0.0
        self._last_error = ""

    # --- connection ---------------------------------------------------------------------
    def _new_remote(self):
        try:
            from androidtvremote2 import AndroidTVRemote
        except ImportError:
            raise DeviceError("androidtvremote2 is not installed (pip install androidtvremote2)")
        return AndroidTVRemote("Whole-House Remote", self.certfile, self.keyfile, self.host,
                               api_port=self.api_port, pair_port=self.pair_port, loop=asyncio.get_running_loop())

    async def _connect(self):
        from androidtvremote2 import CannotConnect, ConnectionClosed, InvalidAuth
        atv = self._new_remote()
        await atv.async_generate_cert_if_missing()
        atv.add_is_on_updated_callback(lambda v: self._state.__setitem__("is_on", v))
        atv.add_current_app_updated_callback(lambda v: self._state.__setitem__("app", v))
        atv.add_volume_info_updated_callback(lambda v: self._state.__setitem__("volume", v))
        atv.add_is_available_updated_callback(lambda v: self._state.__setitem__("available", v))
        try:
            await atv.async_connect()
        except InvalidAuth:
            self.needs_pairing = True
            raise DeviceError("Not paired with this TV yet. Use 'Start pairing'.")
        except CannotConnect:
            raise DeviceError(f"Cannot reach {self.host}:{self.api_port} (TV off or in deep sleep?)")
        except ConnectionClosed:
            raise DeviceError("The TV closed the connection")
        self.needs_pairing = False
        atv.keep_reconnecting(lambda: setattr(self, "needs_pairing", True))
        self._state.update(is_on=atv.is_on, app=atv.current_app, volume=atv.volume_info,
                           info=atv.device_info, available=True)
        self._remote = atv

    def _ensure(self, quiet=False):
        with self._lock:
            if self._remote:
                return
            if quiet and time.time() < self._next_try:
                raise DeviceError(self._last_error)
            t0 = time.perf_counter()
            try:
                _Loop.run(self._connect(), 12)
            except DeviceError as e:
                self._last_error, self._next_try = str(e), time.time() + 6
                if not quiet:
                    self.log("connect", f"connect {self.host}:{self.api_port}", None, False, (time.perf_counter() - t0) * 1000, str(e))
                raise
            if not quiet:
                self.log("connect", f"connect {self.host}:{self.api_port}", None, True, (time.perf_counter() - t0) * 1000, "connected")

    def _reset(self):
        with self._lock:
            atv, self._remote = self._remote, None
            self._state = {}
        if atv:
            async def go():
                atv.disconnect()
            try:
                _Loop.run(go(), 3)
            except DeviceError:
                pass

    def _call(self, method, arg, action, retry=True, record=True, count=1):
        """Run one non-blocking remote call on the loop thread (the library is not thread-safe)."""
        self._ensure()
        request = f"{method}({arg!r})" + (f" x{count}" if count > 1 else "")
        t0 = time.perf_counter()

        async def go():
            for _ in range(count):
                getattr(self._remote, method)(arg)
        try:
            _Loop.run(go(), 8)
        except DeviceError:
            raise
        except Exception as e:
            from androidtvremote2 import ConnectionClosed
            if isinstance(e, ConnectionClosed) and retry:
                self._reset()
                return self._call(method, arg, action, retry=False, record=record, count=count)
            self.log(action, request, None, False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")
            raise DeviceError(f"{type(e).__name__}: {e}")
        # The protocol gives no acknowledgement, only that the message was queued.
        if record:
            self.log(action, request, None, True, (time.perf_counter() - t0) * 1000, "queued (the protocol has no acknowledgement)")

    # --- Driver interface ---------------------------------------------------------------
    def status(self, quiet=False):
        try:
            self._ensure(quiet=quiet)
        except DeviceError as e:
            return {"online": False, "power": None, "app": None, "error": str(e), "needsPairing": self.needs_pairing}
        s = self._state
        if s.get("available") is False:
            return {"online": False, "power": None, "app": None, "needsPairing": self.needs_pairing,
                    "error": "Connection lost; reconnecting"}
        is_on, app = s.get("is_on"), s.get("app")
        power = "on" if is_on else "standby" if is_on is False else None
        if is_on and app in DREAM_APPS:
            power = "dreaming"
        out = {"online": True, "power": power,
               "app": app, "appName": FRIENDLY.get(app, app), "volume": s.get("volume"),
               "needsPairing": self.needs_pairing, "detail": s.get("info")}
        if self.adb and is_on:
            out["media"] = self.adb.media(app)
        return out

    def _in_screensaver(self, status):
        return status.get("power") == "dreaming" or status.get("app") in DREAM_APPS

    def valid_native_key(self, key):
        return bool(KEY_RE.match(key))

    def send_key(self, native, count=1):
        self._call("send_key_command", native, f"key {native}" + (f" x{count}" if count > 1 else ""), count=count)

    def power(self, on):
        self._ensure()
        is_on = self._state.get("is_on")
        if on and self._state.get("app") in DREAM_APPS:
            self.send_key("KEYCODE_HOME")   # leaves the screensaver; POWER would switch the TV off
            return
        # KEYCODE_POWER toggles, so only send it when it will change something.
        if (on and is_on) or (not on and is_on is False):
            self.log("power", "", None, True, None, f"already {'on' if on else 'standby'}; nothing sent")
            return
        self.send_key("KEYCODE_POWER")

    def apps(self):
        if self.adb:
            return self.adb.apps()
        configured = self.cfg.get("apps")
        if configured:
            return [{"id": a, "name": FRIENDLY.get(a, a), "type": "app"} if isinstance(a, str)
                    else {"id": a["id"], "name": a.get("name", a["id"]), "type": "app"} for a in configured]
        return DEFAULT_APPS

    def launch(self, app_id, params=None):
        """Open an app. Some TVs (a TCL, in testing) reject the Remote protocol's app links, so ADB goes first
        when it is enabled, and every Remote attempt is checked: the app must actually come to the front."""
        is_package = bool(PACKAGE_RE.match(app_id))
        if not (is_package or LINK_RE.match(app_id)):
            raise DeviceError("app must be a package name or a link")
        self.leave_screensaver()
        failures = []
        if self.adb and is_package:
            try:
                self.adb.launch(app_id)
                return "launched via ADB"
            except DeviceError as e:
                failures.append(f"ADB: {e}")
        links = ([f"intent://#Intent;action=android.intent.action.MAIN;category=android.intent.category.LEANBACK_LAUNCHER;"
                  f"package={app_id};end", f"market://launch?id={app_id}"] if is_package else [app_id])
        for link in links:
            ok, why = self._launch_link(link, app_id if is_package else None)
            if ok:
                return f"launched via a {link.split(':', 1)[0]}: link over the Remote protocol"
            failures.append(f"{link.split(':', 1)[0]}: link {why}")
        raise DeviceError(f"Could not open {app_id}: " + "; ".join(failures) +
                          ("" if self.adb else '. Set "adb": true on this device to launch apps over ADB, which works on TVs '
                                               "that reject app links."))

    def _launch_link(self, link, package):
        """Send one launch link and watch for the outcome. -> (ok, reason)."""
        t0 = time.time()
        self._call("send_launch_app_command", link, f"launch {link[:70]}", record=False)
        while time.time() - t0 < 4:
            errors = _ERRORS.since(t0)
            if errors:
                self.log("launch", link, None, False, (time.time() - t0) * 1000, errors[0])
                return False, "was rejected by the TV"
            if package and self._state.get("app") == package:
                self.log("launch", link, None, True, (time.time() - t0) * 1000, f"{package} is in the foreground")
                return True, ""
            time.sleep(0.2)
        if package:
            self.log("launch", link, None, False, 4000, f"sent, but the TV never switched to {package}")
            return False, "was sent but the TV did not switch to the app"
        self.log("launch", link, None, True, 4000, "sent (a link cannot be verified)")
        return True, ""

    def text(self, text):
        if not TEXT_RE.match(text):
            raise DeviceError("text must be printable ASCII (max 100 chars)")
        self._call("send_text", text, "text")

    def _wake_remote(self):
        self.power(True)   # connects, then sends POWER only if the TV reports itself off
        return "connected over the Remote protocol and sent the power key"

    def _wake_adb(self):
        if not self.adb:
            raise DeviceError('ADB is not enabled for this device ("adb": true)')
        return self.adb._wake_adb()

    def _need_adb(self, what):
        if not self.adb:
            raise DeviceError(f'{what} needs ADB: set "adb": true on this device')
        return self.adb

    def screensaver(self):
        return self._need_adb("The screensaver").screensaver()

    def diagnose(self):
        return self._need_adb("Diagnose").diagnose()

    def screenshot(self):
        if not self.adb:
            raise DeviceError('Screenshots need ADB: set "adb": true on this device')
        return self.adb.screenshot()

    def jellyfin_app(self):
        return self.jellyfin.get("app") or "org.jellyfin.androidtv"

    # --- pairing / identity -------------------------------------------------------------
    def pair_start(self):
        async def go():
            from androidtvremote2 import CannotConnect, ConnectionClosed
            atv = self._new_remote()
            await atv.async_generate_cert_if_missing()
            try:
                await atv.async_start_pairing()
            except CannotConnect:
                raise DeviceError(f"Cannot reach the TV's pairing port ({self.host}:{self.pair_port}). Is the TV on?")
            except ConnectionClosed:
                raise DeviceError("The TV closed the pairing connection")
            self._pairing = atv
        try:
            _Loop.run(go(), 15)
        except DeviceError as e:
            self.log("pair start", f"pair {self.host}:{self.pair_port}", None, False, None, str(e))
            raise
        self.log("pair start", f"pair {self.host}:{self.pair_port}", None, True, None, "waiting for the code shown on the TV")
        return "Enter the code now shown on the TV"

    def pair_finish(self, code):
        code = re.sub(r"\s", "", code or "").upper()
        if not CODE_RE.match(code):
            raise DeviceError("The pairing code is 6 characters (0-9, A-F)")
        if not self._pairing:
            raise DeviceError("Start pairing first")
        atv = self._pairing

        async def go():
            from androidtvremote2 import ConnectionClosed, InvalidAuth
            try:
                await atv.async_finish_pairing(code)
            except InvalidAuth:
                raise DeviceError("Wrong pairing code")
            except ConnectionClosed:
                raise DeviceError("The pairing session ended (cancelled or timed out). Start pairing again.")
        try:
            _Loop.run(go(), 15)
        except DeviceError as e:
            self.log("pair finish", "", None, False, None, str(e))
            raise
        self._pairing = None
        self.needs_pairing = False
        self._reset()
        self.log("pair finish", "", None, True, None, "paired")
        self._ensure()
        return "paired"

    def detect_mac(self):
        async def go():
            from androidtvremote2 import CannotConnect
            atv = self._new_remote()
            await atv.async_generate_cert_if_missing()
            try:
                return await atv.async_get_name_and_mac()
            except CannotConnect:
                raise DeviceError(f"Cannot reach {self.host}:{self.pair_port}. The TV must be on to detect its MAC.")
        name, mac = _Loop.run(go(), 10)
        self.mac = mac.upper()
        self.log("detect mac", "", None, True, None, f"{name}: {self.mac}")
        return self.mac

    def close(self):
        self._reset()
