"""Links Jellyfin and Sonarr episodes to the TMDB episode rows of their show.

Jellyfin numbers episodes like TMDB, so (season, episode) is exact. Sonarr uses TVDB numbering: it is matched on
(season, episode) only when the air dates agree, otherwise on air date + title, and the TVDB numbering is stored
on the episode so requests to Sonarr can use it.
"""
import re
from collections import Counter

from synchub import db
from synchub.matching import _within_a_day
from synchub.text import norm_title


def titles_similar(a, b):
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return False
    if a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)):
        return True
    ta, tb = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.6


def _save(conn, source, key, episode_id, method, confidence, now):
    conn.execute(
        """INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence, status)
           VALUES ('episode', ?, ?, ?, ?, ?, 'auto')
           ON CONFLICT(source, source_key) DO UPDATE SET entity_id = excluded.entity_id, method = excluded.method,
             confidence = excluded.confidence, updated_at = ? WHERE link.status = 'auto'""",
        (episode_id, source, key, method, confidence, now))


def _episodes_of(conn, media_id):
    rows = conn.execute("SELECT * FROM episode WHERE media_id = ?", (media_id,)).fetchall()
    return {(r["season_number"], r["episode_number"]): r for r in rows}, rows


def link_jellyfin_episodes(conn, now=None):
    now = now or db.now()
    stats = Counter()
    shows = conn.execute("SELECT source_key AS series_id, entity_id AS media_id FROM link WHERE source = 'jellyfin' AND entity_type = 'media'").fetchall()
    media_of_series = {r["series_id"]: r["media_id"] for r in shows}
    cache = {}
    for ep in conn.execute("SELECT * FROM src_jellyfin_item WHERE type = 'episode' AND removed_at IS NULL AND series_id IS NOT NULL").fetchall():
        media_id = media_of_series.get(ep["series_id"])
        if not media_id:
            stats["series_not_linked"] += 1
            continue
        by_number, rows = cache.setdefault(media_id, _episodes_of(conn, media_id))
        target = by_number.get((ep["season_number"], ep["episode_number"]))
        if target is not None:
            _save(conn, "jellyfin_episode", ep["item_id"], target["id"], "series+season_episode", 1.0, now)
            stats["linked"] += 1
            continue
        fallback = [r for r in rows if ep["premiere_date"] and r["air_date"] and _within_a_day(r["air_date"], ep["premiere_date"]) and titles_similar(r["title"], ep["name"])]
        if len(fallback) == 1:
            _save(conn, "jellyfin_episode", ep["item_id"], fallback[0]["id"], "air_date+title", 0.8, now)
            stats["linked_by_date"] += 1
        else:
            stats["no_tmdb_episode"] += 1
    return dict(stats)


def map_sonarr_episodes(conn, now=None):
    now = now or db.now()
    stats = Counter()
    shows = conn.execute("SELECT source_key AS sonarr_id, entity_id AS media_id FROM link WHERE source = 'sonarr' AND entity_type = 'media'").fetchall()
    for show in shows:
        by_number, rows = _episodes_of(conn, show["media_id"])
        sonarr_eps = conn.execute("SELECT * FROM src_sonarr_episode WHERE sonarr_id = ? AND removed_at IS NULL", (show["sonarr_id"],)).fetchall()
        taken, leftover = set(), []
        for s in sonarr_eps:
            target = by_number.get((s["season_number"], s["episode_number"]))
            if target is None or target["id"] in taken:
                leftover.append(s)
                continue
            if s["air_date"] and target["air_date"] and not _within_a_day(s["air_date"], target["air_date"]):
                leftover.append(s)       # same numbers but a different broadcast date: the numbering differs
                continue
            taken.add(target["id"])
            confident = bool(s["air_date"] and target["air_date"])
            _save(conn, "sonarr_episode", s["sonarr_episode_id"], target["id"], "season_episode+air_date" if confident else "season_episode", 0.95 if confident else 0.85, now)
            conn.execute("UPDATE episode SET tvdb_episode_id = ?, tvdb_season_number = ?, tvdb_episode_number = ?, numbering_status = 'same' WHERE id = ?",
                         (s["tvdb_episode_id"], s["season_number"], s["episode_number"], target["id"]))
            stats["same_numbering"] += 1
        free = [r for r in rows if r["id"] not in taken]
        for s in leftover:
            hits = [r for r in free if s["air_date"] and r["air_date"] and _within_a_day(s["air_date"], r["air_date"]) and (titles_similar(s["title"], r["title"]) or not s["title"])]
            if len(hits) != 1:
                stats["unmapped"] += 1
                continue
            target = hits[0]
            free.remove(target)
            _save(conn, "sonarr_episode", s["sonarr_episode_id"], target["id"], "air_date+title", 0.8, now)
            conn.execute("UPDATE episode SET tvdb_episode_id = ?, tvdb_season_number = ?, tvdb_episode_number = ?, numbering_status = 'mapped' WHERE id = ?",
                         (s["tvdb_episode_id"], s["season_number"], s["episode_number"], target["id"]))
            stats["mapped"] += 1
    return dict(stats)
