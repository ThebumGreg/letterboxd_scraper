"""The Discover page: what is new and trending, and what is coming, from TMDB, Jellyfin (via the catalog) and the Radarr and Sonarr calendars.

TMDB lists are kept for a few hours, so the page is quick and costs a handful of TMDB calls a day however many people open it. A list that cannot be
read leaves its own section out with a note, never the whole page."""
import logging
import threading
import time
from datetime import date, timedelta

from synchub import catalog, db, ratings, search
from synchub.clients import ApiError, ConfigError
from synchub.clients.arr import RadarrClient, SonarrClient
from synchub.clients.tmdb import TmdbClient

logger = logging.getLogger(__name__)

CACHE_SECONDS = 3 * 3600
PER_SECTION = 12
CALENDAR_DAYS = 21
_cache, _lock = {}, threading.Lock()


def cached(key, load, now=None):
    """load() unless it was read within CACHE_SECONDS. Failures are not remembered."""
    now = time.monotonic() if now is None else now
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = load()
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_cache():
    with _lock:
        _cache.clear()


def _cards(results, kind=None, today=None, upcoming=False):
    """TMDB results -> the same cards the search page uses. `kind` forces movie/show when the list has one kind; a mixed list names it per row."""
    cards = []
    for r in results:
        what = kind or {"movie": "movie", "tv": "show"}.get(r.get("media_type"))
        if not what or not r.get("id"):
            continue
        card = search._movie(r) if what == "movie" else search._show(r)
        card["date"] = r.get("release_date") or r.get("first_air_date") or None
        if upcoming and today and (not card["date"] or card["date"] < today):
            continue
        cards.append(card)
    return cards


# (key, heading, what it shows, loader name on TmdbClient + kind)
TMDB_SECTIONS = (
    ("trending", "Trending Today", "What people are watching across movies and TV today.", lambda t: _cards(t.trending("all", "day"))),
    ("popular_movies", "Popular Movies", "The most popular movies right now.", lambda t: _cards(t.popular_movies(), "movie")),
    ("popular_shows", "Popular TV Shows", "The most popular shows right now.", lambda t: _cards(t.popular_tv(), "show")),
    ("upcoming", "Upcoming Releases", "Movies coming to cinemas soon, soonest first.", None),
)


def _upcoming(tmdb, today):
    cards = _cards(tmdb.upcoming_movies(), "movie", today=today, upcoming=True)
    return sorted(cards, key=lambda c: c["date"])


def tmdb_sections(tmdb, limit, today=None):
    """[{'key', 'title', 'blurb', 'cards'|'error'}] for the four TMDB lists, age-rating limited (None: no limit) and annotated with where each stands in SyncHub."""
    today = today or date.today().isoformat()
    out = []
    for key, title, blurb, loader in TMDB_SECTIONS:
        try:
            raw = cached(("tmdb", key), (lambda: _upcoming(tmdb, today)) if key == "upcoming" else (lambda: loader(tmdb)))
            cards = ratings.for_results(tmdb, [dict(c) for c in raw[:PER_SECTION * 2]], limit)[:PER_SECTION]
            with db.connection() as conn:
                search.annotate(conn, cards)
            out.append({"key": key, "title": title, "blurb": blurb, "cards": cards})
        except (ApiError, ConfigError) as exc:
            out.append({"key": key, "title": title, "blurb": blurb, "cards": [], "error": str(exc)})
        except Exception as exc:
            logger.warning(f"Discover: could not read '{key}' from TMDB: {exc}")
            out.append({"key": key, "title": title, "blurb": blurb, "cards": [], "error": "Could not reach TMDB."})
    return out


def recently_added(conn, limit, max_rating=None):
    """The newest titles in the Jellyfin library (from what SyncHub already knows; no network)."""
    rows = [r for r in catalog.list_catalog(conn, max_rating=max_rating) if r["date_added"] and r["jellyfin_link"]]
    rows.sort(key=lambda r: r["date_added"], reverse=True)
    return rows[:PER_SECTION]


def _in_days(days, today=None):
    start = date.fromisoformat(today) if today else date.today()
    return start.isoformat(), (start + timedelta(days=days)).isoformat()


def coming_up(conn, max_rating=None, today=None):
    """Releases in the next few weeks among the movies Radarr and shows Sonarr already follow: [{'title', 'kind', 'date', 'detail', 'url'}], soonest first.
    A service that is not set up or does not answer adds nothing. Returns (items, notes)."""
    start, end = _in_days(CALENDAR_DAYS, today)
    items, notes = [], []
    known = {(r["kind"], r["tmdb_id"]): r for r in catalog.list_catalog(conn, max_rating=max_rating)}
    try:
        for m in RadarrClient().calendar(start, end):
            when = (m.get("digitalRelease") or m.get("physicalRelease") or m.get("inCinemas") or "")[:10]
            if not when or not (start <= when <= end):
                continue
            row = known.get(("movie", str(m.get("tmdbId"))))
            if max_rating is not None and not row:
                continue
            items.append({"title": m.get("title"), "kind": "movie", "date": when, "detail": "Movie release", "url": row["url"] if row else None})
    except ConfigError:
        pass
    except Exception as exc:
        logger.warning(f"Discover: Radarr calendar failed: {exc}")
        notes.append("Radarr's calendar could not be read.")
    try:
        shows = {}
        for e in SonarrClient().calendar(start, end):
            when = (e.get("airDate") or "")[:10]
            series = e.get("series") or {}
            if not when or not series.get("title"):
                continue
            row = known.get(("show", str(series.get("tmdbId")))) if series.get("tmdbId") else None
            if max_rating is not None and not row:
                continue
            entry = shows.setdefault(series["title"], {"title": series["title"], "kind": "show", "date": when, "episodes": [], "url": row["url"] if row else None})
            entry["date"] = min(entry["date"], when)
            entry["episodes"].append(f"S{e.get('seasonNumber')}E{e.get('episodeNumber')}")
        for entry in shows.values():
            eps = entry.pop("episodes")
            entry["detail"] = eps[0] if len(eps) == 1 else f"{eps[0]} and {len(eps) - 1} more"
            items.append(entry)
    except ConfigError:
        pass
    except Exception as exc:
        logger.warning(f"Discover: Sonarr calendar failed: {exc}")
        notes.append("Sonarr's calendar could not be read.")
    items.sort(key=lambda i: (i["date"], i["title"] or ""))
    return items[:PER_SECTION * 2], notes


# ---------------------------------------------------------------- the daily list
def picks_text(sections, limit_each=4):
    """A short message from the sections: a line for each of Trending Today, Popular and Upcoming, a few titles long."""
    lines = []
    for s in sections:
        if s["key"] in ("trending", "upcoming") and s["cards"]:
            names = [f"{c['title']}" + (f" ({c['year']})" if c.get("year") else "") for c in s["cards"][:limit_each]]
            lines.append(f"{s['title']}: " + ", ".join(names))
    return "\n".join(lines)


def send_daily_picks(now=None):
    """Sends the day's Trending and Upcoming titles to the people who switched 'Daily picks' on. Returns how many notifications were queued.
    Each person's age limit applies. Network calls happen with no database connection open."""
    from synchub.notifications import emit, store
    try:
        tmdb = TmdbClient()
    except ConfigError:
        return 0
    with db.connection() as conn:
        people = [dict(r) for r in conn.execute("SELECT id, name, max_rating FROM profile WHERE is_active = 1")]
        wanting = [p for p in people if store.wants(conn, p["id"], "daily_picks")]
    queued = 0
    stamp = (now or db.now())[:10]
    for p in wanting:
        sections = tmdb_sections(tmdb, ratings.parse_level(p["max_rating"]))
        text = picks_text(sections)
        if not text:
            continue
        with db.connection() as conn:
            queued += len(emit.notify(conn, "daily_picks", f"picks:{stamp}:profile:{p['id']}", {"text": text, "date": stamp}, profile_ids=[p["id"]], now=now))
    return queued
