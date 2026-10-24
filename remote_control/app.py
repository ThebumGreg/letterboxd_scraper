"""Flask routes for the whole-house remote."""
import argparse
import hmac
import json
import os
import re
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

app = Flask(__name__, static_folder=STATIC, static_url_path="/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
state = SimpleNamespace(logbook=LogBook(), jf=None, devices={}, config_path=None, config_error=None,
                        options={}, last_status={}, token="", public_url="", icons={})


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
    state.devices, state.config_error = load_devices(state.config_path, state.logbook, state.options)
    state.last_status.clear()
    state.logbook.add("system", None, "config", ok=state.config_error is None,
                      response=state.config_error or f"Loaded {len(state.devices)} device(s) from {state.config_path}")


# ------------------------------------------------------------------ pages / config / log
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


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
    return jsonify(entries=entries, last=entries[-1]["id"] if entries else int(request.args.get("since", 0)))


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
    d.send_key(native)
    return reply(True, native)


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
    return jsonify(device(dev_id).apps())


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


@app.post("/api/devices/<dev_id>/play")
def api_device_play(dev_id):
    """Optionally prepare the device, then PlayNow/Next/Last on its Jellyfin session."""
    d = device(dev_id)
    body = json_body()
    if body.get("playCommand") not in PLAY_COMMANDS or not body.get("itemIds"):
        raise DeviceError("bad playCommand or itemIds")
    steps = []
    if body.get("prepare", True):
        result = orchestrator.prepare(d, jf(), state.logbook)
        steps = result["steps"]
        if not result["ok"]:
            return reply(False, "\n".join(steps), steps=steps)
        session = result["session"]
    else:
        found = orchestrator.device_sessions(jf(), d)
        if not found:
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
    out = []
    for s in raw:
        summary = summarize_session(s)
        summary["deviceId"] = next((d.id for d in state.devices.values() if match_session(d.session_rule(), s)), None)
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
    p.add_argument("--public-url", default=os.environ.get("JELLYFIN_PUBLIC_URL", ""),
                   help="Jellyfin address a phone should open (defaults to --url)")
    p.add_argument("--token", default=os.environ.get("REMOTE_TOKEN", ""),
                   help="require this secret (open /?token=... once per device); use it with --host 0.0.0.0")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5055)
    a = p.parse_args()

    state.logbook = LogBook(path=a.log_file or None)
    state.options = {"adb": a.adb, "cert_dir": a.cert_dir}
    state.token, state.public_url = a.token, a.public_url.rstrip("/")
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
