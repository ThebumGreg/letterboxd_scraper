"""A student's day (from the typed school week) and "left for school" (from the phone's Wi-Fi)."""
import json
import logging
from datetime import datetime, timedelta

from synchub import settings, unifi_network
from synchub.dashboard import config

logger = logging.getLogger(__name__)


def day_plan(kid, now):
    """-> {"state": "school"|"none"|"holiday", "leave", "start", "end", "label"} for today. Times are ISO strings."""
    label = config.no_school_on(now.date())
    if label is not None:
        return {"state": "holiday", "label": label}
    entry = (kid.get("week") or {}).get(config.DAYS[now.weekday()])
    if not entry:
        return {"state": "none"}

    def at(text):
        minutes = config.minutes(text)
        return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)).isoformat() if minutes is not None else ""
    return {"state": "school", "leave": at(entry["leave"]), "start": at(entry["start"]), "end": at(entry.get("end"))}


# ------------------------------------------------------------------ presence
def load_state(day):
    try:
        state = json.loads(settings.get("dash_presence") or "{}")
    except ValueError:
        state = {}
    return state if state.get("day") == day.isoformat() else {"day": day.isoformat(), "kids": {}, "error": ""}


def save_state(state):
    settings.set_value("dash_presence", json.dumps(state, separators=(",", ":")))


def settle(entry, present, now, earliest, grace_minutes):
    """Moves one student's record forward. `entry` is {"state": "unknown"|"home"|"left", "seen", "gone_since", "left_at"}; returns the new record.
    A phone must have been seen at home first, must stay away for the grace time, and only counts as leaving after the earliest time of the morning.
    Once someone has left, they stay left for the rest of the day."""
    entry = dict(entry or {"state": "unknown"})
    if entry.get("state") == "left":
        return entry
    if present:
        return {"state": "home", "seen": now.isoformat(), "gone_since": None, "left_at": None}
    if entry.get("state") != "home" or now < earliest:
        return entry
    gone = datetime.fromisoformat(entry["gone_since"]) if entry.get("gone_since") else now
    entry["gone_since"] = gone.isoformat()
    if now - gone >= timedelta(minutes=grace_minutes):
        entry.update(state="left", left_at=gone.isoformat())
    return entry


def tracked():
    return [p for p in config.people() if p.get("kind") == "school" and p.get("mac")]


def poll(now=None, client=None):
    """Asks UniFi who is on the Wi-Fi and updates every student with a phone. Safe to call often; does nothing outside the morning."""
    now = now or config.now()
    kids = tracked()
    if not (config.enabled() and kids and unifi_network.configured()):
        return None
    _, end = config.window()
    minute = now.hour * 60 + now.minute
    if minute < 5 * 60 or minute > end + 30:
        return None
    state = load_state(now.date())
    try:
        online = (client or unifi_network.NetworkClient()).online_macs()
    except unifi_network.NetworkError as exc:
        state["error"] = str(exc)
        save_state(state)
        logger.warning(f"[dashboard] Wi-Fi presence: {exc}")
        return state
    state["error"] = ""
    earliest = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=config.minutes(settings.get("dash_presence_from"), 390))
    try:
        grace = max(1, int(settings.get("dash_presence_grace")))
    except ValueError:
        grace = 3
    for kid in kids:
        before = state["kids"].get(kid["name"])
        after = settle(before, unifi_network.mac_key(kid["mac"]) in online, now, earliest, grace)
        if (after or {}).get("state") == "left" and (before or {}).get("state") != "left":
            logger.info(f"[dashboard] {kid['name']} left for school (phone off the Wi-Fi since {after['left_at']}).")
        state["kids"][kid["name"]] = after
    save_state(state)
    return state


def for_page(kid, now):
    """What the dashboard shows for one student."""
    plan = day_plan(kid, now)
    out = {**plan, "tracked": bool(kid.get("mac")), "presence": "", "left_at": ""}
    if kid.get("mac") and plan["state"] == "school":
        state = load_state(now.date())
        entry = (state["kids"].get(kid["name"]) or {})
        out["presence"] = entry.get("state", "unknown")
        out["left_at"] = entry.get("left_at") or ""
        out["presence_error"] = state.get("error", "")
    return out
