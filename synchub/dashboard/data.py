"""Everything the TV page shows, in one answer. Each part fails on its own: a calendar that cannot be read leaves the rest of the screen alone."""
import logging

from synchub import settings
from synchub.dashboard import calendars, config, quote, school, weather

logger = logging.getLogger(__name__)


def person(p, now):
    base = {"name": p["name"], "color": p.get("color") or "#4dabf7", "kind": p.get("kind", "calendar")}
    if base["kind"] == "school":
        return {**base, "school": school.for_page(p, now)}
    if not p.get("ics"):
        return {**base, "error": "No calendar link yet."}
    try:
        return {**base, **calendars.for_person(p["ics"], now)}
    except calendars.CalendarError as exc:
        return {**base, "error": str(exc)}
    except Exception as exc:
        logger.warning(f"[dashboard] {p['name']}'s calendar could not be read: {exc.__class__.__name__}: {exc}")
        return {**base, "error": "The calendar could not be read."}


def build(now=None):
    now = now or config.now()
    start, end = config.window()
    return {"now": now.isoformat(), "metric": config.metric(), "window": {"start": start, "end": end},
            "weather": weather.get(),
            "quote": quote.today(now.date()) if settings.get("dash_quote_on") == "1" else None,
            "people": [person(p, now) for p in config.people()]}
