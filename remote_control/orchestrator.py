"""Get a device ready for Jellyfin playback: power on, open the Jellyfin app, find its session."""
import time

from .jellyfin import match_session, summarize_session


def device_sessions(jf, driver):
    """Jellyfin sessions that belong to this device, newest activity first."""
    rule = driver.session_rule()
    found = [s for s in jf.sessions() if s.get("SupportsRemoteControl") and match_session(rule, s)]
    return sorted(found, key=lambda s: s.get("LastActivityDate") or "", reverse=True)


def _wait_for(predicate, timeout, interval=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def ensure_on(driver, step, timeout=25):
    """Bring a device from any power state to "on". -> the device status, or None on failure.

    unreachable   -> Wake-on-LAN (needs a MAC), then wait for it to answer
    standby/off   -> the device's power-on key, falling back to Wake-on-LAN if that does nothing
    screensaver   -> same wake path
    """
    st = driver.status()
    if not st["online"]:
        if st.get("needsPairing"):
            step(f'{driver.name} is not paired yet. Use "Start pairing" on its remote first.', False)
            return None
        if not driver.mac:
            step(f'{driver.name} is unreachable ({st.get("error")}). Press "Detect MAC" while it is on, or add '
                 '"mac" to devices.json, so Wake-on-LAN can be tried.', False)
            return None
        driver.wol()
        step(f"Unreachable; sent Wake-on-LAN to {driver.mac}")
        st = _wait_for(lambda: (s := driver.status(quiet=True))["online"] and s, timeout)
        if not st:
            step("Still unreachable after Wake-on-LAN. The TV may not support it (check its network-standby / "
                 "'wake on Wi-Fi' setting), or this computer is not on the TV's network segment.", False)
            return None
        step("Device is reachable again")

    if st["power"] != "on":
        driver.power(True)
        step(f'Sent power on (was {st["power"]})')
        woke = _wait_for(lambda: (s := driver.status(quiet=True))["power"] == "on" and s, timeout, 1.5)
        if not woke and driver.mac:
            driver.wol()
            step("Power key had no effect; sent Wake-on-LAN as well")
            woke = _wait_for(lambda: (s := driver.status(quiet=True))["power"] == "on" and s, timeout, 1.5)
        if woke:
            time.sleep(2)  # let the launcher finish drawing before we start an app
            return woke
        step("Device never reported powered on", False)
        return None
    return st


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
    rule = driver.session_rule()
    baseline = {s["Id"]: s.get("LastActivityDate") for s in device_sessions(jf, driver)}
    if st["app"] == app:
        step(f"Jellyfin app ({app}) already in the foreground")
        session = _wait_for(lambda: (device_sessions(jf, driver) or [None])[0], 15)
    else:
        driver.launch(app)
        step(f'Launched {app} (was {st["app"]})')
        # A stale session may linger from an earlier run, so wait for one that is new
        # or has reported activity since the launch.
        session = _wait_for(
            lambda: next((s for s in device_sessions(jf, driver) if baseline.get(s["Id"]) != s.get("LastActivityDate")), None),
            session_timeout)
    if not session:
        return fail(f"No Jellyfin session matching {rule} with remote control appeared")
    step(f'Session found: {session.get("Client")} / {session.get("DeviceName")}')
    time.sleep(1)  # let the client finish registering before the first command
    return {"ok": True, "steps": steps, "session": summarize_session(session)}


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
