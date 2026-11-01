"""Put a camera on a TV full screen, and put the TV back the way it was.

Pressing Cameras on the remote pauses what the TV is playing in Jellyfin and remembers it (which title, where, whether it was playing), then opens
the camera's stream in a video player app. Closing the camera closes that app and carries on with the title: if Jellyfin's app is still open
and holds the title it is unpaused; if the app was closed meanwhile, it is opened again and told to play the title from the remembered place.
"""
import time

from . import orchestrator
from .devices import DeviceError
from .jellyfin import JellyfinError, estimated_ticks


def _playing(jf, dev):
    """The device's Jellyfin session that has something open (playing or paused), or None. A Jellyfin that cannot be reached is not an error here:
    the camera still opens, there is just nothing to come back to."""
    try:
        return next((s for s in orchestrator.device_sessions(jf, dev) if s.get("NowPlayingItem")), None)
    except JellyfinError:
        return None


def remember(r, dev, step):
    """Pauses what the TV is playing in Jellyfin and returns what to come back to ({} when nothing is open)."""
    jf = r.jf() if _has_jellyfin() else None
    session = _playing(jf, dev) if jf else None
    if not session:
        step("Nothing is open in Jellyfin on this TV, so there is nothing to come back to")
        return {}
    item, play = session["NowPlayingItem"], session.get("PlayState") or {}
    was_playing = not play.get("IsPaused")
    ticks = estimated_ticks(session) if was_playing else None
    if ticks is None:
        ticks = play.get("PositionTicks") or 0
    if was_playing:
        resp = jf.playstate(session["Id"], "Pause")
        step(f'Paused "{item.get("Name")}" -> HTTP {resp.status_code}', resp.ok)
    else:
        step(f'"{item.get("Name")}" was already paused')
    return {"item_id": item.get("Id"), "title": item.get("Name"), "ticks": int(ticks), "was_playing": was_playing, "session_id": session["Id"]}


def _has_jellyfin():
    from synchub import settings
    return bool(settings.get("jellyfin_url") and settings.get("jellyfin_api_key")) and settings.get("disable_jellyfin_connection") != "1"


_closed_at = {}               # device id -> when its camera player was last closed
REOPEN_WAIT = 4                # seconds the relay and the player get to let go of the last stream before a new one is asked for
PLAYER_LOOK = 4                # seconds after opening that the player must still be in front


def _in_front(dev, package):
    """True when the player is the app on screen (or the TV cannot say)."""
    try:
        app = (dev.status(quiet=True) or {}).get("app")
    except DeviceError:
        return True
    return not app or app == package


def show(r, dev, camera, url, package):
    """Open a camera full screen on this TV (or switch to another one when a camera is already showing). -> {"ok", "steps", "state"}."""
    if "stream_view" not in dev.capabilities:
        raise DeviceError(f"{dev.name} cannot show a camera yet. Cameras work on a Google TV or Android TV.")
    steps = []

    def step(message, ok=True):
        steps.append(message)
        r.logbook.add("system", dev.id, "camera", response=message, ok=ok)

    state = r.camera.get(dev.id)
    if state:
        step(f"Switching from {state['camera']['title']} to {camera['title']}")
        try:
            dev.close_stream(package)
        except DeviceError as e:
            step(f"Could not close the player first ({e}); opening the next camera anyway", False)
        time.sleep(1)
        before = {k: v for k, v in state.items() if k != "camera"}
    else:
        if not orchestrator.ensure_on(dev, step):
            return {"ok": False, "steps": steps, "state": None}
        before = remember(r, dev, step)
    wait = REOPEN_WAIT - (time.time() - _closed_at.get(dev.id, 0))
    if wait > 0:
        time.sleep(wait)
    how = dev.view_stream(url, package)
    step(f'{camera["title"]}: {how}')
    r.camera[dev.id] = {**before, "camera": camera, "since": time.time()}
    time.sleep(PLAYER_LOOK)
    if not _in_front(dev, package):
        step("The player closed by itself right after opening, so the stream probably did not start. Trying once more", False)
        time.sleep(1)
        dev.view_stream(url, package)
        time.sleep(PLAYER_LOOK)
        if not _in_front(dev, package):
            step("The player closed again. Try another \"Stream the TV opens\" type in the Protect settings. Carrying on with what you were watching", False)
            carried = close(r, dev, package)
            return {"ok": False, "steps": steps + carried["steps"], "state": carried["state"]}
    return {"ok": True, "steps": steps, "state": status(r, dev.id)}


def close(r, dev, package):
    """Close the camera and carry on with what was playing. -> {"ok", "steps", "state"}."""
    steps = []

    def step(message, ok=True):
        steps.append(message)
        r.logbook.add("system", dev.id, "camera", response=message, ok=ok)

    state = r.camera.pop(dev.id, None)
    _closed_at[dev.id] = time.time()
    try:
        step(f"Closed the camera: {dev.close_stream(package)}")
    except DeviceError as e:
        step(f"Could not close the camera player: {e}", False)
        if state:
            r.camera[dev.id] = state                    # still showing: leave it so closing can be tried again
        return {"ok": False, "steps": steps, "state": status(r, dev.id)}
    if not state or not state.get("item_id"):
        step("Nothing to carry on with")
        return {"ok": True, "steps": steps, "state": status(r, dev.id)}
    time.sleep(2)                                       # let Android bring the Jellyfin app back to the front
    jf = r.jf()
    wanted = orchestrator._norm(state["item_id"])
    session = orchestrator._wait_for(lambda: next((s for s in orchestrator.device_sessions(jf, dev)
                                                   if orchestrator._norm((s.get("NowPlayingItem") or {}).get("Id")) == wanted), None), 8, 1)
    if session:
        if state["was_playing"]:
            resp = jf.playstate(session["Id"], "Unpause")
            step(f'Carrying on with "{state["title"]}" -> HTTP {resp.status_code}', resp.ok)
        else:
            step(f'"{state["title"]}" is back, paused as you left it')
        return {"ok": True, "steps": steps, "state": status(r, dev.id)}
    step(f'Jellyfin closed "{state["title"]}" meanwhile, so opening it again at {orchestrator._clock(state["ticks"])}')
    prepared = orchestrator.prepare(dev, jf, r.logbook)
    steps.extend(prepared["steps"])
    if not prepared["ok"]:
        return {"ok": False, "steps": steps, "state": status(r, dev.id)}
    sid = prepared["session"]["Id"]
    resp = jf.play(sid, {"playCommand": "PlayNow", "itemIds": state["item_id"], "startPositionTicks": state["ticks"]})
    ok = resp.ok and orchestrator.confirm_playing(jf, sid, state["item_id"])[0]
    step(f'Playing "{state["title"]}" again -> HTTP {resp.status_code}' + ("" if ok else ", but Jellyfin never reported it playing"), bool(ok))
    return {"ok": bool(ok), "steps": steps, "state": status(r, dev.id)}


def status(r, dev_id):
    """What the remote shows for this TV: None, or the camera on screen and what will resume."""
    state = r.camera.get(dev_id)
    if not state:
        return None
    return {"camera": state["camera"]["name"], "title": state["camera"]["title"],
            "resume": ({"title": state["title"], "wasPlaying": state["was_playing"], "ticks": state["ticks"]} if state.get("item_id") else None)}
