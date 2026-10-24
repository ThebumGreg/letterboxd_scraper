"""Device drivers.

A driver wraps one kind of TV/streamer behind a common interface so the web UI and
the orchestrator never care how a device is controlled. To add a new kind (Apple TV
via pyatv, Fire TV, ...): subclass Driver in a new module, decorate it with
@register, and import the module at the bottom of this file. Devices are declared
in devices.json with a matching "type".
"""
import json
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HOST_RE = re.compile(r"^[\w.\-]+$")

# Logical keys every remote layout in the UI is built from. A driver maps each to
# its native key name, or None when the device has no such key.
LOGICAL_KEYS = (
    "home", "back", "up", "down", "left", "right", "ok", "options", "info", "search",
    "play_pause", "stop", "rewind", "ff", "prev", "next", "replay",
    "vol_up", "vol_down", "mute", "ch_up", "ch_down", "backspace", "enter",
)

REGISTRY = {}


class DeviceError(Exception):
    pass


def register(cls):
    REGISTRY[cls.type] = cls
    return cls


def wake_on_lan(mac, broadcast=None, host=None, repeat=3):
    """Send a magic packet to every plausible address. -> {"targets": [...], "errors": [...]}"""
    clean = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(clean) != 12:
        raise DeviceError(f"bad MAC address: {mac!r}")
    packet = bytes.fromhex("FF" * 6 + clean * 16)
    targets = [("255.255.255.255", 9), ("255.255.255.255", 7)]
    if broadcast:
        targets.append((broadcast, 9))
    if host:
        targets.append((host, 9))   # some TVs answer a unicast magic packet while their ARP entry is warm
    targets = list(dict.fromkeys(targets))
    errors = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            for target in targets:
                try:
                    s.sendto(packet, target)
                except OSError as e:
                    errors.append(f"{target[0]}:{target[1]} {e}")
    return {"targets": [f"{h}:{p}" for h, p in targets], "errors": sorted(set(errors))}


class Driver:
    type = ""
    label = ""
    log_kind = ""                   # tag shown in the log: adb, roku, ... (defaults to type)
    capabilities = frozenset()      # keys, apps, text, screenshot, power_on, power_off, launch
    keymap = {}                     # logical key -> native key (None = unsupported)
    extra_keys = ()                 # ((label, native key), ...) shown under "More keys"
    default_session_rule = {}       # how this kind of device's Jellyfin client appears in /Sessions
    default_wake = ("key", "wol")   # wake methods fired together when the device is not on
    probe_ports = {}                # port -> name, for the network probe

    def __init__(self, cfg, logbook, options=None):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name") or cfg["id"]
        self.host = cfg["host"]
        self.mac = cfg.get("mac")
        self.logbook = logbook
        self.options = options or {}
        self.jellyfin = cfg.get("jellyfin") or {}

    def log(self, action, request="", status=None, ok=None, ms=None, response=""):
        return self.logbook.add(self.log_kind or self.type, self.id, action, request, status, ok, ms, response)

    def session_rule(self):
        return self.jellyfin.get("session") or self.default_session_rule

    def wol(self):
        """Send Wake-on-LAN for this device (needs its MAC)."""
        if not self.mac:
            raise DeviceError(f'No MAC address known for {self.name}. Add "mac" to devices.json, '
                              "or press Detect MAC while the device is on.")
        t0 = time.perf_counter()
        info = wake_on_lan(self.mac, self.cfg.get("broadcast"), self.host)
        ok = len(info["errors"]) < len(info["targets"])
        self.logbook.add("network", self.id, "wake-on-lan", f"magic packet for {self.mac} to {', '.join(info['targets'])}",
                         None, ok, (time.perf_counter() - t0) * 1000,
                         "sent" + (f"; errors: {'; '.join(info['errors'])}" if info["errors"] else ""))
        if not ok:
            raise DeviceError("could not send the magic packet: " + "; ".join(info["errors"]))
        return info

    def detect_mac(self):
        raise DeviceError("MAC detection is not supported by this device type")

    # --- waking ---------------------------------------------------------------------------
    def wake_methods(self):
        return list(self.cfg.get("wake") or self.default_wake)

    def wake_with(self, name):
        """Send one wake attempt. -> a short description; raises DeviceError if it cannot be tried."""
        fn = getattr(self, f"_wake_{name}", None)
        if not fn:
            raise DeviceError(f"unknown wake method {name!r}")
        return fn()

    def _wake_key(self):
        self.power(True)
        return "sent the power-on key"

    def _wake_wol(self):
        info = self.wol()
        return f"magic packet sent to {len(info['targets'])} addresses"

    def _wake_cast(self):
        from .cast import cast_wake
        return cast_wake(self.host)

    # --- sleeping ---------------------------------------------------------------------------
    def sleep(self, mode=None):
        """Put the device to sleep. mode: "standby" (default) or "screensaver" (stays reachable)."""
        mode = mode or self.cfg.get("sleep") or "standby"
        if mode == "screensaver":
            return self.screensaver()
        self.power(False)
        return "standby"

    def screensaver(self):
        raise DeviceError("this device cannot start a screensaver")

    def diagnose(self):
        raise DeviceError("diagnostics are not available for this device type")

    # --- network probe ----------------------------------------------------------------------
    def probe(self):
        """Which network paths answer right now? Run it with the device on and again in standby."""
        t0 = time.perf_counter()
        ports, lines = [], []
        for port, name in self.probe_ports.items():
            p0 = time.perf_counter()
            try:
                socket.create_connection((self.host, port), timeout=1.5).close()
                opened, note = True, "open"
            except OSError as e:
                opened, note = False, e.strerror or type(e).__name__
            ms = round((time.perf_counter() - p0) * 1000)
            ports.append({"port": port, "name": name, "open": opened})
            lines.append(f"{port} {name}: {note} ({ms} ms)")
        ping = None
        if shutil.which("ping"):
            try:
                ping = subprocess.run(["ping", "-c", "1", "-W", "1", self.host], capture_output=True, timeout=4).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ping = None
        lines.insert(0, f"ping: {'replies' if ping else 'no reply' if ping is False else 'not tested'}")
        any_open = any(p["open"] for p in ports) or bool(ping)
        self.logbook.add("network", self.id, "probe", f"tcp connect {self.host} " + ",".join(map(str, self.probe_ports)),
                         None, any_open, (time.perf_counter() - t0) * 1000, "\n".join(lines))
        return {"ping": ping, "ports": ports, "summary": "\n".join(lines)}

    def close(self):
        """Release connections (called when the device list is reloaded)."""

    # --- interface drivers implement -------------------------------------------------
    def status(self, quiet=False):
        """-> {"online": bool, "power": on|standby|off|dreaming|None, "app": id, "appName": str, "error": str}"""
        raise NotImplementedError

    def valid_native_key(self, key):
        return False

    def send_key(self, native):
        raise NotImplementedError

    def power(self, on):
        raise NotImplementedError

    def apps(self):
        """-> [{"id", "name", "type"}]"""
        raise NotImplementedError

    def launch(self, app_id, params=None):
        raise NotImplementedError

    def text(self, text):
        raise DeviceError("text entry is not supported by this device")

    def screenshot(self):
        raise DeviceError("screenshots are not supported by this device")

    def icon(self, app_id):
        """-> (bytes, mimetype) or raise DeviceError."""
        raise DeviceError("app icons are not provided by this device")

    def jellyfin_app(self):
        """Id of the app to launch when we need the Jellyfin client."""
        raise NotImplementedError

    # --- shared ------------------------------------------------------------------------
    def describe(self):
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "typeLabel": self.label,
            "host": self.host,
            "capabilities": sorted(self.capabilities),
            "keys": {k: self.keymap.get(k) is not None for k in LOGICAL_KEYS},
            "extraKeys": [{"label": l, "key": k} for l, k in self.extra_keys],
            "sessionRule": self.session_rule(),
            "hasMac": bool(self.mac),
            "sleepMode": self.cfg.get("sleep") or "standby",
            "wakeMethods": self.wake_methods(),
        }


def build_driver(cfg, logbook, options):
    dev_id = cfg.get("id", "")
    if not ID_RE.match(dev_id):
        raise DeviceError(f"device id {dev_id!r} must be lowercase letters, digits, - or _")
    if not HOST_RE.match(str(cfg.get("host", ""))):
        raise DeviceError(f"device {dev_id}: missing or invalid host")
    cls = REGISTRY.get(cfg.get("type"))
    if not cls:
        raise DeviceError(f"device {dev_id}: unknown type {cfg.get('type')!r} (known: {', '.join(sorted(REGISTRY))})")
    return cls(cfg, logbook, options)


def load_devices(path, logbook, options):
    """-> (devices by id in file order, error message or None)."""
    devices = {}
    if not path or not Path(path).exists():
        return devices, f"No device file at {path}. Copy devices.example.json to devices.json and edit it."
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return devices, f"Could not read {path}: {e}"
    for cfg in raw.get("devices", []):
        try:
            drv = build_driver(cfg, logbook, options)
        except DeviceError as e:
            logbook.add("system", cfg.get("id"), "config", ok=False, response=str(e))
            continue
        if drv.id in devices:
            logbook.add("system", drv.id, "config", ok=False, response=f"duplicate device id {drv.id}")
            continue
        devices[drv.id] = drv
    return devices, None


from . import androidtv, googletv, roku  # noqa: E402,F401  (registers the built-in drivers)
