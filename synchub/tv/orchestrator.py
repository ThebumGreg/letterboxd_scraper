"""Get a device ready for Jellyfin playback: power on, open the Jellyfin app, find its session."""
import time
from concurrent.futures import ThreadPoolExecutor, wait

from . import casting
from .devices import DeviceError
from .jellyfin import match_session, summarize_session


UNCONTROLLABLE_GRACE = 15      # seconds after opening the app during which a session that cannot be controlled is assumed to be a leftover


def rule_ladder(driver):
    """The device's session rule, then its kind's default rule as a fallback."""
    rules = [driver.session_rule()]
    if driver.default_session_rule and driver.default_session_rule not in rules:
        rules.append(driver.default_session_rule)
    return rules


def effective_rule(driver, sessions):
    """The first rule in the ladder that matches any of these sessions (or the primary rule if none do)."""
    rules = rule_ladder(driver)
    return next((r for r in rules if any(match_session(r, s) for s in sessions)), rules[0])


def device_sessions(jf, driver):
    """Jellyfin sessions that belong to this device, newest activity first."""
    raw = [s for s in jf.sessions() if s.get("SupportsRemoteControl")]
    rule = effective_rule(driver, raw)
    if rule != driver.session_rule() and not getattr(driver, "_warned_rule", False):
        driver._warned_rule = True
        jf.logbook.add("system", driver.id, "session rule", ok=False,
                       response=f"The session rule {driver.session_rule()} matched nothing, so the default {rule} is being used. "
                                'Fix it on TV tools under "Sessions Jellyfin can see".')
    found = [s for s in raw if match_session(rule, s)]
    return sorted(found, key=lambda s: s.get("LastActivityDate") or "", reverse=True)


def explain_no_session(jf, driver):
    """-> (message, reason): why no session matched, with what Jellyfin does list, so the rule can be corrected.
    reason is "not_remote" when the app's session exists but cannot take commands, else "no_match"."""
    raw = jf.sessions()
    rules = rule_ladder(driver)
    not_remote = [s for s in raw if not s.get("SupportsRemoteControl") and any(match_session(r, s) for r in rules)]
    if not_remote:
        s = not_remote[0]
        return (f'Jellyfin has a session for {s.get("Client")} / {s.get("DeviceName")}, but it reports that it cannot be '
                "controlled remotely (SupportsRemoteControl is false), so the app on this device ignores play commands."), "not_remote"
    seen = "; ".join(f'{s.get("Client")} / {s.get("DeviceName")}' for s in raw) or "none"
    return (f"No Jellyfin session matched {rules[0]}. Sessions Jellyfin lists right now: {seen}. On TV tools, under "
            f'"Sessions Jellyfin can see", choose "Use for {driver.name}" on the right one.'), "no_match"


def _wait_for(predicate, timeout, interval=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def ensure_on(driver, step):
    """Bring a device from any power state to "on". -> the device status, or None on failure.

    Anything that is not clearly on gets every configured wake method at once (Wake-on-LAN, the device's
    own power key, a Cast launch, a queued ADB key, ...). They are all safe to repeat, and which one a
    given TV honours in standby varies, so waiting for the first success beats trying them one by one.
    """
    timeout = int(driver.cfg.get("wake_timeout", 45))
    st = driver.status()
    if st["online"] and st["power"] == "on":
        return st
    if st.get("needsPairing") and not st["online"]:
        step(f'{driver.name} is not paired yet. Use "Start pairing" on its remote first.', False)
        return None

    # Reachable but screen off or dreaming: the device's own key is enough. Unreachable: try everything.
    methods = ["key"] if st["online"] else driver.wake_methods()
    step(f'{driver.name} is {st.get("power") or "unreachable"}'
         f'{" (" + st["error"] + ")" if st.get("error") else ""}; waking via {", ".join(methods)}')
    executor = ThreadPoolExecutor(max_workers=max(1, len(methods)))
    futures = {executor.submit(driver.wake_with, m): m for m in methods}
    done, pending = wait(futures, timeout=15)
    sent = len(pending)
    for fut in done:
        try:
            step(f"Wake via {futures[fut]}: {fut.result()}")
            sent += 1
        except DeviceError as e:
            step(f"Wake via {futures[fut]} failed: {e}", False)
    for fut in pending:
        step(f"Wake via {futures[fut]} is still running in the background", True)
    executor.shutdown(wait=False)
    if not sent:
        step("None of the wake methods could even be sent, so there is nothing to wait for.", False)
        return None

    keyed = methods == ["key"]

    def came_on():
        nonlocal keyed
        s = driver.status(quiet=True)
        if s["online"] and s["power"] == "on":
            return s
        if s["online"] and not keyed:
            # It answers now (e.g. after Wake-on-LAN) but is still off: press its own power key once.
            keyed = True
            try:
                step(f'Device answers but is {s["power"]}: {driver.wake_with("key")}')
            except DeviceError as e:
                step(f"Power key failed: {e}", False)
        return None

    woke = _wait_for(came_on, timeout, 2)
    if not woke:
        step(f"The device did not come on within {timeout}s. Run Probe network now and compare it with a probe while "
             "the TV is on: whichever ports answer in standby show which wake method can work. TVs often ignore "
             "Wake-on-LAN; the screensaver sleep mode avoids needing to wake them at all.", False)
        return None
    time.sleep(2)  # let the launcher finish drawing before we start an app
    return woke


def power_on(driver, logbook):
    steps = []

    def step(msg, ok=True):
        steps.append(msg)
        logbook.add("system", driver.id, "power on", response=msg, ok=ok)

    st = ensure_on(driver, step)
    if st:
        step("Device is on")
    return {"ok": bool(st), "steps": steps}


def prepare(driver, jf, logbook, session_timeout=40):
    """-> {"ok", "steps", "session"}. Every step is also written to the log."""
    steps = []

    def step(msg, ok=True):
        steps.append(msg)
        logbook.add("system", driver.id, "prepare", response=msg, ok=ok)

    def fail(msg):
        step(msg, ok=False)
        return {"ok": False, "steps": steps, "session": None}

    st = ensure_on(driver, step)
    if not st:
        return {"ok": False, "steps": steps, "session": None}

    app = driver.jellyfin_app()

    def uncontrollable():
        """The app's session exists but says it cannot take commands."""
        rules = rule_ladder(driver)
        return any(not x.get("SupportsRemoteControl") and any(match_session(r, x) for r in rules) for x in jf.sessions())

    in_front = st["app"] == app
    # A session that cannot be controlled only means something while the app is open: with another app in front it is a leftover
    # from an earlier run, and opening the app makes a new one.
    if in_front and not device_sessions(jf, driver) and uncontrollable():
        message, reason = explain_no_session(jf, driver)
        return {**fail(message), "reason": reason}
    baseline = {s["Id"]: s.get("LastActivityDate") for s in device_sessions(jf, driver)}
    if in_front:
        step(f"Jellyfin app ({app}) already in the foreground")
        session = _wait_for(lambda: (device_sessions(jf, driver) or [None])[0] or ("uncontrollable" if uncontrollable() else None), 15)
    else:
        try:
            how = driver.launch(app)
        except DeviceError as e:
            return fail(str(e))
        step(f'Launched {app} (was {st["app"]}): {how or "sent"}')
        launched = time.time()
        # A stale session may linger from an earlier run, so wait for one that is new or has reported activity since the launch.
        # The old session says it cannot be controlled until the app registers a new one, so that only counts after a grace period.
        session = _wait_for(
            lambda: next((s for s in device_sessions(jf, driver) if baseline.get(s["Id"]) != s.get("LastActivityDate")), None)
            or ("uncontrollable" if time.time() - launched > UNCONTROLLABLE_GRACE and uncontrollable() else None),
            session_timeout)
    if not session or session == "uncontrollable":
        message, reason = explain_no_session(jf, driver)
        return {**fail(message), "reason": reason}
    step(f'Session found: {session.get("Client")} / {session.get("DeviceName")}')
    time.sleep(1)  # let the client finish registering before the first command
    return {"ok": True, "steps": steps, "session": summarize_session(session)}


def go_home(driver, step, wait=2):
    """Press the device's Home key and give it time to settle (a player screen ignores play commands from outside)."""
    key = driver.keymap.get("home")
    if not key:
        return False
    try:
        driver.send_key(key)
    except DeviceError as e:
        step(f"Could not press Home: {e}", False)
        return False
    time.sleep(wait)
    return True


def stop_playback(jf, session_id, step, timeout=8):
    """Something is already playing (or paused) in the app: stop it and wait for the session to say nothing is playing."""
    resp = jf.playstate(session_id, "Stop")
    step(f"Stopped what was playing -> HTTP {resp.status_code}", resp.ok)
    if not resp.ok:
        return False

    def cleared():
        return all(not s.get("NowPlayingItem") for s in jf.sessions() if s.get("Id") == session_id) or None
    return bool(_wait_for(cleared, timeout, 1))


def _playing_item(jf, driver, item_id):
    """The device's Jellyfin session is playing this item (or something from this series/season)."""
    rules = rule_ladder(driver)
    wanted = _norm(item_id)
    for s in jf.sessions():   # not filtered by SupportsRemoteControl: a session that ignores commands still reports playback
        if not any(match_session(r, s) for r in rules):
            continue
        n = s.get("NowPlayingItem") or {}
        if wanted in {_norm(n.get(k)) for k in ("Id", "SeriesId", "SeasonId", "ParentId")}:
            return n
    return None


def play_deeplink(driver, jf, logbook, item_id, start_ticks=None, timeout=45):
    """Power the device on and send the Jellyfin app a deep link. -> {"ok", "steps", "confirmed", "retryable"}.

    The app plays inside itself, so Jellyfin sees the session and records progress and watched state.
    Deep links always start from the beginning. `retryable` means the request itself failed (so a
    different route may be tried), as opposed to it being sent but not confirmed."""
    steps = []

    def step(msg, ok=True):
        steps.append(msg)
        logbook.add("system", driver.id, "play (deep link)", response=msg, ok=ok)

    def fail(msg, retryable=False):
        step(msg, False)
        return {"ok": False, "steps": steps, "confirmed": None, "retryable": retryable}

    st = ensure_on(driver, step)
    if not st:
        return {"ok": False, "steps": steps, "confirmed": None, "retryable": False}
    item = jf.item(item_id)
    if not item.get("Id"):
        return fail(f"Jellyfin has no item {item_id}")
    media_type = str(item.get("Type") or "").lower()
    if media_type not in driver.deeplink_types:
        return fail(f'The Jellyfin app cannot be sent a "{item.get("Type")}" by deep link')
    series_id = item.get("SeriesId") if media_type == "season" else None
    if media_type == "season" and not series_id:
        return fail("A season deep link needs its series id, which Jellyfin did not give")
    try:
        app = driver.jellyfin_app()
        how = driver.deeplink(app, media_type, item_id, series_id, running=st.get("app") == app)
    except DeviceError as e:
        return fail(str(e), retryable=True)
    step(f'Asked the Jellyfin app to play "{item.get("Name")}" ({media_type}): {how}')
    if start_ticks:
        step("Deep links always start from the beginning; the saved resume point is not used", True)
    playing = _wait_for(lambda: _playing_item(jf, driver, item_id), timeout, 2)
    if playing:
        step(f'Jellyfin reports it playing: {playing.get("Name")}')
    else:
        step("The link was sent, but Jellyfin never reported it playing. The app may still be loading, or not be signed in.", False)
    return {"ok": True, "steps": steps, "confirmed": bool(playing), "retryable": False}


def play_url(driver, jf, logbook, item_id, start_ticks=None):
    """Power the device on, then hand it a stream URL. -> {"ok", "steps", "confirmed"}.

    No Jellyfin app or session is involved, so Jellyfin does not learn about the playback
    (no resume point or watched mark), but nothing depends on the TV app's remote control.
    """
    steps = []

    def step(msg, ok=True):
        steps.append(msg)
        logbook.add("system", driver.id, "play (stream)", response=msg, ok=ok)

    def fail(msg):
        step(msg, False)
        return {"ok": False, "steps": steps, "confirmed": None}

    if not ensure_on(driver, step):
        return {"ok": False, "steps": steps, "confirmed": None}
    item = jf.item(item_id)
    if not item.get("Id"):
        return fail(f"Jellyfin has no item {item_id}")
    url, fmt, why = casting.stream_url(jf, item, driver.cfg.get("stream_base"), driver.cfg.get("stream_mode", "auto"), start_ticks)
    step(f'Streaming "{item.get("Name")}" to {driver.name}: {why}')
    try:
        driver.play_url(url, item.get("Name"), fmt)
    except DeviceError as e:
        return fail(str(e))
    playing = _wait_for(lambda: ((driver.status(quiet=True).get("media") or {}).get("state") in ("playing", "buffering", "starting")), 25, 2)
    if playing:
        step("The device reports the stream playing")
    else:
        step("The stream was sent, but the device never reported it playing. The TV may not support this file or Jellyfin's address "
             "may not be reachable from it (set stream_base in devices.json to Jellyfin's LAN address).", False)
    return {"ok": True, "steps": steps, "confirmed": bool(playing)}


def _norm(item_id):
    return str(item_id or "").replace("-", "").lower()


def confirm_playing(jf, session_id, expected_id, timeout=15):
    """Wait for the session to report the item playing. -> (confirmed, title, seconds)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for s in jf.sessions():
            now = s.get("NowPlayingItem") or {}
            if s.get("Id") == session_id and _norm(now.get("Id")) == _norm(expected_id):
                return True, now.get("Name"), round(time.time() - t0, 1)
        time.sleep(1)
    return False, None, timeout
