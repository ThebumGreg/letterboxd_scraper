"""Get a device ready for Jellyfin playback: power on, open the Jellyfin app, find its session."""
import time

from .devices import DeviceError, wake_on_lan
from .jellyfin import match_session, summarize_session


def device_sessions(jf, driver):
    """Jellyfin sessions that belong to this device, newest activity first."""
    rule = driver.session_rule()
    found = [s for s in jf.sessions() if s.get("SupportsRemoteControl") and match_session(rule, s)]
    return sorted(found, key=lambda s: s.get("LastActivityDate") or "", reverse=True)


def prepare(driver, jf, logbook, power_timeout=25, session_timeout=40):
    """-> {"ok", "steps", "session"}. Every step is also written to the log."""
    steps = []

    def step(msg, ok=True):
        steps.append(msg)
        logbook.add("system", driver.id, "prepare", response=msg, ok=ok)

    def wait_for(predicate, timeout, interval=2):
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(interval)
        return None

    def fail(msg):
        step(msg, ok=False)
        return {"ok": False, "steps": steps, "session": None}

    st = driver.status()
    if not st["online"]:
        if not driver.mac:
            return fail(f'{driver.name} is unreachable ({st.get("error")}). Add "mac" to its config to try Wake-on-LAN.')
        wake_on_lan(driver.mac)
        step(f"Unreachable; sent Wake-on-LAN to {driver.mac}")
        st = wait_for(lambda: (s := driver.status(quiet=True))["online"] and s, power_timeout) or st
        if not st["online"]:
            return fail("Still unreachable after Wake-on-LAN")

    if st["power"] != "on":
        driver.power(True)
        step(f'Sent power on (was {st["power"]})')
        woke = wait_for(lambda: (s := driver.status(quiet=True))["power"] == "on" and s, power_timeout, 1.5)
        if woke:
            st = woke
            time.sleep(2)  # let the launcher finish drawing before we start an app
        else:
            step("Device never reported powered on; continuing anyway", ok=False)

    app = driver.jellyfin_app()
    rule = driver.session_rule()
    baseline = {s["Id"]: s.get("LastActivityDate") for s in device_sessions(jf, driver)}
    if st["app"] == app:
        step(f"Jellyfin app ({app}) already in the foreground")
        session = wait_for(lambda: (device_sessions(jf, driver) or [None])[0], 15)
    else:
        driver.launch(app)
        step(f'Launched {app} (was {st["app"]})')
        # A stale session may linger from an earlier run, so wait for one that is new
        # or has reported activity since the launch.
        session = wait_for(
            lambda: next((s for s in device_sessions(jf, driver) if baseline.get(s["Id"]) != s.get("LastActivityDate")), None),
            session_timeout)
    if not session:
        return fail(f"No Jellyfin session matching {rule} with remote control appeared")
    step(f'Session found: {session.get("Client")} / {session.get("DeviceName")}')
    time.sleep(1)  # let the client finish registering before the first command
    return {"ok": True, "steps": steps, "session": summarize_session(session)}
