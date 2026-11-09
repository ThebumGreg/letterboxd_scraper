"""A person's calendar from the private ICS address Google, iCloud or Outlook gives out: today's events, or the next day with some."""
import time
from datetime import date, datetime, time as dtime, timedelta

from synchub import http

TTL = 600
MAX_BYTES = 8_000_000
_cache = {}             # address -> {"at", "text", "error"}


class CalendarError(Exception):
    pass


def download(url):
    """The calendar text for an address; cached for ten minutes. When the address stops answering the last good copy is used and marked stale."""
    url = url.replace("webcal://", "https://")
    held = _cache.get(url)
    if held and held.get("text") is not None and time.time() - held["at"] < TTL:
        return held["text"], False
    try:
        res = http.request("calendar", "ics", "GET", url, timeout=20, headers={"Accept": "text/calendar"})
        if res.status_code != 200:
            raise CalendarError(f"The calendar link answered HTTP {res.status_code}. Is it still shared?")
        if len(res.content) > MAX_BYTES:
            raise CalendarError("The calendar is too big to read (over 8 MB).")
        text = res.text
        if "BEGIN:VCALENDAR" not in text:
            raise CalendarError("That address is not a calendar (ICS) link.")
    except CalendarError:
        if held and held.get("text") is not None:
            return held["text"], True
        raise
    except Exception as exc:
        if held and held.get("text") is not None:
            return held["text"], True
        raise CalendarError(f"Could not read the calendar ({exc.__class__.__name__}).")
    _cache[url] = {"at": time.time(), "text": text}
    return text, False


def _as_local(value, tz):
    if isinstance(value, datetime):
        return (value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)), False
    return datetime.combine(value, dtime.min, tzinfo=tz), True


def events_between(text, start, end):
    """Events that overlap [start, end) (timezone aware datetimes), sorted: all-day first, then by start. -> [{"title", "start", "end", "all_day", "where"}]"""
    import icalendar
    import recurring_ical_events
    cal = icalendar.Calendar.from_ical(text)
    tz = start.tzinfo
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin, all_day = _as_local(ev.decoded("DTSTART"), tz)
        if "DTEND" in ev:
            finish, _ = _as_local(ev.decoded("DTEND"), tz)
        elif "DURATION" in ev:
            finish = begin + ev.decoded("DURATION")
        else:
            finish = begin + (timedelta(days=1) if all_day else timedelta())
        out.append({"title": str(ev.get("SUMMARY") or "(no title)")[:80], "start": begin.isoformat(), "end": finish.isoformat(), "all_day": all_day,
                    "where": str(ev.get("LOCATION") or "")[:60]})
    return sorted(out, key=lambda e: (not e["all_day"], e["start"], e["title"]))


def day_range(day, tz):
    first = datetime.combine(day, dtime.min, tzinfo=tz)
    return first, first + timedelta(days=1)


def for_person(url, now):
    """-> {"today": [...], "next": {"date", "events"} or None, "stale": bool}. Events already over today are dropped, except all-day ones."""
    text, stale = download(url)
    tz = now.tzinfo
    start, end = day_range(now.date(), tz)
    today = [e for e in events_between(text, start, end) if e["all_day"] or datetime.fromisoformat(e["end"]) > now or datetime.fromisoformat(e["start"]) >= now]
    upcoming = None
    if not any(not e["all_day"] for e in today):
        for ahead in range(1, 8):
            d = now.date() + timedelta(days=ahead)
            first, last = day_range(d, tz)
            found = events_between(text, first, last)
            if found:
                upcoming = {"date": d.isoformat(), "events": found[:4]}
                break
    return {"today": today[:8], "next": upcoming, "stale": stale}


def for_day(url, day, tz):
    """-> {"events": [...], "stale": bool} for one whole calendar day (the evening screen's tomorrow)."""
    text, stale = download(url)
    start, end = day_range(day, tz)
    return {"events": events_between(text, start, end)[:8], "stale": stale}
