"""The TV dashboard's settings, read and cleaned in one place (docs/TV_DASHBOARD.md)."""
import json
import re
import secrets
from datetime import date, datetime

from synchub import settings

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = dict(zip(DAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")))
COLORS = ("#4dabf7", "#f783ac", "#69db7c", "#ffd43b", "#b197fc", "#ff922b", "#3bc9db", "#ff6b6b")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def enabled():
    return settings.get("dash_enabled") == "1"


def token():
    """The long random part of the TV page's address; made the first time it is needed."""
    value = settings.get("dash_token")
    if not value:
        value = secrets.token_urlsafe(24)
        settings.set_value("dash_token", value)
    return value


def minutes(text, default=None):
    """'06:30' -> 390, or default when it is not a time."""
    return int(text[:2]) * 60 + int(text[3:]) if isinstance(text, str) and _TIME.match(text) else default


def days():
    chosen = [d for d in settings.get("dash_days").split(",") if d in DAYS]
    return chosen


def window():
    """(start, end) in minutes after midnight."""
    return minutes(settings.get("dash_start"), 390), minutes(settings.get("dash_end"), 510)


def metric():
    return settings.get("dash_units") == "metric"


def clean_time(value):
    return value if isinstance(value, str) and _TIME.match(value) else ""


def clean_people(text):
    """Validates the people list sent by the settings page. -> (True, json text) or (False, message)."""
    try:
        raw = json.loads(text) if text.strip() else []
    except ValueError:
        return False, "The people list could not be read. Reload the page and try again."
    if not isinstance(raw, list) or len(raw) > 12:
        return False, "Add at most 12 people to the dashboard."
    out, seen = [], set()
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            return False, "The people list could not be read."
        name = str(p.get("name") or "").strip()[:30]
        if not name:
            return False, "Every person on the dashboard needs a name."
        if name.lower() in seen:
            return False, f"{name} is on the dashboard twice."
        seen.add(name.lower())
        color = p.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(p.get("color") or "")) else COLORS[i % len(COLORS)]
        entry = {"name": name, "color": color, "kind": "school" if p.get("kind") == "school" else "calendar"}
        if entry["kind"] == "calendar":
            ics = str(p.get("ics") or "").strip()
            ics = re.sub(r"^webcal://", "https://", ics, flags=re.I)
            if ics and not re.match(r"^https?://\S{4,2000}$", ics):
                return False, f"{name}: the calendar link must be an address that starts with https:// (or webcal://)."
            entry["ics"] = ics
        else:
            week = {}
            for d in DAYS:
                day = (p.get("week") or {}).get(d) or {}
                leave, start, end = clean_time(day.get("leave")), clean_time(day.get("start")), clean_time(day.get("end"))
                if leave or start:
                    if not leave or not start:
                        return False, f"{name}, {DAY_LABELS[d]}: give both the leave-by time and the school start time, or clear both."
                    week[d] = {"leave": leave, "start": start, "end": end}
            entry["week"] = week
            mac = re.sub(r"[^0-9a-f]", "", str(p.get("mac") or "").lower())
            if mac and len(mac) != 12:
                return False, f"{name}: the phone's Wi-Fi address (MAC) should look like aa:bb:cc:dd:ee:ff."
            ics = re.sub(r"^webcal://", "https://", str(p.get("ics") or "").strip(), flags=re.I)
            if ics and not re.match(r"^https?://\S{4,2000}$", ics):
                return False, f"{name}: the school calendar link must be an address that starts with https:// (or webcal://)."
            entry["ics"] = ics
            ok, lines = clean_no_school(str(p.get("no_school") or ""))
            if not ok:
                return False, f"{name}: {lines}"
            entry["no_school"] = lines
            entry["mac"] = ":".join(mac[i:i + 2] for i in range(0, 12, 2)) if mac else ""
            entry["phone"] = str(p.get("phone") or "").strip()[:60]
        out.append(entry)
    return True, json.dumps(out, separators=(",", ":"))


def people():
    try:
        found = json.loads(settings.get("dash_people") or "[]")
    except ValueError:
        return []
    return [p for p in found if isinstance(p, dict) and p.get("name")]


def clean_no_school(text):
    """Lines like '2026-12-21 to 2027-01-03 Winter break' or '2026-11-26 Thanksgiving'. -> (True, cleaned text) or (False, message)."""
    lines = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        found = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:\s*(?:to|-|–)\s*(\d{4}-\d{2}-\d{2}))?\s*(.*)", line)
        if not found:
            return False, f'"{line[:40]}" is not a no-school line. Write 2026-12-21 to 2027-01-03 Winter break, or 2026-11-26 Thanksgiving.'
        try:
            first = date.fromisoformat(found.group(1))
            last = date.fromisoformat(found.group(2) or found.group(1))
        except ValueError:
            return False, f'"{line[:40]}" has a date that does not exist.'
        if last < first:
            return False, f'"{line[:40]}" ends before it starts.'
        lines.append(f"{first}" + (f" to {last}" if last != first else "") + (f" {found.group(3).strip()[:40]}" if found.group(3).strip() else ""))
    return True, "\n".join(lines[:200])


def no_school_on(day, text=None):
    """The label of the no-school entry that covers this date ('No school' when it has none), else None. `text` is one student's own list; by default the one for everybody."""
    for line in (settings.get("dash_no_school") if text is None else text).split("\n"):
        found = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?: to (\d{4}-\d{2}-\d{2}))?\s*(.*)", line.strip())
        if found:
            first, last = date.fromisoformat(found.group(1)), date.fromisoformat(found.group(2) or found.group(1))
            if first <= day <= last:
                return found.group(3).strip() or "No school"
    return None


_COUNT = re.compile(r"(?:(\d{4})-)?(\d{2})-(\d{2})\s+(.+)")


def clean_countdowns(text):
    """Lines like '2026-12-21 Winter break' (once) or '03-14 Sam's birthday' (every year). -> (True, cleaned text) or (False, message)."""
    lines = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        found = _COUNT.fullmatch(line)
        if not found:
            return False, f'"{line[:40]}" is not a countdown. Write 2026-12-21 Winter break, or 03-14 Sam\'s birthday for every year.'
        try:
            date(int(found.group(1) or 2024), int(found.group(2)), int(found.group(3)))      # 2024 is a leap year, so 02-29 is allowed
        except ValueError:
            return False, f'"{line[:40]}" has a date that does not exist.'
        lines.append(f"{found.group(1) + '-' if found.group(1) else ''}{found.group(2)}-{found.group(3)} {found.group(4).strip()[:40]}")
    return True, "\n".join(lines[:60])


def countdowns(today, limit=3, horizon=90):
    """The next few dates within `horizon` days: [{"label", "date", "days", "yearly"}], today first."""
    out = []
    for line in settings.get("dash_countdowns").split("\n"):
        found = _COUNT.fullmatch(line.strip())
        if not found:
            continue
        month, day, label, year = int(found.group(2)), int(found.group(3)), found.group(4).strip(), found.group(1)
        try:
            if year:
                when = date(int(year), month, day)
            else:
                when = next(d for d in (_safe_date(today.year, month, day), _safe_date(today.year + 1, month, day)) if d and d >= today)
        except (ValueError, StopIteration):
            continue
        gap = (when - today).days
        if 0 <= gap <= horizon:
            out.append({"label": label, "date": when.isoformat(), "days": gap, "yearly": not year})
    return sorted(out, key=lambda c: (c["days"], c["label"]))[:limit]


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:                     # 29 February in a year that has none
        return None


def now():
    return datetime.now().astimezone()
