"""A person's calendar from the private ICS address Google, iCloud or Outlook gives out: today's events, or the next day with some."""
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta

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
    forget_except(c["text"] for c in _cache.values() if c.get("text") is not None)
    return text, False


def _as_local(value, tz):
    if isinstance(value, datetime):
        return (value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)), False
    return datetime.combine(value, dtime.min, tzinfo=tz), True


_parsed = {}            # calendar text -> the parsed calendar: reading a big calendar takes seconds and held the whole server while it did
_parse_lock = threading.Lock()
_parse_key_locks = {}
MAX_PARSED = 16


def parsed(text):
    """The parsed calendar for this text, read once however many pages ask (the text is replaced only when the calendar is downloaded again, every ten minutes).
    The parsing happens outside the shared lock, so reading one big calendar does not hold up answers that are already kept."""
    import icalendar
    with _parse_lock:
        cal = _parsed.get(text)
        if cal is not None:
            return cal
        key_lock = _parse_key_locks.setdefault(hash(text), threading.Lock())
    with key_lock:
        with _parse_lock:
            cal = _parsed.get(text)
        if cal is None:
            cal = icalendar.Calendar.from_ical(text)
            with _parse_lock:
                if len(_parsed) >= MAX_PARSED:
                    _parsed.pop(next(iter(_parsed)))
                _parsed[text] = cal
        return cal


_found = {}             # (calendar text, start, end) -> the events: spreading out repeating events over a window is slow too
MAX_FOUND = 64


_trimmed = {}           # (calendar text, first day, last day) -> the text with the events far from those days left out
_EVENT = re.compile(r"BEGIN:VEVENT\r?\n.*?END:VEVENT\r?\n?", re.S)
_DATES = re.compile(r"^(DTSTART|DTEND)[^:\r\n]*:(\d{8})", re.M)
_REPEATS = re.compile(r"^(RRULE|RDATE|RECURRENCE-ID|EXDATE)\b", re.M)
MARGIN_DAYS = 2


def _stamp(day):
    return day.strftime("%Y%m%d")


def trim_ics(text, first, last):
    """The calendar text with every single (not repeating) event that lies wholly outside [first, last] days, give or take a couple of days, left out.
    A Google calendar holds years of old events and reading them all in Python takes tens of seconds, during which nothing else in the app moves; the days the
    screen shows hold a handful. Repeating events, their exceptions and anything unusual are always kept."""
    lo, hi = _stamp(first - timedelta(days=MARGIN_DAYS)), _stamp(last + timedelta(days=MARGIN_DAYS))
    lo_long = _stamp(first - timedelta(days=MARGIN_DAYS + 40))          # an event with a DURATION instead of an end may run for a while
    out, at = [], 0
    for m in _EVENT.finditer(text):
        block = m.group(0)
        out.append(text[at:m.start()])
        at = m.end()
        if _REPEATS.search(block):
            out.append(block)
            continue
        found = {k: v for k, v in _DATES.findall(block)}
        begin = found.get("DTSTART")
        if begin is None:
            out.append(block)
            continue
        finish = found.get("DTEND") or (lo_long if "\nDURATION" in block or block.startswith("DURATION") else begin)
        if finish == lo_long:
            keep = lo_long <= begin <= hi
        else:
            keep = finish >= lo and begin <= hi
        if keep:
            out.append(block)
    out.append(text[at:])
    return "".join(out)


def trimmed(text, start, end):
    first, last = start.date(), (end - timedelta(microseconds=1)).date()
    key = (text, first, last)
    with _parse_lock:
        held = _trimmed.get(key)
    if held is None:
        held = trim_ics(text, first, last)
        with _parse_lock:
            if len(_trimmed) >= MAX_FOUND:
                _trimmed.pop(next(iter(_trimmed)))
            _trimmed[key] = held
    return held


def forget_except(texts):
    """Drops what is kept for calendar texts that were replaced by a new download, so old multi-megabyte copies do not stay in memory."""
    keep = set(texts)
    with _parse_lock:
        for k in [k for k in _found if k[0] not in keep]:
            del _found[k]
        for k in [k for k in _trimmed if k[0] not in keep]:
            del _trimmed[k]


def events_between(text, start, end):
    """Events that overlap [start, end) (timezone aware datetimes), sorted: all-day first, then by start. -> [{"title", "start", "end", "all_day", "where"}]
    The answer for a window is kept (the text changes only when the calendar is downloaded again); callers must not change it."""
    key = (text, start.isoformat(), end.isoformat())
    with _parse_lock:
        held = _found.get(key)
    if held is not None:
        return list(held)
    out = _events_between(text, start, end)
    with _parse_lock:
        if len(_found) >= MAX_FOUND:
            _found.pop(next(iter(_found)))
        _found[key] = out
    return list(out)


def _events_between(text, start, end):
    import recurring_ical_events
    cal = parsed(trimmed(text, start, end))
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


def _overlapping(events, start, end):
    """The events (already sorted) that overlap [start, end), the way events_between finds them; an event with no length counts at its start."""
    out = []
    for e in events:
        begin, finish = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
        if begin < end and (finish > start or (finish == begin and begin >= start)):
            out.append(e)
    return out


def for_person(url, now):
    """-> {"today": [...], "next": {"date", "events"} or None, "stale": bool}. Events already over today are dropped, except all-day ones."""
    text, stale = download(url)
    tz = now.tzinfo
    start, end = day_range(now.date(), tz)
    week = events_between(text, start, start + timedelta(days=8))       # one pass over the calendar for today and the week ahead
    today = [e for e in _overlapping(week, start, end) if e["all_day"] or datetime.fromisoformat(e["end"]) > now or datetime.fromisoformat(e["start"]) >= now]
    upcoming = None
    if not any(not e["all_day"] for e in today):
        for ahead in range(1, 8):
            d = now.date() + timedelta(days=ahead)
            first, last = day_range(d, tz)
            found = _overlapping(week, first, last)
            if found:
                upcoming = {"date": d.isoformat(), "events": found[:4]}
                break
    return {"today": today[:8], "next": upcoming, "stale": stale}


def for_day(url, day, tz):
    """-> {"events": [...], "stale": bool} for one whole calendar day (the evening screen's tomorrow)."""
    text, stale = download(url)
    start, end = day_range(day, tz)
    return {"events": events_between(text, start, end)[:8], "stale": stale}
