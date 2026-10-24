"""Flask routes for the whole-house remote."""
import argparse
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, Response, g, jsonify, request, send_from_directory

from . import orchestrator
from .devices import DeviceError, load_devices
from .jellyfin import JellyfinClient, JellyfinError, match_session, summarize_session
from .logbook import LogBook

STATIC = os.path.join(os.path.dirname(__file__), "static")
ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
PLAYSTATE_COMMANDS = {"Stop", "Pause", "Unpause", "NextTrack", "PreviousTrack", "Seek", "Rewind", "FastForward", "PlayPause"}
PLAY_COMMANDS = {"PlayNow", "PlayNext", "PlayLast", "PlayInstantMix", "PlayShuffle"}

def _build_id():
    """Changes whenever the code changes, so a tab left open across an update can reload itself."""
    newest = max((f.stat().st_mtime for f in Path(__file__).parent.rglob("*") if f.suffix in (".py", ".js", ".css", ".html")), default=0)
    return str(int(newest))


BUILD = _build_id()
app = Flask(__name__, static_folder=STATIC, static_url_path="/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
state = SimpleNamespace(logbook=LogBook(), jf=None, devices={}, config_path=None, config_error=None,
                        options={}, last_status={}, token="", public_url="", icons={},
                        prefs={"quick": {}, "session": {}, "play": {}}, prefs_path=None, prefs_lock=threading.Lock(), apps_cache={})

APPS_TTL = 600           # seconds an installed-app list is reused
QUICK_MAX = 4            # apps pinned to a remote
APP_ID_RE = re.compile(r"^[\w.\-]{1,120}$")


def reply(ok, body, status=None, **extra):
    return jsonify(ok=ok, status=status if status is not None else (200 if ok else 0), body=body, **extra)


def relay(resp):
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = resp.text[:500]
    return reply(resp.ok, body, resp.status_code)


def jf():
    if not state.jf:
        raise JellyfinError("Jellyfin is not configured (set JELLYFIN_URL and JELLYFIN_API_KEY)")
    return state.jf


def device(dev_id):
    d = state.devices.get(dev_id)
    if not d:
        raise DeviceError(f"unknown device {dev_id!r}")
    return d


def json_body():
    return request.get_json(force=True, silent=True) or {}


def valid_id(value):
    return bool(value) and bool(ID_RE.match(str(value)))


@app.errorhandler(DeviceError)
@app.errorhandler(JellyfinError)
def known_error(e):
    return reply(False, str(e)), 502


@app.before_request
def check_token():
    """Optional shared secret, for when the app is reachable from other devices on the network."""
    if not state.token:
        return None
    supplied = request.args.get("token", "")
    if supplied and hmac.compare_digest(supplied, state.token):
        g.set_cookie = True
        return None
    if hmac.compare_digest(request.cookies.get("rc_token", ""), state.token):
        return None
    return Response("Access token required. Open /?token=<your token> once from this device.", 401)


@app.after_request
def remember_token(resp):
    if getattr(g, "set_cookie", False):
        resp.set_cookie("rc_token", state.token, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 365)
    return resp


def reload_devices():
    for old in state.devices.values():
        try:
            old.close()
        except Exception as e:  # a failing disconnect must not block the reload
            state.logbook.add("system", old.id, "close", ok=False, response=str(e))
    state.icons.clear()
    state.apps_cache.clear()
    state.devices, state.config_error = load_devices(state.config_path, state.logbook, state.options)
    apply_session_overrides()
    state.last_status.clear()
    state.logbook.add("system", None, "config", ok=state.config_error is None,
                      response=state.config_error or f"Loaded {len(state.devices)} device(s) from {state.config_path}")


# ------------------------------------------------------------------ pages / config / log
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/settings")
def settings_page():
    return send_from_directory(STATIC, "settings.html")


@app.get("/manifest.webmanifest")
def web_manifest():
    return send_from_directory(os.path.join(STATIC, "pwa"), "manifest.webmanifest", mimetype="application/manifest+json")


def load_prefs():
    try:
        data = json.loads(Path(state.prefs_path).read_text(encoding="utf-8"))
        state.prefs = {"quick": {k: v for k, v in (data.get("quick") or {}).items() if isinstance(v, list)},
                       "session": {k: v for k, v in (data.get("session") or {}).items() if isinstance(v, dict)},
                       "play": {k: v for k, v in (data.get("play") or {}).items() if v == "url"}}
    except (OSError, ValueError, TypeError, AttributeError):
        state.prefs = {"quick": {}, "session": {}, "play": {}}


def apply_session_overrides():
    for d in state.devices.values():
        d.session_override = state.prefs["session"].get(d.id)
        d.play_learned = state.prefs["play"].get(d.id)
        d._warned_rule = False


def save_prefs():
    path = Path(state.prefs_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.prefs, indent=2), encoding="utf-8")
    os.replace(tmp, path)   # a crash mid-write must not lose the saved choices


@app.get("/api/prefs")
def api_prefs():
    return jsonify(prefs=state.prefs, quickMax=QUICK_MAX)


@app.put("/api/prefs/quick/<dev_id>")
def api_prefs_quick(dev_id):
    device(dev_id)
    apps = json_body().get("apps")
    if not isinstance(apps, list) or len(apps) > QUICK_MAX:
        raise DeviceError(f"choose up to {QUICK_MAX} apps")
    clean = []
    for a in apps:
        if not isinstance(a, dict) or not APP_ID_RE.match(str(a.get("id", ""))):
            raise DeviceError("bad app id")
        if a["id"] not in [c["id"] for c in clean]:
            clean.append({"id": a["id"], "name": str(a.get("name") or a["id"])[:60]})
    with state.prefs_lock:
        state.prefs["quick"][dev_id] = clean
        save_prefs()
    return reply(True, f"saved {len(clean)} quick-launch app(s)", apps=clean)


@app.put("/api/prefs/session/<dev_id>")
def api_prefs_session(dev_id):
    """Pin which Jellyfin session belongs to a TV (chosen from what Jellyfin actually lists)."""
    device(dev_id)
    body = json_body()
    rule = {k: str(body[k]).strip()[:120] for k in ("client", "device", "device_id") if str(body.get(k) or "").strip()}
    if not rule:
        raise DeviceError("give a client, device or device_id")
    with state.prefs_lock:
        state.prefs["session"][dev_id] = rule
        save_prefs()
    apply_session_overrides()
    return reply(True, f"{state.devices[dev_id].name} will use sessions matching {rule}", rule=rule)


@app.delete("/api/prefs/session/<dev_id>")
def api_prefs_session_reset(dev_id):
    with state.prefs_lock:
        state.prefs["session"].pop(dev_id, None)
        save_prefs()
    apply_session_overrides()
    return reply(True, "back to the rule in devices.json")


@app.delete("/api/prefs/play/<dev_id>")
def api_prefs_play_reset(dev_id):
    """Forget that a device's Jellyfin app cannot be controlled, so the session route is tried again."""
    with state.prefs_lock:
        state.prefs["play"].pop(dev_id, None)
        save_prefs()
    apply_session_overrides()
    return reply(True, "will try the Jellyfin app's session again")


@app.delete("/api/prefs/quick/<dev_id>")
def api_prefs_quick_reset(dev_id):
    with state.prefs_lock:
        state.prefs["quick"].pop(dev_id, None)
        save_prefs()
    return reply(True, "reset to the defaults")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/api/config")
def api_config():
    icons = Path(STATIC) / "icons"
    return jsonify(jellyfinUrl=state.jf.url if state.jf else None,
                   iconFiles=sorted(f.name for f in icons.glob("*") if f.suffix in (".png", ".svg", ".webp", ".jpg")),
                   jellyfinPublicUrl=(state.public_url or (state.jf.url if state.jf else None)),
                   configPath=str(state.config_path), configError=state.config_error,
                   devices=[d.describe() for d in state.devices.values()])


@app.post("/api/reload")
def api_reload():
    reload_devices()
    return api_config()


@app.get("/api/log")
def api_log():
    entries = state.logbook.since(int(request.args.get("since", 0)))
    return jsonify(entries=entries, last=entries[-1]["id"] if entries else int(request.args.get("since", 0)), build=BUILD)


@app.delete("/api/log")
def api_log_clear():
    state.logbook.clear()
    return reply(True, "cleared")


@app.get("/api/log/download")
def api_log_download():
    lines = "\n".join(json.dumps(e) for e in state.logbook.dump())
    return Response(lines + "\n", mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=remote-control-log.jsonl"})


# ------------------------------------------------------------------ devices
@app.get("/api/devices/<dev_id>/status")
def api_device_status(dev_id):
    d = device(dev_id)
    poll = request.args.get("poll") == "1"
    st = d.status(quiet=poll)
    st["mac"] = d.mac
    key = (st["online"], st.get("power"), st.get("app"))
    if poll:
        if state.last_status.get(dev_id) != key:
            previous = state.last_status.get(dev_id)
            state.last_status[dev_id] = key
            detail = (f"online={st['online']} power={st.get('power')} app={st.get('appName') or st.get('app')}"
                      + (f" ({st['error']})" if st.get("error") else ""))
            state.logbook.add("system", dev_id, "status change" if previous else "status (first)", response=detail, ok=st["online"])
    else:
        state.last_status[dev_id] = key
    return jsonify(st)


@app.post("/api/devices/<dev_id>/key")
def api_device_key(dev_id):
    d = device(dev_id)
    body = json_body()
    if body.get("native"):
        native = str(body["native"])
        if not d.valid_native_key(native):
            raise DeviceError("bad key")
    else:
        native = d.keymap.get(body.get("key"))
        if not native:
            raise DeviceError(f'{d.name} has no "{body.get("key")}" key')
    count = max(1, min(int(body.get("count") or 1), 30))
    d.send_key(native, count)
    return reply(True, native if count == 1 else f"{native} x{count}")


@app.post("/api/devices/<dev_id>/power")
def api_device_power(dev_id):
    d = device(dev_id)
    if json_body().get("state") == "on":
        result = orchestrator.power_on(d, state.logbook)
        return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"])
    return reply(True, d.sleep())


@app.post("/api/devices/<dev_id>/sleep")
def api_device_sleep(dev_id):
    return reply(True, device(dev_id).sleep(json_body().get("mode")))


@app.post("/api/devices/<dev_id>/screensaver")
def api_device_screensaver(dev_id):
    return reply(True, device(dev_id).screensaver())


@app.post("/api/devices/<dev_id>/probe")
def api_device_probe(dev_id):
    result = device(dev_id).probe()
    return reply(True, result["summary"], ports=result["ports"], ping=result["ping"])


@app.post("/api/devices/<dev_id>/diagnose")
def api_device_diagnose(dev_id):
    return reply(True, device(dev_id).diagnose())


@app.post("/api/devices/<dev_id>/wol")
def api_device_wol(dev_id):
    info = device(dev_id).wol()
    return reply(True, "magic packet sent to " + ", ".join(info["targets"]))


@app.post("/api/devices/<dev_id>/detect-mac")
def api_device_detect_mac(dev_id):
    d = device(dev_id)
    mac = d.detect_mac()
    return reply(True, f'{mac} - add "mac": "{mac}" to {d.id} in devices.json to keep it', mac=mac)


@app.post("/api/devices/<dev_id>/pair/start")
def api_device_pair_start(dev_id):
    d = device(dev_id)
    if not hasattr(d, "pair_start"):
        raise DeviceError(f"{d.name} does not need pairing")
    return reply(True, d.pair_start())


@app.post("/api/devices/<dev_id>/pair/finish")
def api_device_pair_finish(dev_id):
    d = device(dev_id)
    if not hasattr(d, "pair_finish"):
        raise DeviceError(f"{d.name} does not need pairing")
    return reply(True, d.pair_finish(str(json_body().get("code", ""))))


@app.get("/api/devices/<dev_id>/icon/<app_id>")
def api_device_icon(dev_id, app_id):
    d = device(dev_id)
    key = (dev_id, app_id)
    if key not in state.icons:
        try:
            state.icons[key] = d.icon(app_id)
        except DeviceError:
            return "", 404
    data, mime = state.icons[key]
    return Response(data, mimetype=mime, headers={"Cache-Control": "max-age=86400"})


@app.post("/api/devices/<dev_id>/text")
def api_device_text(dev_id):
    device(dev_id).text(str(json_body().get("text", "")))
    return reply(True, "sent")


@app.get("/api/devices/<dev_id>/apps")
def api_device_apps(dev_id):
    """Installed apps. Listing them can be slow (ADB), so the result is reused for a while."""
    d = device(dev_id)
    cached = state.apps_cache.get(dev_id)
    if cached and time.time() - cached[0] < APPS_TTL and request.args.get("refresh") != "1":
        return jsonify(cached[1])
    apps = d.apps()
    state.apps_cache[dev_id] = (time.time(), apps)
    return jsonify(apps)


@app.post("/api/devices/<dev_id>/launch")
def api_device_launch(dev_id):
    body = json_body()
    out = device(dev_id).launch(str(body.get("app", "")), body.get("params"))
    return reply(True, out or "launched")


@app.get("/api/devices/<dev_id>/screenshot")
def api_device_screenshot(dev_id):
    d = device(dev_id)
    if "screenshot" not in d.capabilities:
        raise DeviceError(f"{d.name} does not support screenshots")
    return Response(d.screenshot(), mimetype="image/png")


@app.post("/api/devices/<dev_id>/connect")
def api_device_connect(dev_id):
    d = device(dev_id)
    if not hasattr(d, "connect"):
        raise DeviceError(f"{d.name} does not need a connect step")
    return reply(True, d.connect())


@app.post("/api/devices/<dev_id>/prepare")
def api_device_prepare(dev_id):
    result = orchestrator.prepare(device(dev_id), jf(), state.logbook)
    return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"], session=result["session"])


def play_params(d):
    params = {"playCommand": d["playCommand"], "itemIds": ",".join(d.get("itemIds") or [])}
    for key in ("startPositionTicks", "startIndex", "audioStreamIndex", "subtitleStreamIndex", "mediaSourceId"):
        if d.get(key) not in (None, ""):
            params[key] = d[key]
    return params


def play_by_url(d, body, prior_steps=()):
    """Hand the device a stream URL (no Jellyfin app or session involved)."""
    if body["playCommand"] != "PlayNow":
        raise DeviceError("Stream playback supports Play now only (there is no queue)")
    ids, index = body["itemIds"], int(body.get("startIndex") or 0)
    result = orchestrator.play_url(d, jf(), state.logbook, ids[index] if index < len(ids) else ids[0], body.get("startPositionTicks"))
    steps = list(prior_steps) + result["steps"]
    return reply(result["ok"], "\n".join(steps), steps=steps, confirmed=result["confirmed"], via="url")


def play_by_deeplink(d, body, fallback):
    """Send the device's Jellyfin app a deep link. If the request itself fails, optionally stream instead."""
    if body["playCommand"] != "PlayNow":
        raise DeviceError("A deep link starts playback straight away; there is no queue")
    ids, index = body["itemIds"], int(body.get("startIndex") or 0)
    item_id = ids[index] if index < len(ids) else ids[0]
    result = orchestrator.play_deeplink(d, jf(), state.logbook, item_id, body.get("startPositionTicks"))
    if not result["ok"] and result.get("retryable") and fallback:
        note = "The deep link request failed, so the movie is streamed directly instead"
        state.logbook.add("system", d.id, "play", response=note, ok=True)
        return play_by_url(d, body, result["steps"] + [note])
    return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"], confirmed=result["confirmed"], via="deeplink")


def learn_url_play(d):
    with state.prefs_lock:
        state.prefs["play"][d.id] = "url"
        save_prefs()
    apply_session_overrides()


@app.post("/api/devices/<dev_id>/play")
def api_device_play(dev_id):
    """Optionally prepare the device, then play. Via the device's Jellyfin session, or (for devices that can) a stream URL.

    In auto mode the Jellyfin app is tried first. If its session reports it cannot be remote controlled, the movie is
    streamed instead, and that is remembered so the next play skips straight to streaming."""
    d = device(dev_id)
    body = json_body()
    if body.get("playCommand") not in PLAY_COMMANDS or not body.get("itemIds"):
        raise DeviceError("bad playCommand or itemIds")
    mode = body.get("via") or d.play_mode()
    can_stream, can_link = "url_play" in d.capabilities, "deeplink" in d.capabilities
    if mode == "deeplink" and not can_link:
        raise DeviceError(f"{d.name} cannot be sent a deep link")
    if mode == "auto" and can_link:
        mode = "deeplink+"   # deep link first; stream only if the request itself fails
    if mode in ("deeplink", "deeplink+"):
        return play_by_deeplink(d, body, fallback=(mode == "deeplink+" and can_stream))
    if mode == "url" and not can_stream:
        raise DeviceError(f"{d.name} cannot play from a stream URL")
    if mode == "auto" and not can_stream:
        mode = "session"
    if mode == "url":
        return play_by_url(d, body)
    steps = []
    if body.get("prepare", True):
        result = orchestrator.prepare(d, jf(), state.logbook)
        steps = result["steps"]
        if not result["ok"]:
            if mode == "auto" and body["playCommand"] == "PlayNow" and result.get("reason") in ("not_remote", "no_match"):
                note = ("The Jellyfin app on this device cannot be remote controlled, so it is streamed directly instead"
                        if result["reason"] == "not_remote" else
                        "No usable Jellyfin session appeared, so the movie is streamed directly instead")
                state.logbook.add("system", dev_id, "play", response=note, ok=True)
                if result["reason"] == "not_remote":
                    learn_url_play(d)
                out = play_by_url(d, body, steps + [note])
                payload = out.get_json()
                payload["learned"] = result["reason"] == "not_remote"
                return jsonify(payload)
            return reply(False, "\n".join(steps), steps=steps)
        session = result["session"]
    else:
        found = orchestrator.device_sessions(jf(), d)
        if not found:
            if mode == "auto" and body["playCommand"] == "PlayNow":
                return play_by_url(d, body, ["No Jellyfin session is open on this device, so the movie is streamed directly"])
            raise JellyfinError(f"No Jellyfin session for {d.name}. Tick 'prepare device first' or open the app.")
        session = summarize_session(found[0])
    r = jf().play(session["Id"], play_params(body))
    steps.append(f'{body["playCommand"]} -> HTTP {r.status_code}')
    confirmed = None
    if r.ok and body["playCommand"] == "PlayNow":
        ids = body["itemIds"]
        index = int(body.get("startIndex") or 0)
        confirmed, title, secs = orchestrator.confirm_playing(jf(), session["Id"], ids[index] if index < len(ids) else ids[0])
        steps.append(f"Playback confirmed: {title} (after {secs}s)" if confirmed else
                     f"The command was accepted but the session never reported the item playing within {secs}s")
        state.logbook.add("system", dev_id, "play", response=steps[-1], ok=confirmed)
    return reply(r.ok, "\n".join(steps), r.status_code, steps=steps, sessionId=session["Id"], confirmed=confirmed)


# ------------------------------------------------------------------ jellyfin
@app.get("/api/jf/sessions")
def api_jf_sessions():
    try:
        raw = jf().sessions(quiet=True)
    except JellyfinError as e:
        state.logbook.poll_failed("jf-sessions", "jellyfin", None, "sessions poll", "GET /Sessions", 0, str(e))
        raise
    state.logbook.poll_ok("jf-sessions", "jellyfin", None, "sessions poll")
    rules = {d.id: orchestrator.effective_rule(d, raw) for d in state.devices.values()}
    out = []
    for s in raw:
        summary = summarize_session(s)
        summary["deviceId"] = next((did for did, rule in rules.items() if match_session(rule, s)), None)
        out.append(summary)
    return jsonify(out)


@app.get("/api/jf/search")
def api_jf_search():
    return jsonify(jf().search(request.args.get("q", "").strip()))


@app.get("/api/jf/episodes/<series_id>")
def api_jf_episodes(series_id):
    if not valid_id(series_id):
        raise JellyfinError("bad id")
    return jsonify(jf().episodes(series_id))


def session_id(body):
    sid = body.get("sessionId")
    if not valid_id(sid):
        raise JellyfinError("bad or missing sessionId")
    return sid


@app.post("/api/jf/play")
def api_jf_play():
    body = json_body()
    if body.get("playCommand") not in PLAY_COMMANDS:
        raise JellyfinError("bad playCommand")
    return relay(jf().play(session_id(body), play_params(body)))


@app.post("/api/jf/playstate")
def api_jf_playstate():
    body = json_body()
    if body.get("command") not in PLAYSTATE_COMMANDS:
        raise JellyfinError("bad command")
    params = {}
    if body.get("seekPositionTicks") is not None:
        params["seekPositionTicks"] = int(body["seekPositionTicks"])
    return relay(jf().playstate(session_id(body), body["command"], params))


@app.post("/api/jf/command")
def api_jf_command():
    body = json_body()
    if not body.get("command"):
        raise JellyfinError("missing command")
    payload = {"Name": body["command"]}
    if body.get("arguments"):
        payload["Arguments"] = body["arguments"]
    return relay(jf().command(session_id(body), payload))


@app.post("/api/jf/viewing")
def api_jf_viewing():
    body = json_body()
    item_id = body.get("itemId")
    if not valid_id(item_id):
        raise JellyfinError("bad itemId")
    # Newer servers require itemName as well as itemType; look both up from the item.
    item = jf().item(item_id)
    params = {"itemId": item_id,
              "itemType": item.get("Type") or body.get("itemType") or "Movie",
              "itemName": item.get("Name") or body.get("itemName") or "Item"}
    return relay(jf().viewing(session_id(body), params))


@app.post("/api/jf/message")
def api_jf_message():
    body = json_body()
    payload = {"Header": body.get("header") or "", "Text": body.get("text") or "",
               "TimeoutMs": int(body.get("timeoutMs") or 5000)}
    return relay(jf().message(session_id(body), payload))


# ------------------------------------------------------------------ entry point
def main():
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="Whole-house remote: TVs + Jellyfin from one web UI")
    p.add_argument("--url", default=os.environ.get("JELLYFIN_URL"))
    p.add_argument("--api-key", default=os.environ.get("JELLYFIN_API_KEY"))
    p.add_argument("--user-id", default=os.environ.get("JELLYFIN_USER_ID", ""),
                   help="user for library search (default: first admin)")
    p.add_argument("--config", default=os.environ.get("REMOTE_DEVICES", str(root / "devices.json")),
                   help="device list (see devices.example.json)")
    p.add_argument("--adb", default=os.environ.get("ADB_PATH", "adb"), help="path to the adb binary")
    p.add_argument("--adb-host", default=os.environ.get("ADB_HOST", ""),
                   help="shortcut: use one Google TV at this address when no device file exists")
    p.add_argument("--log-file", default=os.environ.get("REMOTE_LOG", "remote_control.log"),
                   help="also write every log entry here as JSON lines ('' to disable)")
    p.add_argument("--cert-dir", default=os.environ.get("REMOTE_CERT_DIR", str(root / ".remote_control" / "certs")),
                   help="where the Remote-protocol client certificate (from pairing) is kept")
    p.add_argument("--prefs", default=os.environ.get("REMOTE_PREFS", str(root / ".remote_control" / "prefs.json")),
                   help="where choices made in the UI (quick-launch apps) are saved")
    p.add_argument("--public-url", default=os.environ.get("JELLYFIN_PUBLIC_URL", ""),
                   help="Jellyfin address a phone should open (defaults to --url)")
    p.add_argument("--token", default=os.environ.get("REMOTE_TOKEN", ""),
                   help="require this secret (open /?token=... once per device); use it with --host 0.0.0.0")
    p.add_argument("--host", default=os.environ.get("REMOTE_HOST", "0.0.0.0"),
                   help="address to listen on (default 0.0.0.0: reachable from other devices; use 127.0.0.1 for this computer only)")
    p.add_argument("--port", type=int, default=int(os.environ.get("REMOTE_PORT", 5000)))
    a = p.parse_args()

    state.logbook = LogBook(path=a.log_file or None)
    state.options = {"adb": a.adb, "cert_dir": a.cert_dir}
    state.token, state.public_url = a.token, a.public_url.rstrip("/")
    state.prefs_path = a.prefs
    load_prefs()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.token:
        warning = ("Listening beyond this computer with no --token: anyone on your network can control "
                   "these devices. Add --token SECRET.")
        print("WARNING:", warning)
        state.logbook.add("system", None, "security", ok=False, response=warning)
    state.config_path = Path(a.config)
    if a.url and a.api_key:
        state.jf = JellyfinClient(a.url, a.api_key, state.logbook, a.user_id)
    else:
        state.logbook.add("system", None, "config", ok=False,
                          response="Jellyfin not configured; device remotes work, playback panels will not.")
    if not state.config_path.exists() and a.adb_host:
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / "devices.json"
        tmp.write_text(json.dumps({"devices": [{"id": "tv", "name": "Google TV", "type": "androidtv", "host": a.adb_host}]}))
        state.config_path = tmp
    reload_devices()
    app.run(host=a.host, port=a.port, threaded=True)
