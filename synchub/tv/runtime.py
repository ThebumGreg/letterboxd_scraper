"""The TV remote's shared state: the devices, the choices saved from the UI, the log of outbound calls and the Jellyfin client.

One instance lives for the life of the process (`get()`); tests call `configure(folder)` to start from a clean folder.
Everything the remote saves is under one folder (`<data>/tv`): prefs.json, certs/ (the Remote-protocol pairing identity),
tv.log (JSON lines of every outbound call) and, optionally, a hand-written devices.json for options the UI does not edit.
"""
import json
import os
import re
import threading
from pathlib import Path

from . import discovery
from .devices import HOST_RE, ID_RE as DEVICE_ID_RE, REGISTRY, DeviceError, load_devices
from .jellyfin import JellyfinClient, JellyfinError
from .logbook import LogBook

QUICK_MAX = 4            # apps pinned to a remote
APPS_MAX = 300           # apps one TV can have on its remote
APP_ID_RE = re.compile(r"^[\w.\-]{1,120}$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}$")
SLEEP_MODES = ("standby", "screensaver")
APPS_TTL = 600           # seconds an installed-app list is reused
EMPTY_PREFS = {"quick": {}, "session": {}, "play": {}, "apps": {}, "resume": {}, "devices": []}
RESUME_MODES = ("a", "b", "c", "e")


def _empty_prefs():
    return json.loads(json.dumps(EMPTY_PREFS))


class Runtime:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.prefs_path = self.folder / "prefs.json"
        self.config_path = Path(os.environ.get("SYNCHUB_TV_DEVICES") or self.folder / "devices.json")
        self.options = {"adb": os.environ.get("ADB_PATH", "adb"), "cert_dir": str(self.folder / "certs"),
                        "adb_home": str(self.folder / "adb")}      # adb keeps the key the TVs were told to trust here, so it survives a container being replaced
        self.logbook = LogBook(path=str(self.folder / "tv.log"))
        self.lock = threading.RLock()
        self.prefs = _empty_prefs()
        self.devices, self.config_error = {}, None
        self.last_status, self.icons, self.apps_cache = {}, {}, {}
        self.camera = {}            # TV id -> what it was doing before a camera was put on it (see synchub.tv.camera)
        self._jf = (None, None)
        self.load_prefs()
        self.reload_devices()

    # ------------------------------------------------------------------ Jellyfin (the address and key are SyncHub's own settings)
    def jf(self):
        from synchub import settings
        url, key = settings.get("jellyfin_url").rstrip("/"), settings.get("jellyfin_api_key")
        if not (url and key) or settings.get("disable_jellyfin_connection") == "1":
            raise JellyfinError("Jellyfin is not set up: enter its address and API key under Settings, Connections.")
        if self._jf[0] != (url, key):
            self._jf = ((url, key), JellyfinClient(url, key, self.logbook))
        return self._jf[1]

    def jellyfin_url(self):
        from synchub import settings
        return settings.get("jellyfin_url").rstrip("/") or None

    # ------------------------------------------------------------------ saved choices
    def load_prefs(self):
        try:
            data = json.loads(self.prefs_path.read_text(encoding="utf-8"))
            prefs = {"quick": {}, "apps": {},
                     "session": {k: v for k, v in (data.get("session") or {}).items() if isinstance(v, dict)},
                     "play": {k: v for k, v in (data.get("play") or {}).items() if v == "url"},
                     "resume": {k: v for k, v in (data.get("resume") or {}).items() if v in RESUME_MODES},
                     "devices": [d for d in (data.get("devices") or []) if isinstance(d, dict)]}
            for key, limit in (("quick", QUICK_MAX), ("apps", APPS_MAX)):
                for dev_id, apps in (data.get(key) or {}).items():
                    try:
                        prefs[key][dev_id] = clean_apps(apps, limit)
                    except DeviceError:
                        pass   # one bad entry must not discard everything else
            self.prefs = prefs
        except (OSError, ValueError, TypeError, AttributeError):
            self.prefs = _empty_prefs()

    def save_prefs(self):
        tmp = self.prefs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.prefs, indent=2), encoding="utf-8")
        os.replace(tmp, self.prefs_path)   # a crash mid-write must not lose the saved choices

    def update_prefs(self, change):
        """Runs change(prefs) under the lock, then saves."""
        with self.lock:
            change(self.prefs)
            self.save_prefs()

    # ------------------------------------------------------------------ devices
    def reload_devices(self):
        with self.lock:
            for old in self.devices.values():
                try:
                    old.close()
                except Exception as e:  # a failing disconnect must not block the reload
                    self.logbook.add("system", old.id, "close", ok=False, response=str(e))
            self.icons.clear()
            self.apps_cache.clear()
            self.devices, self.config_error = load_devices(self.config_path, self.logbook, self.options, self.prefs["devices"])
            self.apply_overrides()
            self.last_status.clear()
        self.logbook.add("system", None, "config", ok=self.config_error is None,
                         response=self.config_error or f"Loaded {len(self.devices)} device(s)")

    def apply_overrides(self):
        for d in self.devices.values():
            d.session_override = self.prefs["session"].get(d.id)
            d.play_learned = self.prefs["play"].get(d.id)
            d.resume_override = self.prefs["resume"].get(d.id)
            d._warned_rule = False

    def device(self, dev_id):
        d = self.devices.get(dev_id)
        if not d:
            raise DeviceError(f"unknown device {dev_id!r}")
        return d

    # ------------------------------------------------------------------ TVs added from the UI
    def managed_ids(self):
        return {c["id"] for c in self.prefs["devices"]}

    def tv_view(self, d):
        cfg, managed = d.cfg, d.id in self.managed_ids()
        return {"id": d.id, "name": d.name, "type": d.type, "typeLabel": d.label, "host": d.host, "mac": d.mac or "",
                "sleep": cfg.get("sleep") or "standby", "adb": bool(cfg.get("adb")) or d.type == "androidtv",
                "managed": managed, "source": "saved in the app" if managed else "devices.json"}

    def clean_tv(self, body, dev_id=None):
        """Validate the fields the TVs page edits. -> a device config ready to save."""
        kind, name, host = body.get("type"), str(body.get("name") or "").strip()[:60], str(body.get("host") or "").strip()
        if kind not in REGISTRY:
            raise DeviceError(f"choose a type: {', '.join(sorted(REGISTRY))}")
        if not HOST_RE.match(host):
            raise DeviceError("enter the TV's IP address (such as 192.168.1.50)")
        clash = next((d for d in self.devices.values() if d.id != dev_id and d.host == host), None)
        if clash:
            raise DeviceError(f"{host} is already added as {clash.name}")
        if not name:
            name = {"roku": "Roku", "googletv": "Google TV", "androidtv": "Android TV"}.get(kind, kind)
        if dev_id is None:
            taken = set(self.devices) | self.managed_ids()
            base = dev_id = slug(name, kind)
            n = 1
            while dev_id in taken:
                n += 1
                dev_id = f"{base}-{n}"
            if not DEVICE_ID_RE.match(dev_id):
                raise DeviceError("could not make an id from that name")
        cfg = {"id": dev_id, "name": name, "type": kind, "host": host}
        mac = str(body.get("mac") or "").strip()
        if mac:
            if not MAC_RE.match(mac):
                raise DeviceError("the MAC address should look like AA:BB:CC:DD:EE:FF")
            digits = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
            cfg["mac"] = ":".join(digits[i:i + 2] for i in range(0, 12, 2))
        sleep = body.get("sleep") or "standby"
        if sleep not in SLEEP_MODES:
            raise DeviceError("sleep must be standby or screensaver")
        adb = bool(body.get("adb")) and kind == "googletv"
        if sleep == "screensaver" and not (adb or kind == "androidtv"):
            raise DeviceError('The screensaver sleep mode needs ADB: tick "Also use ADB" for this Google TV, or choose standby')
        if sleep != "standby":
            cfg["sleep"] = sleep
        if adb:
            cfg["adb"] = True
        return cfg

    def save_managed(self, devices):
        self.update_prefs(lambda p: p.update(devices=devices))
        self.reload_devices()

    def add_tv(self, body):
        cfg = self.clean_tv(body)
        self.save_managed(self.prefs["devices"] + [cfg])
        self.logbook.add("system", cfg["id"], "tv added", response=f'{cfg["name"]} ({cfg["type"]}) at {cfg["host"]}')
        return cfg

    def edit_tv(self, dev_id, body):
        if dev_id not in self.managed_ids():
            raise DeviceError("This TV is set in devices.json; edit it there")
        cfg = self.clean_tv(body, dev_id)
        old = next(c for c in self.prefs["devices"] if c["id"] == dev_id)
        cfg.update({k: v for k, v in old.items() if k not in cfg and k not in ("mac", "adb", "sleep")})   # keep fields the page does not edit
        self.save_managed([cfg if c["id"] == dev_id else c for c in self.prefs["devices"]])
        return cfg

    def remove_tv(self, dev_id):
        if dev_id not in self.managed_ids():
            raise DeviceError("This TV is set in devices.json; remove it there")

        def drop(p):
            for key in ("quick", "apps", "session", "play"):
                p[key].pop(dev_id, None)
        self.update_prefs(drop)
        self.save_managed([c for c in self.prefs["devices"] if c["id"] != dev_id])
        self.logbook.add("system", dev_id, "tv removed", response=dev_id)

    # ------------------------------------------------------------------ discovery
    def scan(self, ranges=None):
        if ranges is None:
            ranges = discovery.suggest_ranges([d.host for d in self.devices.values()])
        return discovery.discover(ranges, known={d.host: d.id for d in self.devices.values()}, cert_dir=self.options["cert_dir"])

    def suggested_ranges(self):
        from urllib.parse import urlparse
        hosts = [d.host for d in self.devices.values()]
        if self.jellyfin_url():
            hosts.append(urlparse(self.jellyfin_url()).hostname or "")
        return discovery.suggest_ranges(hosts)


def clean_apps(apps, limit):
    """[{"id", "name"}] with valid, unique ids; raises DeviceError on anything malformed."""
    if not isinstance(apps, list) or len(apps) > limit:
        raise DeviceError(f"choose up to {limit} apps")
    clean = []
    for a in apps:
        if not isinstance(a, dict) or not APP_ID_RE.match(str(a.get("id", ""))):
            raise DeviceError("bad app id")
        if a["id"] not in [c["id"] for c in clean]:
            clean.append({"id": a["id"], "name": str(a.get("name") or a["id"])[:60]})
    return clean


def slug(name, fallback):
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")
    return text if text and text[0].isalnum() else fallback


_runtime = None
_runtime_lock = threading.Lock()


def configure(folder):
    """Start over with this folder (used at start-up and by tests)."""
    global _runtime
    with _runtime_lock:
        _runtime = Runtime(folder)
        return _runtime


def get():
    with _runtime_lock:
        if _runtime is not None:
            return _runtime
    from config import DATA_DIR
    return configure(DATA_DIR / "tv")
