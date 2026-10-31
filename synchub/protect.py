"""UniFi Protect through its Integration API, and the go2rtc relay that turns its camera streams into ones a TV can play.

The Integration API is the one Ubiquiti documents for API keys (Protect > Settings > Control Plane > Integrations). The paths and the shape of
the answers below follow that documentation and were written without a console to try them on, so they are kept in one place (BASE and the
methods of ProtectClient) and the Test connection button says exactly what failed.

Protect hands out RTSPS addresses (rtsps://console:7441/<alias>?enableSrtp). go2rtc reads those as rtspx:// and serves them again as plain RTSP
and HLS, which VLC and most TV players accept. The relay runs on a worker (see camera_relay below) and SyncHub only ever hands a TV the relay's address,
never the console's.
"""
import hashlib
import json
import re
import threading
import time
from urllib.parse import urlsplit

import requests

from synchub import db, http, settings
from synchub.clients import ApiError, ConfigError

BASE = "/proxy/protect/integration/v1"
QUALITIES = ("high", "medium", "low")
CACHE_SECONDS = 300           # the relay's stream list is asked for at most this often (a worker polls every few seconds)
FAILURE_SECONDS = 60          # after a failed ask, wait this long before asking again


def _quiet_tls_warning():
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:                    # noqa: B902  (a warning filter must never be the reason a call fails)
        pass


def slug(name, fallback="camera"):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or fallback


class ProtectClient:
    def __init__(self, host=None, api_key=None, verify=None, timeout=8):
        self.host = re.sub(r"^[a-z]+://", "", (host if host is not None else settings.get("protect_host")).strip(), flags=re.I).strip("/")
        self.key = (api_key if api_key is not None else settings.get("protect_api_key")).strip()
        if not self.host or not self.key:
            raise ConfigError("The UniFi Protect address or API key is not set.")
        self.verify = (settings.get("protect_verify_tls") == "1") if verify is None else bool(verify)
        self.timeout = timeout
        if not self.verify:
            _quiet_tls_warning()           # a console's own certificate is not one a browser trusts

    @property
    def console(self):
        return self.host.split(":")[0]

    def _call(self, method, path, event, **kw):
        headers = {"X-API-KEY": self.key, "Accept": "application/json"}
        try:
            res = http.request("protect", event, method, f"https://{self.host}{BASE}{path}", headers=headers, verify=self.verify, timeout=self.timeout, **kw)
        except requests.exceptions.SSLError as exc:
            reason = str(exc)
            if self.verify and "CERTIFICATE_VERIFY_FAILED" in reason:
                raise ApiError("The console's certificate is not trusted. Switch off Check the console's certificate in the Protect settings.")
            # Not a certificate problem (the check is off, or the failure is in the handshake itself): usually a port that does not speak HTTPS.
            found = re.search(r"\[SSL[^\]]*\][^()']*", reason)
            detail = found.group(0).strip() if found else "the handshake failed"
            raise ApiError(f"Could not make a secure (HTTPS) connection to {self.host}: {detail}. Use the console's own address with no port, or its HTTPS port (443).")
        except requests.RequestException as exc:
            raise ApiError(f"Could not reach UniFi Protect at {self.host}: {exc.__class__.__name__}")
        if res.status_code in (401, 403):
            raise ApiError("UniFi Protect rejected the API key.")
        if res.status_code == 404:
            raise ApiError("UniFi Protect has no Integration API at that address (is Protect up to date, and is this the console's own address?).")
        if res.status_code >= 400:
            raise ApiError(f"UniFi Protect {event} failed: HTTP {res.status_code}")
        return res

    def info(self):
        """{"applicationVersion": "6.x"} and the like."""
        return self._call("GET", "/meta/info", "meta").json()

    def cameras(self):
        """[{"id", "name", "state", "model"}], cameras that are connected first."""
        found = self._call("GET", "/cameras", "cameras").json()
        found = found.get("data", found) if isinstance(found, dict) else found
        cams = [{"id": c.get("id"), "name": c.get("name") or c.get("marketName") or c.get("id"), "state": c.get("state") or "", "model": c.get("modelKey") or c.get("marketName") or ""}
                for c in found or [] if isinstance(c, dict) and c.get("id")]
        return sorted(cams, key=lambda c: (c["state"] != "CONNECTED", c["name"].lower()))

    def rtsps(self, camera_id, quality="medium"):
        """The camera's RTSPS address at this quality, asking Protect to make one the first time."""
        path = f"/cameras/{camera_id}/rtsps-stream"
        res = self._call("GET", path, "rtsps")
        urls = res.json() if res.content else {}
        if not urls.get(quality):
            urls = self._call("POST", path, "rtsps create", json={"qualities": [quality]}).json()
        url = urls.get(quality) or next((v for v in urls.values() if isinstance(v, str) and v.startswith("rtsp")), "")
        if not url:
            raise ApiError("UniFi Protect did not give an RTSPS address for that camera.")
        return url


def relay_source(rtsps_url):
    """What go2rtc is told to read: Protect's rtsps://console:7441/alias?enableSrtp as rtspx://console:7441/alias (RTSPS that accepts the console's own certificate)."""
    parts = urlsplit(rtsps_url)
    return f"rtspx://{parts.netloc}{parts.path}"


def chosen_cameras():
    """The camera ids picked in Settings (a set); empty means every camera is served."""
    return {c for c in (settings.get("protect_cameras") or "").split(",") if c.strip()}


def relay_streams(client, quality="medium", only=None):
    """{stream name: go2rtc source} for every connected camera (or just those ids in `only`), with stream names that are plain words and never repeat."""
    streams, names = {}, {}
    for cam in client.cameras():
        if cam["state"] and cam["state"] != "CONNECTED":
            continue
        if only and cam["id"] not in only:
            continue
        name = slug(cam["name"])
        names[name] = names.get(name, 0) + 1
        if names[name] > 1:
            name = f"{name}-{names[name]}"
        streams[name] = {"title": cam["name"], "src": relay_source(client.rtsps(cam["id"], quality))}
    return streams


# ---------------------------------------------------------------------------------------------- the relay
_lock = threading.Lock()
_cache = {"at": 0.0, "key": None, "streams": None, "error": ""}


def enabled():
    return settings.get("protect_enabled") == "1" and bool(settings.get("protect_host")) and bool(settings.get("protect_api_key"))


def streams(force=False):
    """-> {name: {"title", "src"}}, from a copy that is at most CACHE_SECONDS old. Raises ApiError when the console cannot be asked and nothing is cached."""
    if not enabled():
        raise ConfigError("UniFi Protect is not switched on in Settings.")
    key = (settings.get("protect_host"), settings.get("protect_api_key"), settings.get("protect_quality"), settings.get("protect_verify_tls"), settings.get("protect_cameras"))
    with _lock:
        fresh = _cache["key"] == key and time.time() - _cache["at"] < (CACHE_SECONDS if _cache["streams"] is not None else FAILURE_SECONDS)
        if fresh and not force:
            if _cache["streams"] is None:
                raise ApiError(_cache["error"])
            return _cache["streams"]
    try:
        found = relay_streams(ProtectClient(), settings.get("protect_quality") or "medium", chosen_cameras())
        error = ""
    except (ApiError, ConfigError) as exc:
        found, error = None, str(exc)
    with _lock:
        if found is None and _cache["key"] == key and _cache["streams"] is not None:
            _cache["at"] = time.time() - CACHE_SECONDS + FAILURE_SECONDS            # keep what worked, try again soon
            return _cache["streams"]
        _cache.update(at=time.time(), key=key, streams=found, error=error)
    if found is None:
        raise ApiError(error)
    return found


def forget():
    with _lock:
        _cache.update(at=0.0, key=None, streams=None, error="")


def relay_worker_id():
    value = settings.get("protect_relay_worker")
    return int(value) if value.isdigit() else None


def is_relay_name(conn, name):
    """Whether the worker with this name is the one chosen to run the relay (its setup files then include go2rtc)."""
    row = conn.execute("SELECT id FROM worker WHERE name = ?", (name,)).fetchone()
    return bool(row) and row["id"] == relay_worker_id() and settings.get("protect_enabled") == "1"


NONCE_KEY = "protect_relay_nonce"
INSTALL_KEY = "relay_install:{}"
RELAY_MARK = "# Managed by SyncHub (camera relay)."
RELAY_PORTS = (1984, 8554)


def send_again():
    """Make the relay worker give go2rtc its cameras again (the agent notices the new number), after asking Protect afresh."""
    forget()
    settings.set_value(NONCE_KEY, str(time.time()))


def relay_override():
    """docker-compose.override.yml for the relay worker: Docker Compose loads it next to the worker's own docker-compose.yml, which stays as it is."""
    return "\n".join([RELAY_MARK + " Edits are overwritten; change the relay in Settings.",
                      "services:", "  worker:", "    environment:", "      GO2RTC_URL: http://go2rtc:1984",
                      "  go2rtc:", "    image: alexxit/go2rtc", "    container_name: synchub-go2rtc", "    restart: unless-stopped",
                      "    ports:", *(f'      - "{port}:{port}"' for port in RELAY_PORTS)]) + "\n"


def install_report(conn, worker_id):
    """What the worker PC's updater last said about installing the relay: {"status", "error", "at"} or None."""
    return json.loads(settings.get(INSTALL_KEY.format(worker_id), "", conn=conn) or "null")


def report_install(conn, worker_id, info):
    """The updater on the PC reports: installed (go2rtc is running), native (the worker's own compose file already has it), removed or failed."""
    info = info if isinstance(info, dict) else {}
    if info.get("status") not in ("installed", "native", "removed", "failed", "waiting"):
        raise ValueError("The relay report needs a status of installed, native, removed, failed or waiting.")
    report = {"status": info["status"], "error": str(info.get("error") or "")[:1500], "at": db.now()}
    settings.set_value(INSTALL_KEY.format(worker_id), json.dumps(report), conn=conn)
    return report


def host_instruction(conn, worker_id):
    """What the PC's updater should do about the relay, as fields for its /update/check answer: relay_mode install (with the file's text and a hash
    of it) on the chosen worker, remove on a worker that had it installed but is no longer chosen, else nothing."""
    chosen = worker_id == relay_worker_id() and settings.get("protect_enabled", conn=conn) == "1"
    if chosen:
        text = relay_override()
        return {"relay_mode": "install", "relay_override": text, "relay_hash": hashlib.sha1(text.encode()).hexdigest()[:12]}
    done = install_report(conn, worker_id)
    if done and done["status"] in ("installed", "failed", "waiting"):
        return {"relay_mode": "remove"}
    return {}


def relay_status(conn):
    """Everything the Camera relay card shows: the chosen worker, a checklist from the choice to cameras being served, and where the relay answers."""
    from synchub import workers
    wid = relay_worker_id()
    w = workers.get(conn, wid) if wid else None
    host = relay_host()
    out = {"enabled": settings.get("protect_enabled", conn=conn) == "1", "worker": {"id": w["id"], "name": w["name"], "online": w["online"]} if w else None,
           "address": host, "page": f"http://{host.split(':')[0]}:1984" if host else "", "steps": []}
    if not w:
        out["steps"] = [{"label": "Choose the worker that runs the relay (above) and save", "ok": False}]
        return out
    seen = workers.host_seen(conn, wid)
    install = install_report(conn, wid)
    agent = (w["status"] or {}).get("relay") or {}
    served = len(agent.get("streams") or [])
    out["install"], out["agent"] = install, agent
    out["steps"] = [
        {"label": f"Relay worker: {w['name']}", "ok": True},
        {"label": f"{w['name']} is online", "ok": bool(w["online"])},
        {"label": "Its update helper is checking in" + ("" if seen else " (install it once: agent/auto-update.sh --install)"), "ok": seen},
        {"label": "go2rtc is installed" + (f": {install['error']}" if install and install["status"] == "failed" else ""),
         "ok": bool(install and install["status"] in ("installed", "native")), "failed": bool(install and install["status"] == "failed")},
        {"label": "go2rtc answers on the worker" + (f": {agent['error']}" if agent.get("error") else ""), "ok": bool(agent.get("up"))},
        {"label": f"Cameras served: {served}", "ok": bool(agent.get("up")) and served > 0},
        {"label": "SyncHub knows the relay's address" + ("" if host else " (it is learned from the worker's next poll, or type it above)"), "ok": bool(host)},
    ]
    return out


def test_relay():
    """From SyncHub itself: ask the relay for its streams, as the TVs will reach it. -> list of stream names. Raises ApiError with a plain reason."""
    host = relay_host().split(":")[0]
    if not host:
        raise ConfigError("SyncHub does not know where the relay is yet.")
    try:
        res = http.request("protect", "relay test", "GET", f"http://{host}:1984/api/streams", timeout=5)
        res.raise_for_status()
        return sorted(res.json() or {})
    except (requests.RequestException, ValueError) as exc:
        raise ApiError(f"Could not reach the relay at http://{host}:1984 from SyncHub: {exc.__class__.__name__}. Is go2rtc running, and is port 1984 open on that PC?")


def relay_host():
    """The address a TV reaches the relay at: the one typed in Settings, else the address the relay worker last polled SyncHub from."""
    return settings.get("protect_relay_host") or settings.get("protect_relay_addr")


def note_worker_address(worker_id, address):
    if worker_id == relay_worker_id() and address and address not in ("127.0.0.1", "::1") and settings.get("protect_relay_addr") != address:
        settings.set_value("protect_relay_addr", address)


def for_worker(worker_id):
    """What a poll answer tells the relay worker: {"streams": {name: src}} or None (this is not the relay worker, or Protect cannot be asked right now)."""
    if worker_id != relay_worker_id() or not enabled():
        return None
    try:
        return {"streams": {name: item["src"] for name, item in streams().items()}, "nonce": settings.get(NONCE_KEY)}
    except (ApiError, ConfigError):
        return None


def play_url(name, kind="rtsp"):
    """The address a TV opens for one camera of the relay."""
    host = relay_host().split(":")[0]
    if not host:
        raise ConfigError("SyncHub does not know where the camera relay is. Pick the relay worker in Settings and let it poll once, or type its address.")
    return f"rtsp://{host}:8554/{name}" if kind == "rtsp" else f"http://{host}:1984/api/stream.m3u8?src={name}"
