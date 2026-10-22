#!/usr/bin/env python3
"""Small web UI for exercising Jellyfin's Sessions remote-control API.

Lists active sessions (e.g. Swiftfin tvOS), searches the library, and sends
play / transport / general commands to the selected session. Every request and
the server's response is shown in a log so you can see what a client accepts.
The API key stays on the server; the browser only talks to this app.

For a Google TV / Android TV it can also drive the device over ADB (wake,
launch the Jellyfin app, remote keys, screenshot) and then hand off to the
Sessions API, so a sleeping TV can be woken and told what to play in one click.
That needs the `adb` binary and "Network debugging" enabled on the TV.

  export JELLYFIN_URL=http://192.168.128.55:5000
  export JELLYFIN_API_KEY=xxxxxxxx
  python remote_control_app.py [--adb-host 192.168.1.50]   # http://127.0.0.1:5055
"""
import argparse
import os
import re
import subprocess
import time

import requests
from flask import Flask, Response, jsonify, request

TIMEOUT = 10
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
PLAYSTATE_COMMANDS = {
    "Stop", "Pause", "Unpause", "NextTrack", "PreviousTrack",
    "Seek", "Rewind", "FastForward", "PlayPause",
}
PLAY_COMMANDS = {"PlayNow", "PlayNext", "PlayLast", "PlayInstantMix", "PlayShuffle"}

app = Flask(__name__)
cfg = {"url": "", "key": "", "user_id": "", "adb": "adb", "adb_target": "", "package": ""}

ADB_TARGET_RE = re.compile(r"^[\w.\-]+(:\d+)?$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
KEY_RE = re.compile(r"^(KEYCODE_[A-Z0-9_]+|\d{1,3})$")
TEXT_RE = re.compile(r"^[A-Za-z0-9 ._@-]{1,100}$")


def auth_headers():
    return {
        "Accept": "application/json",
        "Authorization": (
            'MediaBrowser Client="RemoteControlApp", Device="Web", '
            f'DeviceId="remote-control-app-01", Version="1.0.0", Token="{cfg["key"]}"'
        ),
    }


def jf(method, path, **kw):
    return requests.request(method, f'{cfg["url"]}{path}', headers=auth_headers(), timeout=TIMEOUT, **kw)


def relay(resp):
    """Shape a Jellyfin response for the UI log."""
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = resp.text[:500]
    return jsonify(status=resp.status_code, ok=resp.ok, body=body)


def valid_session(data):
    sid = (data or {}).get("sessionId", "")
    if not SESSION_ID_RE.match(sid):
        return None
    return sid


def lookup_user_id():
    """Library queries need a user; use the configured one or the first admin."""
    if cfg["user_id"]:
        return cfg["user_id"]
    users = jf("GET", "/Users").json()
    admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), None)
    cfg["user_id"] = (admin or users[0])["Id"]
    return cfg["user_id"]


def item_summary(i):
    return {
        "Id": i.get("Id"),
        "Name": i.get("Name"),
        "Type": i.get("Type"),
        "Year": i.get("ProductionYear"),
        "SeriesName": i.get("SeriesName"),
        "Season": i.get("ParentIndexNumber"),
        "Episode": i.get("IndexNumber"),
        "RunTimeTicks": i.get("RunTimeTicks"),
    }


@app.errorhandler(requests.RequestException)
def upstream_error(e):
    return jsonify(status=0, ok=False, body=f"Could not reach Jellyfin: {e}"), 502


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/api/config")
def api_config():
    return jsonify(url=cfg["url"], adbTarget=cfg["adb_target"], package=cfg["package"])


@app.get("/api/sessions")
def api_sessions():
    r = jf("GET", "/Sessions")
    r.raise_for_status()
    out = []
    for s in r.json():
        now = s.get("NowPlayingItem") or {}
        out.append({
            "Id": s.get("Id"),
            "Client": s.get("Client"),
            "DeviceName": s.get("DeviceName"),
            "UserName": s.get("UserName"),
            "SupportsRemoteControl": s.get("SupportsRemoteControl"),
            "SupportedCommands": s.get("SupportedCommands") or [],
            "LastActivityDate": s.get("LastActivityDate"),
            "NowPlaying": item_summary(now) if now else None,
            "PlayState": s.get("PlayState") or {},
        })
    return jsonify(out)


@app.get("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    r = jf("GET", "/Items", params={
        "userId": lookup_user_id(),
        "searchTerm": q,
        "recursive": "true",
        "includeItemTypes": "Movie,Series,Episode",
        "limit": 25,
        "fields": "ProductionYear",
    })
    r.raise_for_status()
    return jsonify([item_summary(i) for i in r.json().get("Items", [])])


@app.get("/api/episodes/<series_id>")
def api_episodes(series_id):
    if not SESSION_ID_RE.match(series_id):
        return jsonify(error="bad id"), 400
    r = jf("GET", f"/Shows/{series_id}/Episodes", params={"userId": lookup_user_id()})
    r.raise_for_status()
    return jsonify([item_summary(i) for i in r.json().get("Items", [])])


def play_params(d):
    params = {
        "playCommand": d["playCommand"],
        "itemIds": ",".join(d.get("itemIds") or []),
    }
    for key in ("startPositionTicks", "startIndex", "audioStreamIndex", "subtitleStreamIndex", "mediaSourceId"):
        if d.get(key) not in (None, ""):
            params[key] = d[key]
    return params


@app.post("/api/play")
def api_play():
    d = request.get_json(force=True)
    sid = valid_session(d)
    if not sid or d.get("playCommand") not in PLAY_COMMANDS:
        return jsonify(status=0, ok=False, body="bad sessionId or playCommand"), 400
    return relay(jf("POST", f"/Sessions/{sid}/Playing", params=play_params(d)))


@app.post("/api/playstate")
def api_playstate():
    d = request.get_json(force=True)
    sid = valid_session(d)
    cmd = d.get("command")
    if not sid or cmd not in PLAYSTATE_COMMANDS:
        return jsonify(status=0, ok=False, body="bad sessionId or command"), 400
    params = {}
    if d.get("seekPositionTicks") is not None:
        params["seekPositionTicks"] = int(d["seekPositionTicks"])
    return relay(jf("POST", f"/Sessions/{sid}/Playing/{cmd}", params=params))


@app.post("/api/command")
def api_command():
    d = request.get_json(force=True)
    sid = valid_session(d)
    if not sid or not d.get("command"):
        return jsonify(status=0, ok=False, body="bad sessionId or command"), 400
    payload = {"Name": d["command"]}
    if d.get("arguments"):
        payload["Arguments"] = d["arguments"]
    return relay(jf("POST", f"/Sessions/{sid}/Command", json=payload))


@app.post("/api/viewing")
def api_viewing():
    d = request.get_json(force=True)
    sid = valid_session(d)
    if not sid or not d.get("itemId"):
        return jsonify(status=0, ok=False, body="bad sessionId or itemId"), 400
    if not SESSION_ID_RE.match(d["itemId"]):
        return jsonify(status=0, ok=False, body="bad itemId"), 400
    # Newer servers require itemName as well as itemType; look both up from the item.
    item = jf("GET", f"/Items/{d['itemId']}", params={"userId": lookup_user_id()})
    item = item.json() if item.ok else {}
    params = {
        "itemId": d["itemId"],
        "itemType": item.get("Type") or d.get("itemType") or "Movie",
        "itemName": item.get("Name") or d.get("itemName") or "Item",
    }
    return relay(jf("POST", f"/Sessions/{sid}/Viewing", params=params))


@app.post("/api/message")
def api_message():
    d = request.get_json(force=True)
    sid = valid_session(d)
    if not sid:
        return jsonify(status=0, ok=False, body="bad sessionId"), 400
    payload = {"Header": d.get("header") or "", "Text": d.get("text") or "",
               "TimeoutMs": int(d.get("timeoutMs") or 5000)}
    return relay(jf("POST", f"/Sessions/{sid}/Message", json=payload))


# ---------------------------------------------------------------- ADB (Google TV)

class AdbError(Exception):
    pass


@app.errorhandler(AdbError)
def adb_error(e):
    return jsonify(status=500, ok=False, body=str(e)), 502


def adb(*args, connect=False, timeout=15):
    """Run adb against the configured TV and return stdout bytes."""
    if not connect and not cfg["adb_target"]:
        raise AdbError("Enter the TV's address and press Connect first.")
    cmd = [cfg["adb"]] + ([] if connect else ["-s", cfg["adb_target"]]) + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise AdbError(f'adb not found ("{cfg["adb"]}"). Install it (sudo apt install adb) or pass --adb PATH.')
    except subprocess.TimeoutExpired:
        raise AdbError("adb timed out")
    if p.returncode != 0:
        err = (p.stderr or p.stdout).decode(errors="replace").strip()
        if "unauthorized" in err:
            err += " - accept the 'Allow USB debugging' prompt on the TV"
        raise AdbError(err or f"adb exited with {p.returncode}")
    return p.stdout


def adb_text(*args, **kw):
    return adb(*args, **kw).decode(errors="replace").strip()


def adb_state():
    """Wakefulness and the focused app package."""
    adb("get-state")
    out = adb_text("shell", "dumpsys power | grep -m1 mWakefulness=; dumpsys window | grep -m1 mCurrentFocus")
    wake = re.search(r"mWakefulness=(\w+)", out)
    focus = re.search(r"mCurrentFocus=.*?([A-Za-z][\w]*(?:\.\w+)+)/", out)
    return {"awake": wake.group(1) if wake else None, "app": focus.group(1) if focus else None}


def launch_app(package):
    for category in ("android.intent.category.LEANBACK_LAUNCHER", "android.intent.category.LAUNCHER"):
        out = adb_text("shell", "monkey", "-p", package, "-c", category, "1")
        if "No activities found" not in out and "monkey aborted" not in out:
            return out
    raise AdbError(f"No launchable activity found for {package}")


def adb_ok(body):
    return jsonify(status=200, ok=True, body=body)


@app.post("/api/adb/connect")
def api_adb_connect():
    target = (request.get_json(force=True).get("target") or "").strip()
    if not ADB_TARGET_RE.match(target):
        return jsonify(status=0, ok=False, body="bad address"), 400
    if ":" not in target:
        target += ":5555"
    cfg["adb_target"] = target
    out = adb_text("connect", target, connect=True)
    if not out.startswith(("connected", "already connected")):
        return jsonify(status=500, ok=False, body=out)
    state = adb_state()  # raises with a hint if the TV hasn't authorised us yet
    return adb_ok(f"{out}\nawake={state['awake']} app={state['app']}")


@app.get("/api/adb/status")
def api_adb_status():
    try:
        return jsonify(connected=True, target=cfg["adb_target"], **adb_state())
    except AdbError as e:
        return jsonify(connected=False, error=str(e))


@app.post("/api/adb/key")
def api_adb_key():
    key = str(request.get_json(force=True).get("key", ""))
    if not KEY_RE.match(key):
        return jsonify(status=0, ok=False, body="bad key"), 400
    adb("shell", "input", "keyevent", key)
    return adb_ok(key)


@app.post("/api/adb/text")
def api_adb_text():
    text = request.get_json(force=True).get("text", "")
    if not TEXT_RE.match(text):
        return jsonify(status=0, ok=False, body="text may only use letters, digits, space . _ @ -"), 400
    adb("shell", "input", "text", text.replace(" ", "%s"))
    return adb_ok(text)


def package_arg(d):
    pkg = (d or {}).get("package", "")
    if not PACKAGE_RE.match(pkg):
        raise AdbError("bad package name")
    return pkg


@app.post("/api/adb/launch")
def api_adb_launch():
    return adb_ok(launch_app(package_arg(request.get_json(force=True))))


@app.post("/api/adb/stop")
def api_adb_stop():
    adb("shell", "am", "force-stop", package_arg(request.get_json(force=True)))
    return adb_ok("stopped")


@app.get("/api/adb/packages")
def api_adb_packages():
    out = adb_text("shell", "pm", "list", "packages", "-3")
    return jsonify(sorted(l.split(":", 1)[1] for l in out.splitlines() if l.startswith("package:")))


@app.get("/api/adb/screenshot")
def api_adb_screenshot():
    return Response(adb("exec-out", "screencap", "-p", timeout=20), mimetype="image/png")


@app.post("/api/adb/wake_play")
def api_adb_wake_play():
    """Wake the TV, make sure the Jellyfin app is up, wait for its session, then play."""
    d = request.get_json(force=True)
    package = package_arg(d)
    if d.get("playCommand") not in PLAY_COMMANDS or not d.get("itemIds"):
        return jsonify(status=0, ok=False, body="bad playCommand or itemIds"), 400
    match = (d.get("clientMatch") or "").lower()
    steps = []

    def sessions():
        found = [s for s in jf("GET", "/Sessions").json()
                 if s.get("SupportsRemoteControl") and match in f'{s.get("Client")} {s.get("DeviceName")}'.lower()]
        return sorted(found, key=lambda s: s.get("LastActivityDate") or "", reverse=True)

    state = adb_state()
    if state["awake"] != "Awake":
        adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        steps.append(f'Sent WAKEUP (was {state["awake"]})')
        time.sleep(2)
        state = adb_state()

    target = None
    if state["app"] == package:
        steps.append("App already in the foreground")
        target = (sessions() or [None])[0]
    else:
        # A session left over from an earlier run may still be listed; wait for one
        # that is new or has reported activity since launch.
        baseline = {s["Id"]: s.get("LastActivityDate") for s in sessions()}
        launch_app(package)
        steps.append(f"Launched {package} (was {state['app']})")
        deadline = time.time() + 40
        while time.time() < deadline and not target:
            time.sleep(2)
            fresh = [s for s in sessions() if baseline.get(s["Id"]) != s.get("LastActivityDate")]
            target = fresh[0] if fresh else None

    if not target:
        steps.append(f'No Jellyfin session matching "{match}" with remote control appeared')
        return jsonify(status=0, ok=False, body="\n".join(steps))
    steps.append(f'Session {target.get("Client")} / {target.get("DeviceName")}')
    time.sleep(1)  # let the client finish registering before the first command
    r = jf("POST", f'/Sessions/{target["Id"]}/Playing', params=play_params(d))
    steps.append(f'{d["playCommand"]} -> HTTP {r.status_code}')
    return jsonify(status=r.status_code, ok=r.ok, body="\n".join(steps), sessionId=target["Id"])


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jellyfin Remote Test</title>
<style>
:root{--bg:#101418;--card:#1a2027;--line:#2c3540;--fg:#e6e9ed;--dim:#8d99a6;--acc:#4da3ff;--ok:#3fb950;--bad:#f85149;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px;padding:12px 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);margin:0 0 8px}
button,select,input,textarea{font:inherit;color:var(--fg);background:#232b34;border:1px solid var(--line);border-radius:5px;padding:5px 9px}
button{cursor:pointer}button:hover{border-color:var(--acc)}
button.unsup{opacity:.45;border-style:dashed}
input,textarea{background:#0e1216}
.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.row>input{flex:1;min-width:90px}
.dim{color:var(--dim)}.small{font-size:12px}
.chip{display:inline-block;background:#232b34;border-radius:10px;padding:1px 8px;margin:2px;font-size:11px}
.bar{height:6px;background:#0e1216;border-radius:3px;overflow:hidden;margin:6px 0}
.bar>div{height:100%;background:var(--acc)}
.item{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:5px 0;border-top:1px solid var(--line)}
.item .name{flex:1;min-width:160px}
.eps{margin-left:16px}
#log{max-height:420px;overflow:auto;font:12px/1.4 ui-monospace,monospace}
.entry{border-top:1px solid var(--line);padding:4px 0;white-space:pre-wrap;word-break:break-all}
.s2{color:var(--ok)}.s4{color:var(--bad)}.s0{color:var(--bad)}
.wide{grid-column:1/-1}
</style></head><body>
<header>
  <h1>Jellyfin Remote Test</h1>
  <span class="dim small" id="srv"></span>
  <label>Session <select id="sess"></select></label>
  <button onclick="refresh()">Refresh</button>
  <span id="caps" class="small"></span>
</header>
<main>
  <section><h2>Now playing</h2><div id="np" class="dim">No session selected</div></section>

  <section><h2>Transport (Sessions/{id}/Playing/{command})</h2>
    <div class="row" id="transport"></div>
    <div class="row">
      <button onclick="relSeek(-30)">-30s</button><button onclick="relSeek(30)">+30s</button>
      <input id="seekSec" type="number" min="0" placeholder="seek to (s)" style="max-width:130px">
      <button onclick="seekTo()">Seek</button>
    </div>
  </section>

  <section class="wide"><h2>Library &rarr; Play</h2>
    <div class="row">
      <input id="q" placeholder="Search movies / shows / episodes" onkeydown="if(event.key==='Enter')search()">
      <button onclick="search()">Search</button>
    </div>
    <div class="row small dim">Options:
      <input id="optPos" type="number" min="0" placeholder="start (s)" style="max-width:100px">
      <input id="optAud" type="number" min="0" placeholder="audio idx" style="max-width:100px">
      <input id="optSub" type="number" min="-1" placeholder="subtitle idx" style="max-width:110px">
      <input id="ids" placeholder="or paste item id(s), comma separated">
      <button onclick="playIds(splitIds(),'PlayNow')">Now</button>
      <button onclick="playIds(splitIds(),'PlayNext')">Next</button>
      <button onclick="playIds(splitIds(),'PlayLast')">Last</button>
    </div>
    <div id="results"></div>
  </section>

  <section><h2>General commands (Sessions/{id}/Command)</h2>
    <div class="small dim">Dashed = not in the session's SupportedCommands (still clickable, to test).</div>
    <div class="row" id="cmds"></div>
    <div class="row">
      <select id="rawCmd" onchange="fillTemplate()"></select>
      <input id="rawArgs" placeholder='arguments JSON, e.g. {"Volume":"50"}'>
      <button onclick="sendRaw()">Send</button>
    </div>
  </section>

  <section><h2>Message &amp; detail page</h2>
    <div class="row"><input id="mHead" placeholder="Header"><input id="mText" placeholder="Text">
      <input id="mTime" type="number" value="5000" style="max-width:90px"><button onclick="sendMsg()">Show message</button></div>
    <div class="row"><input id="vId" placeholder="Item id"><select id="vType"><option>Movie</option><option>Series</option><option>Episode</option></select>
      <button onclick="viewItem($('#vId').value,$('#vType').value)">Open detail page</button></div>
  </section>

  <section class="wide"><h2>Google TV (ADB)</h2>
    <div class="row">
      <input id="adbHost" placeholder="TV address, e.g. 192.168.1.50 or 192.168.1.50:5555">
      <button onclick="adbConnect()">Connect</button>
      <button onclick="adbStatus()">Status</button>
      <span id="adbSt" class="small dim">not connected</span>
    </div>
    <div class="row">
      <input id="adbPkg" value="org.jellyfin.androidtv" placeholder="Jellyfin app package">
      <button onclick="adbApp('launch')">Launch</button>
      <button onclick="adbApp('stop')">Force stop</button>
      <button onclick="adbPackages()">List apps</button>
      <select id="adbPkgs" style="display:none" onchange="$('#adbPkg').value=this.value"></select>
    </div>
    <div class="row" id="adbKeys"></div>
    <div class="row">
      <input id="adbText" placeholder="Type text on the TV (letters, digits, space . _ @ -)">
      <button onclick="adbType()">Send text</button>
      <button onclick="adbShot()">Screenshot</button>
    </div>
    <div class="row small dim">
      <label>Session match <input id="adbMatch" value="Android TV" style="max-width:180px"></label>
      <label><input type="checkbox" id="wakePlay"> Wake TV + launch app via ADB before playing (Library &rarr; Play buttons)</label>
    </div>
    <img id="shot" alt="TV screenshot" style="display:none;max-width:480px;border:1px solid var(--line);border-radius:6px">
  </section>

  <section class="wide"><h2>Log <button onclick="$('#log').innerHTML=''" class="small">clear</button></h2><div id="log"></div></section>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TICKS = 1e7;
const COMMANDS = ['MoveUp','MoveDown','MoveLeft','MoveRight','PageUp','PageDown','PreviousLetter','NextLetter','ToggleOsd','ToggleContextMenu','Select','Back','TakeScreenshot','SendKey','SendString','GoHome','GoToSettings','VolumeUp','VolumeDown','Mute','Unmute','ToggleMute','SetVolume','SetAudioStreamIndex','SetSubtitleStreamIndex','ToggleFullscreen','DisplayContent','GoToSearch','DisplayMessage','SetRepeatMode','ChannelUp','ChannelDown','Guide','ToggleStats','PlayMediaSource','PlayTrailers','SetShuffleQueue','PlayState','PlayNext','ToggleOsdMenu','Play','SetMaxStreamingBitrate','SetPlaybackOrder'];
const TEMPLATES = {SetVolume:'{"Volume":"50"}',SetAudioStreamIndex:'{"Index":"1"}',SetSubtitleStreamIndex:'{"Index":"-1"}',SetRepeatMode:'{"RepeatMode":"RepeatNone"}',SetShuffleQueue:'{"ShuffleMode":"Shuffle"}',SetMaxStreamingBitrate:'{"Bitrate":"20000000"}',SendString:'{"String":"hello"}',SendKey:'{"Key":"Enter"}',DisplayContent:'{"ItemId":"","ItemType":"Movie","ItemName":""}',DisplayMessage:'{"Header":"Hi","Text":"Test","TimeoutMs":"3000"}',PlayMediaSource:'{"ItemId":"","MediaSourceId":""}'};
const TRANSPORT = ['Pause','Unpause','PlayPause','Stop','PreviousTrack','NextTrack','Rewind','FastForward'];
let sessions = [], sel = localStorage.getItem('sid') || '', sig = '';

function cur(){ return sessions.find(s => s.Id === sel); }
function need(){ if(!sel){ alert('Select a session first'); return false; } return true; }
function log(label, res){
  const st = res.status, cls = 's' + Math.floor(st/100);
  const body = res.body == null ? '' : '\n' + (typeof res.body === 'string' ? res.body : JSON.stringify(res.body));
  $('#log').insertAdjacentHTML('afterbegin', `<div class="entry"><span class="dim">${new Date().toLocaleTimeString()}</span> ${esc(label)} <b class="${cls}">HTTP ${st}</b>${esc(body)}</div>`);
}
async function call(label, path, body){
  let res;
  try {
    const r = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    res = await r.json();
  } catch(e){ res = {status:0, body:String(e)}; }
  if(body !== undefined) log(label, res);
  return res;
}
async function act(label, path, body){
  if(!need()) return;
  const res = await call(label, path, {sessionId: sel, ...body});
  setTimeout(refresh, 700);
  return res;
}

async function refresh(){
  const r = await call('sessions', '/api/sessions');
  if(!Array.isArray(r)){ $('#np').textContent = r.body || 'Cannot reach Jellyfin'; return; }
  sessions = r;
  if(!cur()){ const s = sessions.find(x => /swiftfin/i.test(x.Client||'')) || sessions[0]; sel = s ? s.Id : ''; }
  const nsig = sessions.map(s => s.Id + s.Client + s.DeviceName).join('|') + '#' + sel;
  if(nsig !== sig){
    sig = nsig;
    $('#sess').innerHTML = sessions.map(s => `<option value="${esc(s.Id)}">${esc(s.Client)} - ${esc(s.DeviceName)} (${esc(s.UserName)})</option>`).join('');
    $('#sess').value = sel;
  }
  localStorage.setItem('sid', sel);
  render();
}
function render(){
  const s = cur();
  $('#caps').innerHTML = s ? `<span class="chip">RemoteControl: ${s.SupportsRemoteControl}</span>` + s.SupportedCommands.map(c => `<span class="chip">${esc(c)}</span>`).join('') : '';
  document.querySelectorAll('#cmds button').forEach(b => b.classList.toggle('unsup', !s || !s.SupportedCommands.includes(b.textContent)));
  document.querySelectorAll('#transport button').forEach(b => b.classList.toggle('unsup', !s || !s.SupportedCommands.includes('PlayState')));
  if(!s){ $('#np').textContent = 'No session selected'; return; }
  const n = s.NowPlaying, p = s.PlayState || {};
  if(!n){ $('#np').innerHTML = '<span class="dim">Nothing playing</span>'; return; }
  const pos = (p.PositionTicks||0)/TICKS, len = (n.RunTimeTicks||0)/TICKS;
  $('#np').innerHTML = `<b>${esc(label(n))}</b> ${p.IsPaused ? '(paused)' : ''}
    <div class="bar"><div style="width:${len ? Math.min(100, pos/len*100) : 0}%"></div></div>
    <div class="small dim">${fmt(pos)} / ${fmt(len)} &middot; volume ${esc(p.VolumeLevel)} ${p.IsMuted ? '(muted)' : ''} &middot; audio ${esc(p.AudioStreamIndex)} &middot; subs ${esc(p.SubtitleStreamIndex)} &middot; ${esc(p.PlayMethod)} &middot; repeat ${esc(p.RepeatMode)}</div>`;
}
const fmt = t => { t = Math.round(t); const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60; return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(s).padStart(2,'0'); };
const label = i => i.Type === 'Episode' ? `${i.SeriesName} S${i.Season}E${i.Episode} - ${i.Name}` : i.Name + (i.Year ? ` (${i.Year})` : '');

/* transport */
function playstate(command, seekPositionTicks){ return act('Playing/' + command, '/api/playstate', {command, seekPositionTicks}); }
function seekTo(){ const v = $('#seekSec').value; if(v !== '') playstate('Seek', Math.round(v * TICKS)); }
function relSeek(d){ const s = cur(); if(!s) return need(); playstate('Seek', Math.max(0, Math.round(((s.PlayState.PositionTicks||0)/TICKS + d) * TICKS))); }

/* general commands */
function sendCmd(command, args){ return act('Command/' + command, '/api/command', {command, arguments: args}); }
function fillTemplate(){ $('#rawArgs').value = TEMPLATES[$('#rawCmd').value] || ''; }
function sendRaw(){
  let args; const t = $('#rawArgs').value.trim();
  try { args = t ? JSON.parse(t) : undefined; } catch(e){ return alert('Arguments are not valid JSON'); }
  sendCmd($('#rawCmd').value, args);
}
function sendMsg(){ act('Message', '/api/message', {header:$('#mHead').value, text:$('#mText').value, timeoutMs:$('#mTime').value}); }
function viewItem(itemId, itemType){ if(itemId) act('Viewing ' + itemId, '/api/viewing', {itemId, itemType}); }

/* library */
const splitIds = () => $('#ids').value.split(',').map(x => x.trim()).filter(Boolean);
async function playIds(itemIds, playCommand, startIndex){
  if(!itemIds.length) return;
  const pos = $('#optPos').value;
  const body = {
    itemIds, playCommand, startIndex,
    startPositionTicks: pos === '' ? undefined : Math.round(pos * TICKS),
    audioStreamIndex: $('#optAud').value, subtitleStreamIndex: $('#optSub').value};
  if($('#wakePlay').checked){
    const res = await call(`Wake + launch + ${playCommand}`, '/api/adb/wake_play',
      {...body, package: $('#adbPkg').value, clientMatch: $('#adbMatch').value});
    if(res.sessionId){ sel = res.sessionId; sig = ''; }
    refresh(); adbStatus();
    return res;
  }
  return act(`${playCommand} ${itemIds.length} item(s)`, '/api/play', body);
}
function itemRow(i, extra=''){
  return `<div class="item"><span class="name">${esc(label(i))} <span class="dim small">${esc(i.Type)}</span></span>${extra}
    <button onclick="playIds(['${esc(i.Id)}'],'PlayNow')">Now</button>
    <button onclick="playIds(['${esc(i.Id)}'],'PlayNext')">Next</button>
    <button onclick="playIds(['${esc(i.Id)}'],'PlayLast')">Last</button>
    <button onclick="viewItem('${esc(i.Id)}','${esc(i.Type)}')">Details</button></div>`;
}
async function search(){
  const r = await call('search', '/api/search?q=' + encodeURIComponent($('#q').value));
  if(!Array.isArray(r)){ $('#results').textContent = r.body || 'Search failed'; return; }
  $('#results').innerHTML = r.map(i => `<div>${itemRow(i, i.Type === 'Series' ? `<button onclick="episodes('${esc(i.Id)}',this)">Episodes</button>` : '')}<div class="eps"></div></div>`).join('') || '<span class="dim">No results</span>';
}
async function episodes(id, btn){
  const box = btn.closest('div.item').nextElementSibling;
  const r = await call('episodes', '/api/episodes/' + id);
  if(!Array.isArray(r)) return;
  const ids = r.map(e => e.Id);
  box.innerHTML = r.map((e, n) => itemRow(e, `<button onclick='playIds(${JSON.stringify(ids)},"PlayNow",${n})'>Play from here</button>`)).join('');
}

/* google tv (adb) */
const ADB_KEYS = [['Wake','KEYCODE_WAKEUP'],['Sleep','KEYCODE_SLEEP'],['Home','KEYCODE_HOME'],['Back','KEYCODE_BACK'],['Menu','KEYCODE_MENU'],
  ['Up','KEYCODE_DPAD_UP'],['Down','KEYCODE_DPAD_DOWN'],['Left','KEYCODE_DPAD_LEFT'],['Right','KEYCODE_DPAD_RIGHT'],['OK','KEYCODE_DPAD_CENTER'],
  ['Play/Pause','KEYCODE_MEDIA_PLAY_PAUSE'],['Stop','KEYCODE_MEDIA_STOP'],['Prev','KEYCODE_MEDIA_PREVIOUS'],['Next','KEYCODE_MEDIA_NEXT'],
  ['Rewind','KEYCODE_MEDIA_REWIND'],['FF','KEYCODE_MEDIA_FAST_FORWARD'],['Vol +','KEYCODE_VOLUME_UP'],['Vol -','KEYCODE_VOLUME_DOWN'],['Mute','KEYCODE_VOLUME_MUTE']];
let adbOn = false;
async function adbStatus(){
  const r = await call('adb status', '/api/adb/status');
  adbOn = !!r.connected;
  $('#adbSt').innerHTML = r.connected
    ? `<span class="chip">${esc(r.target)}</span><span class="chip">${esc(r.awake)}</span><span class="chip">app: ${esc(r.app)}</span>`
    : esc(r.error || 'not connected');
}
async function adbPost(label, path, body){ const r = await call(label, path, body); if(r.ok) setTimeout(adbStatus, 600); return r; }
function adbConnect(){
  localStorage.setItem('adbHost', $('#adbHost').value);
  return adbPost('ADB connect', '/api/adb/connect', {target: $('#adbHost').value});
}
const adbKey = key => adbPost('ADB key ' + key, '/api/adb/key', {key});
const adbApp = action => adbPost('ADB ' + action, '/api/adb/' + action, {package: $('#adbPkg').value});
const adbType = () => adbPost('ADB text', '/api/adb/text', {text: $('#adbText').value});
async function adbPackages(){
  const r = await call('adb packages', '/api/adb/packages');
  if(!Array.isArray(r)) return alert(r.body || 'Could not list apps');
  $('#adbPkgs').innerHTML = r.map(p => `<option>${esc(p)}</option>`).join('');
  $('#adbPkgs').style.display = '';
}
function adbShot(){ const i = $('#shot'); i.style.display = ''; i.src = '/api/adb/screenshot?t=' + Date.now(); }
$('#shot').onerror = () => { $('#shot').style.display = 'none'; log('ADB screenshot', {status: 500, body: 'screenshot failed (is the TV connected?)'}); };
$('#adbKeys').innerHTML = ADB_KEYS.map(([l, k]) => `<button onclick="adbKey('${k}')">${l}</button>`).join('');
$('#adbHost').value = localStorage.getItem('adbHost') || '';
setInterval(() => { if(adbOn) adbStatus(); }, 5000);

/* init */
$('#sess').onchange = e => { sel = e.target.value; sig = ''; refresh(); };
$('#transport').innerHTML = TRANSPORT.map(c => `<button onclick="playstate('${c}')">${c}</button>`).join('');
$('#cmds').innerHTML = COMMANDS.filter(c => !TEMPLATES[c]).map(c => `<button onclick="sendCmd('${c}')">${c}</button>`).join('');
$('#rawCmd').innerHTML = COMMANDS.map(c => `<option>${c}</option>`).join('');
fillTemplate();
fetch('/api/config').then(r => r.json()).then(c => {
  $('#srv').textContent = c.url;
  if(c.adbTarget){ $('#adbHost').value = c.adbTarget; adbStatus(); }
  if(c.package) $('#adbPkg').value = c.package;
});
refresh(); setInterval(refresh, 3000);
</script></body></html>
"""


def main():
    p = argparse.ArgumentParser(description="Jellyfin remote control test UI")
    p.add_argument("--url", default=os.environ.get("JELLYFIN_URL"))
    p.add_argument("--api-key", default=os.environ.get("JELLYFIN_API_KEY"))
    p.add_argument("--user-id", default=os.environ.get("JELLYFIN_USER_ID", ""),
                   help="user for library search (default: first admin)")
    p.add_argument("--adb-host", default=os.environ.get("ADB_HOST", ""),
                   help="Google TV address (host or host:port) to preselect for ADB")
    p.add_argument("--adb", default=os.environ.get("ADB_PATH", "adb"), help="path to the adb binary")
    p.add_argument("--package", default=os.environ.get("JELLYFIN_PACKAGE", ""),
                   help="Jellyfin app package on the TV (default org.jellyfin.androidtv)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5055)
    a = p.parse_args()
    if not a.url or not a.api_key:
        raise SystemExit("Set JELLYFIN_URL and JELLYFIN_API_KEY (or pass --url/--api-key).")
    if a.adb_host and not ADB_TARGET_RE.match(a.adb_host):
        raise SystemExit("--adb-host must look like 192.168.1.50 or 192.168.1.50:5555")
    cfg.update(url=a.url.rstrip("/"), key=a.api_key, user_id=a.user_id, adb=a.adb, package=a.package,
               adb_target=(a.adb_host + (":5555" if a.adb_host and ":" not in a.adb_host else "")))
    app.run(host=a.host, port=a.port)


if __name__ == "__main__":
    main()
