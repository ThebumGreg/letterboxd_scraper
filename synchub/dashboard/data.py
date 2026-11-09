"""Everything the TV page shows, in one answer. Each part fails on its own: a calendar that cannot be read leaves the rest of the screen alone."""
import logging
import threading
import time
from datetime import timedelta

from synchub import settings
from synchub.dashboard import calendars, config, quote, school, show, weather, welcome

logger = logging.getLogger(__name__)


def person(p, now, mode="morning"):
    base = {"name": p["name"], "color": p.get("color") or "#4dabf7", "kind": p.get("kind", "calendar")}
    tomorrow = now + timedelta(days=1)
    if base["kind"] == "school":
        return {**base, "school": school.for_day(p, tomorrow) if mode == "evening" else school.for_page(p, now)}
    if not p.get("ics"):
        return {**base, "error": "No calendar link yet."}
    try:
        if mode == "evening":
            found = calendars.for_day(p["ics"], tomorrow.date(), now.tzinfo)
            return {**base, "tomorrow": found["events"], "stale": found["stale"]}
        return {**base, **calendars.for_person(p["ics"], now)}
    except calendars.CalendarError as exc:
        return {**base, "error": str(exc)}
    except Exception as exc:
        logger.warning(f"[dashboard] {p['name']}'s calendar could not be read: {exc.__class__.__name__}: {exc}")
        return {**base, "error": "The calendar could not be read."}


def with_extras(w, day):
    """The weather plus what is worked out here: the moon (no service needed) and the one-line UV note."""
    if not w or w.get("error"):
        return w
    today = next((d for d in w.get("days", []) if d["date"] == day.isoformat()), None) or (w.get("days") or [{}])[0]
    notes = [n for n in (weather.uv_note(today.get("uv")),) if n]
    if w.get("aqi") is not None and w["aqi"] > 100:
        notes.append(f"Air quality: {w['aqi_words'].lower()} ({w['aqi']})")
    return {**w, "moon": weather.moon(day), "uv": today.get("uv"), "sun": {"rise": today.get("sunrise") or w.get("sunrise"), "set": today.get("sunset") or w.get("sunset")}, "notes": notes}


_lock = threading.Lock()
_last = {}                    # (database, mode asked for) -> (taken at, change counter, answer)
_key_locks = {}
KEEP_SECONDS = 15


def cached(mode=None):
    """The page's answer, reused for a few seconds: several screens (and a browser tab) ask every 30 seconds, and working it out reads calendars and the weather.
    One lock per mode: a slow calendar for one mode does not hold up the other, and each mode keeps its own answer."""
    from synchub import db
    from synchub.library import cache
    with db.connection() as conn:
        gen = cache.generation(conn)                       # any setting change (people, ZIP, school days, who has left) shows at once
    key = (db.DB_FILE, mode)
    with _lock:
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:                                         # one request works it out; the others wait and take the same answer
        held = _last.get(key)
        if held and held[1] == gen and time.monotonic() - held[0] < KEEP_SECONDS:
            return held[2]
        answer = build(mode=mode)
        _last[key] = (time.monotonic(), gen, answer)
        return answer


def build(now=None, mode=None):
    now = now or config.now()
    start, end = config.window()
    mode = mode if mode in show.MODES else show.current_mode(now)
    day = now.date() + timedelta(days=1) if mode == "evening" else now.date()
    w = with_extras(weather.get(), day)
    out = {"now": now.isoformat(), "mode": mode, "metric": config.metric(), "window": {"start": start, "end": end},
           "weather": w,
           "quote": quote.today(now.date()) if settings.get("dash_quote_on") == "1" else None,
           "countdowns": config.countdowns(now.date()),
           "birthdays": config.birthdays(now.date()),
           "all_left": school.all_left(now) if mode == "morning" else False,
           "people": [person(p, now, mode) for p in config.people()],
           "welcome": None}
    try:
        out["welcome"] = welcome.page(now, w)
    except Exception as exc:
        logger.warning(f"[dashboard] the welcome page could not be made: {exc.__class__.__name__}: {exc}")
    return out
