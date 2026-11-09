"""Welcome home: when someone's phone joins the Wi-Fi after being away, the TV shows a full-screen "Welcome home" for a couple of minutes.

State lives in two settings: `dash_home` (per person: at home or away since when, the last welcome, and a short log of what happened, so it is easy to see why
a welcome did or did not show) and `dash_welcome` (the welcome that is on the screen now: {"until", "names", "opened", "was_standby"}).
Only "this phone is on the home Wi-Fi" is known, never where anyone is.
"""
import json
import logging
import time
from datetime import datetime, timedelta

from synchub import settings, unifi_network
from synchub.dashboard import calendars, config, school, show

logger = logging.getLogger(__name__)
KEEP_LOG = 12


# ------------------------------------------------------------------ who is home
def people():
    """The people whose phone is watched for arrivals: a phone address set, and the welcome not switched off for them."""
    return [p for p in config.people() if p.get("mac") and p.get("welcome", True) is not False]


def load_home():
    try:
        state = json.loads(settings.get("dash_home") or "{}")
    except ValueError:
        state = {}
    state.setdefault("people", {})
    state.setdefault("log", [])
    state.setdefault("error", "")
    return state


def save_home(state):
    settings.set_value("dash_home", json.dumps(state, separators=(",", ":")))


def step(rec, present, now, away_minutes):
    """Moves one person's record forward -> (record, arrived). `rec` is {"home": True|False|None, "away_since", "last_welcome"}.
    Arriving means the phone is on the Wi-Fi after being away for at least `away_minutes`; a phone that was never seen, or that napped for a few minutes, does not count."""
    rec = dict(rec or {})
    if present:
        since = rec.get("away_since")
        arrived = rec.get("home") is False and bool(since) and now - datetime.fromisoformat(since) >= timedelta(minutes=away_minutes)
        rec.update(home=True, away_since=None)
        return rec, arrived
    if rec.get("home"):
        rec.update(home=False, away_since=now.isoformat())
    return rec, False


def note(state, message, now=None):
    """One line in the log shown on the settings page, and in the TV log."""
    now = now or config.now()
    state["log"] = ([{"at": now.isoformat(timespec="seconds"), "message": message}] + state["log"])[:KEEP_LOG]
    logger.info(f"[dashboard] welcome: {message}")
    try:
        from synchub.tv import runtime
        runtime.get().logbook.add("system", settings.get("dash_tv"), "dashboard", response=f"Welcome home: {message}", ok=True)
    except Exception:
        pass


def poll(now=None, client=None):
    """Asks UniFi who is on the Wi-Fi (all day) and shows a welcome for anyone who has just come home."""
    now = now or config.now()
    who = people()
    if not (config.welcome_on() and who and unifi_network.configured()):
        return None
    state = load_home()
    try:
        online = (client or unifi_network.NetworkClient()).online_macs()
    except unifi_network.NetworkError as exc:
        state["error"] = str(exc)
        save_home(state)
        logger.warning(f"[dashboard] Welcome home: {exc}")
        return state
    state["error"] = ""
    away = config.number("dash_welcome_away", 15, 1, 240)
    arrived = []
    for p in who:
        rec, came = step(state["people"].get(p["name"]), unifi_network.mac_key(p["mac"]) in online, now, away)
        state["people"][p["name"]] = rec
        if came:
            arrived.append(p)
    state["people"] = {k: v for k, v in state["people"].items() if k in {p["name"] for p in who}}
    if arrived:
        welcome(state, arrived, now)
    save_home(state)
    return state


def welcome(state, arrived, now):
    """Decides, for people who have just arrived, whether to show the page, and does it."""
    gap = config.number("dash_welcome_gap", 60, 0, 720)
    ready = []
    for p in arrived:
        rec = state["people"][p["name"]]
        if config.quiet(now):
            note(state, f"{p['name']} came home: quiet hours, nothing shown", now)
        elif rec.get("last_welcome") and now - datetime.fromisoformat(rec["last_welcome"]) < timedelta(minutes=gap):
            note(state, f"{p['name']} came home again: shown less than {gap} minutes ago", now)
        else:
            ready.append(p)
    if not ready:
        return
    names = ", ".join(p["name"] for p in ready)
    try:
        shown, why = present(now, ready)
    except show.ShowError as exc:
        shown, why = False, str(exc)
    except Exception as exc:
        shown, why = False, f"{exc.__class__.__name__}: {exc}"
    if shown:
        for p in ready:
            state["people"][p["name"]]["last_welcome"] = now.isoformat()
        note(state, f"Welcome home shown for {names}" + (f" ({why})" if why else ""), now)
    else:
        note(state, f"{names} came home: not shown, {why}", now)


# ------------------------------------------------------------------ on the TV
def load_active():
    try:
        return json.loads(settings.get("dash_welcome") or "{}")
    except ValueError:
        return {}


def current(now):
    active = load_active()
    return active if active.get("until") and now < datetime.fromisoformat(active["until"]) else None


def present(now, ready):
    """Puts the welcome page on the TV (waking it if allowed). -> (True, note) or (False, why not). Raises ShowError when the TV is not set up."""
    seconds = config.number("dash_welcome_seconds", 120, 20, 900)
    until = (now + timedelta(seconds=seconds)).isoformat()
    names = [p["name"] for p in ready]
    active = current(now)
    if active:                                           # someone else arrived a moment ago: one page for both
        active["names"] = list(dict.fromkeys(active["names"] + names))
        active["until"] = until
        settings.set_value("dash_welcome", json.dumps(active))
        return True, "added to the welcome already on the screen"
    r, dev = show._runtime_device()
    package = settings.get("dash_app")
    if "stream_view" not in dev.capabilities:
        raise show.ShowError(f"{dev.name} cannot open a page yet. Use a Google TV or Android TV.")
    url = show.page_url()
    opened, was_standby = False, False
    if not show.shown_now(now.date()):
        why = show.busy(r, dev, package)
        if why:
            return False, f"the TV is busy: {why}"
        before = dev.status(quiet=True) or {}
        was_standby = before.get("power") in ("standby", None)
        if was_standby and settings.get("dash_welcome_wake") != "1":
            return False, "the TV is off and waking it is switched off"
        opened = True
    settings.set_value("dash_welcome", json.dumps({"until": until, "names": names, "opened": opened, "was_standby": was_standby}))    # before the page opens: it asks for this at once
    if opened:
        try:
            from synchub.tv import orchestrator
            if not orchestrator.ensure_on(dev, lambda message, ok=True: r.logbook.add("system", dev.id, "dashboard", response=message, ok=ok)):
                raise show.ShowError(f"{dev.name} would not wake up (see the TV log).")
            dev.view_stream(url, package)
        except Exception:
            settings.set_value("dash_welcome", "{}")
            raise
    return True, "woke the TV" if was_standby else ""


def tick(now=None):
    """Takes the welcome off the TV when its time is up (a quick job: every 15 seconds)."""
    active = load_active()
    if not active.get("until"):
        return
    now = now or config.now()
    if now < datetime.fromisoformat(active["until"]):
        return
    settings.set_value("dash_welcome", "{}")
    if not active.get("opened"):
        return                                          # it was shown on top of the morning or evening screen, which has its own end
    try:
        r, dev = show._runtime_device()
        package = settings.get("dash_app")
        app = (dev.status(quiet=True) or {}).get("app")
        if app == package:
            r.logbook.add("system", dev.id, "dashboard", response=f"Closed the welcome page: {dev.close_stream(package)}", ok=True)
            if active.get("was_standby") and settings.get("dash_sleep_after") == "1":
                time.sleep(2)
                dev.power(False)
                r.logbook.add("system", dev.id, "dashboard", response="Put the TV back to sleep", ok=True)
        else:
            r.logbook.add("system", dev.id, "dashboard", response=f"The welcome page is not on screen ({app or 'TV off'}), so the TV was left as it is", ok=True)
    except Exception as exc:
        logger.warning(f"[dashboard] closing the welcome page: {exc.__class__.__name__}: {exc}")


def end_now():
    """Forgets a welcome that is on the screen (the Close button took the page off)."""
    settings.set_value("dash_welcome", "{}")


# ------------------------------------------------------------------ what the page shows
def join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _clock(when):
    return f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def _lines(p, now):
    """The short useful lines under a person's greeting: their own text, and their next event or tomorrow's school."""
    out = [p["welcome_text"]] if p.get("welcome_text") else []
    if settings.get("dash_welcome_details") != "1":
        return out
    try:
        if p.get("kind") == "school":
            plan = school.day_plan(p, now + timedelta(days=1))
            if plan["state"] == "school":
                out.append(f"Tomorrow: leave by {_clock(datetime.fromisoformat(plan['leave']))}" + (f" ({plan['note']})" if plan.get("note") else ""))
            else:
                out.append(f"Tomorrow: {plan.get('label') or 'no school'}")
        elif p.get("ics"):
            found = calendars.for_person(p["ics"], now)
            soon = next((e for e in found["today"] if not e["all_day"] and datetime.fromisoformat(e["start"]) >= now), None)
            if soon:
                out.append(f"Next: {_clock(datetime.fromisoformat(soon['start']))} {soon['title']}")
            elif found.get("next") and found["next"]["events"]:
                e = found["next"]["events"][0]
                out.append(f"Next: {found['next']['date'][5:]} {e['title']}")
    except Exception as exc:
        logger.warning(f"[dashboard] welcome details for {p['name']}: {exc.__class__.__name__}")
    return out


def page(now, weather=None):
    """What the TV page shows as the welcome right now, or None."""
    active = current(now)
    if not active:
        return None
    by_name = {p["name"]: p for p in config.people()}
    names = [n for n in active["names"] if n in by_name] or active["names"]
    birthdays = {b.lower() for b in config.birthdays(now.date())}
    born = [n for n in names if n.lower() in birthdays]
    shown = []
    for n in names:
        p = by_name.get(n, {"name": n})
        lines = _lines(p, now)
        if n in born and len(born) < len(names):
            lines.insert(0, f"Happy birthday, {n}!")
        shown.append({"name": n, "color": p.get("color") or "#4dabf7", "lines": lines})
    common = []
    if settings.get("dash_welcome_details") == "1":
        if weather and not weather.get("error"):
            hint = weather.get("tomorrow_hint") or weather.get("hint")
            if hint:
                common.append(hint)
        for c in config.countdowns(now.date(), limit=1, horizon=14):
            if c["days"] > 0:
                common.append(f"{c['label']} {'tomorrow' if c['days'] == 1 else 'in ' + str(c['days']) + ' days'}")
    everyone_birthday = bool(born) and len(born) == len(names)
    return {"title": f"Happy birthday, {join(names)}!" if everyone_birthday else f"Welcome home, {join(names)}!", "sub": "Welcome home" if everyone_birthday else "",
            "birthday": bool(born), "color": shown[0]["color"], "people": shown, "common": common, "until": active["until"]}
