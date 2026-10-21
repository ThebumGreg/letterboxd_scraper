"""Item states, computed from what the sources report and from open processing events.

The strongest signal wins (highest state_def.rank), per movie and per episode:
  in Jellyfin -> in_library;  open processing event -> its state;  file in Radarr/Sonarr -> downloaded;
  monitored with no file -> queued;  otherwise missing.
Radarr/Sonarr queue entries are turned into processing events, the same mechanism future transfer/encode scripts use.
"""
from datetime import date

from synchub import db

# Radarr/Sonarr trackedDownloadState (or status) -> state_def code. Unknown values count as downloading.
QUEUE_STATE_MAP = {
    "downloading": "downloading", "paused": "downloading", "queued": "downloading", "delay": "downloading",
    "downloadClientUnavailable": "downloading", "warning": "downloading",
    "importPending": "downloaded", "importing": "downloaded", "importBlocked": "downloaded", "completed": "downloaded",
}
QUEUE_IGNORED = {"failed", "failedPending", "ignored"}
QUEUE_PROVIDERS = {"radarr": "radarr_queue", "sonarr": "sonarr_queue"}
NOT_AIRED = "not_aired"     # a display state only; never stored


# ---------------------------------------------------------------- queue -> processing events
def sync_queue_events(conn, now=None):
    """Opens, updates and closes processing events so they mirror the current Radarr/Sonarr queues."""
    now = now or db.now()
    stats = {"opened": 0, "updated": 0, "closed": 0, "unlinked": 0, "ignored": 0}
    wanted = {}    # (provider, queue_id) -> (target_type, target_id, state, progress, detail)
    rows = conn.execute("SELECT * FROM src_arr_queue WHERE removed_at IS NULL").fetchall()
    for q in rows:
        provider = QUEUE_PROVIDERS[q["service"]]
        if q["status"] in QUEUE_IGNORED:
            stats["ignored"] += 1
            continue
        if q["kind"] == "movie":
            link = conn.execute("SELECT entity_id FROM link WHERE source = 'radarr' AND entity_type = 'media' AND source_key = ?", (q["target_id"],)).fetchone()
            target_type = "movie"
        else:
            link = conn.execute("SELECT entity_id FROM link WHERE source = 'sonarr_episode' AND entity_type = 'episode' AND source_key = ?", (q["target_id"],)).fetchone()
            target_type = "episode"
        if not link:
            stats["unlinked"] += 1
            continue
        wanted[(provider, q["queue_id"])] = (target_type, link["entity_id"], QUEUE_STATE_MAP.get(q["status"], "downloading"), q["progress"], q["title"])

    open_events = {(r["provider"], r["external_ref"]): r for r in conn.execute(
        "SELECT * FROM processing_event WHERE ended_at IS NULL AND provider IN ('radarr_queue', 'sonarr_queue')")}
    for key, (target_type, target_id, state, progress, detail) in wanted.items():
        event = open_events.get(key)
        if event is None:
            conn.execute("INSERT INTO processing_event (target_type, target_id, provider, state_code, progress, detail, external_ref, started_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (target_type, target_id, key[0], state, progress, detail, key[1], now, now))
            stats["opened"] += 1
        elif (event["target_type"], event["target_id"], event["state_code"], event["progress"]) != (target_type, target_id, state, progress):
            conn.execute("UPDATE processing_event SET target_type = ?, target_id = ?, state_code = ?, progress = ?, updated_at = ? WHERE id = ?",
                         (target_type, target_id, state, progress, now, event["id"]))
            stats["updated"] += 1
    for key, event in open_events.items():
        if key not in wanted:
            conn.execute("UPDATE processing_event SET ended_at = ? WHERE id = ?", (now, event["id"]))
            stats["closed"] += 1
    return stats


# ---------------------------------------------------------------- state computation
def _best_events(conn, ranks):
    best = {}
    for e in conn.execute("SELECT target_type, target_id, state_code, progress FROM processing_event WHERE ended_at IS NULL"):
        key = (e["target_type"], e["target_id"])
        if key not in best or ranks[e["state_code"]] > ranks[best[key][0]]:
            best[key] = (e["state_code"], e["progress"])
    return best


def compute(conn, now=None):
    """Recomputes item_state for every movie and episode. Rows are updated in place; updated_at moves only on a change."""
    now = now or db.now()
    ranks = {r["code"]: r["rank"] for r in conn.execute("SELECT code, rank FROM state_def")}
    events = _best_events(conn, ranks)
    results = []

    movies = conn.execute("""
        SELECT m.id,
          EXISTS (SELECT 1 FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key
                  WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = m.id AND j.type = 'movie' AND j.removed_at IS NULL) AS in_library,
          (SELECT MAX(r.has_file) FROM link l JOIN src_radarr_movie r ON r.radarr_id = l.source_key
                  WHERE l.source = 'radarr' AND l.entity_type = 'media' AND l.entity_id = m.id AND r.removed_at IS NULL) AS has_file,
          (SELECT MAX(r.monitored) FROM link l JOIN src_radarr_movie r ON r.radarr_id = l.source_key
                  WHERE l.source = 'radarr' AND l.entity_type = 'media' AND l.entity_id = m.id AND r.removed_at IS NULL) AS monitored
        FROM media m WHERE m.kind = 'movie'""").fetchall()
    for r in movies:
        results.append(("movie", r["id"], _decide(r, events.get(("movie", r["id"])), ranks)))

    episodes = conn.execute("""
        SELECT e.id,
          EXISTS (SELECT 1 FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key
                  WHERE l.source = 'jellyfin_episode' AND l.entity_type = 'episode' AND l.entity_id = e.id AND j.removed_at IS NULL) AS in_library,
          (SELECT MAX(s.has_file) FROM link l JOIN src_sonarr_episode s ON s.sonarr_episode_id = l.source_key
                  WHERE l.source = 'sonarr_episode' AND l.entity_type = 'episode' AND l.entity_id = e.id AND s.removed_at IS NULL) AS has_file,
          (SELECT MAX(s.monitored) FROM link l JOIN src_sonarr_episode s ON s.sonarr_episode_id = l.source_key
                  WHERE l.source = 'sonarr_episode' AND l.entity_type = 'episode' AND l.entity_id = e.id AND s.removed_at IS NULL) AS monitored
        FROM episode e""").fetchall()
    for r in episodes:
        results.append(("episode", r["id"], _decide(r, events.get(("episode", r["id"])), ranks)))

    changed = 0
    for target_type, target_id, (code, progress) in results:
        cur = conn.execute(
            """INSERT INTO item_state (target_type, target_id, state_code, progress, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(target_type, target_id) DO UPDATE SET state_code = excluded.state_code, progress = excluded.progress, updated_at = excluded.updated_at
               WHERE item_state.state_code <> excluded.state_code OR item_state.progress IS NOT excluded.progress""",
            (target_type, target_id, code, progress, now))
        changed += cur.rowcount
    return {"movies": len(movies), "episodes": len(episodes), "changed": changed}


def _decide(row, event, ranks):
    candidates = []
    if row["in_library"]:
        candidates.append(("in_library", None))
    if event:
        candidates.append(event)
    if row["has_file"]:
        candidates.append(("downloaded", None))
    if row["monitored"]:
        candidates.append(("queued", None))
    candidates.append(("missing", None))
    return max(candidates, key=lambda c: ranks[c[0]])


# ---------------------------------------------------------------- reading states (what the UI uses)
def definitions(conn):
    """{code: row} for every state, plus the display-only 'not_aired'."""
    defs = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM state_def ORDER BY rank")}
    defs[NOT_AIRED] = {"code": NOT_AIRED, "label": "Not aired", "rank": -1, "color": "secondary", "icon": "bi-calendar-event", "is_terminal": 0}
    return defs


def display_state(state_code, air_date, today):
    """An episode nobody has and that has not aired (or has no air date yet) is 'Not aired', not Missing."""
    if state_code == "missing" and (not air_date or air_date > today):
        return NOT_AIRED
    return state_code


def movie_state(conn, media_id):
    row = conn.execute("SELECT state_code, progress FROM item_state WHERE target_type = 'movie' AND target_id = ?", (media_id,)).fetchone()
    return {"state": row["state_code"], "progress": row["progress"]} if row else {"state": "missing", "progress": None}


def show_summary(conn, media_id, today=None):
    """Per-season and whole-show counts. Specials (Season 0) are listed last and never counted in the totals."""
    today = today or date.today().isoformat()
    ranks = {r["code"]: r["rank"] for r in conn.execute("SELECT code, rank FROM state_def")}
    rows = conn.execute(
        """SELECT e.season_number, e.air_date, COALESCE(s.state_code, 'missing') AS state_code
           FROM episode e LEFT JOIN item_state s ON s.target_type = 'episode' AND s.target_id = e.id
           WHERE e.media_id = ?""", (media_id,)).fetchall()
    seasons = {}
    for r in rows:
        bucket = seasons.setdefault(r["season_number"], {"counts": {}, "episodes": 0})
        shown = display_state(r["state_code"], r["air_date"], today)
        bucket["counts"][shown] = bucket["counts"].get(shown, 0) + 1
        bucket["episodes"] += 1

    def finish(number, bucket):
        counts = bucket["counts"]
        aired = bucket["episodes"] - counts.get(NOT_AIRED, 0)
        in_library = counts.get("in_library", 0)
        return {"season_number": number, "is_special": number == 0,
                "label": "Specials (Season 0)" if number == 0 else f"Season {number}",
                "counts": counts, "episodes": bucket["episodes"], "aired": aired, "in_library": in_library,
                "percent": round(100 * in_library / aired) if aired else 0,
                "state": rollup(counts, aired, ranks)}

    ordered = sorted((n for n in seasons if n != 0)) + ([0] if 0 in seasons else [])      # Specials last
    season_list = [finish(n, seasons[n]) for n in ordered]
    totals = {}
    for s in season_list:
        if not s["is_special"]:
            for code, n in s["counts"].items():
                totals[code] = totals.get(code, 0) + n
    episodes = sum(s["episodes"] for s in season_list if not s["is_special"])
    aired = episodes - totals.get(NOT_AIRED, 0)
    in_library = totals.get("in_library", 0)
    return {"seasons": season_list,
            "totals": {"counts": totals, "episodes": episodes, "aired": aired, "in_library": in_library,
                       "percent": round(100 * in_library / aired) if aired else 0, "state": rollup(totals, aired, ranks)}}


def rollup(counts, aired, ranks):
    """One label for a group of episodes: in_library, partial, the strongest in-flight state, missing or not_aired."""
    if aired == 0:
        return NOT_AIRED if counts.get(NOT_AIRED) else "missing"
    in_library = counts.get("in_library", 0)
    if in_library == aired:
        return "in_library"
    if in_library:
        return "partial"
    active = [c for c in counts if c not in (NOT_AIRED, "missing")]
    return max(active, key=lambda c: ranks.get(c, 0)) if active else "missing"


def season_episodes(conn, media_id, season_number, today=None):
    today = today or date.today().isoformat()
    rows = conn.execute(
        """SELECT e.id, e.season_number, e.episode_number, e.title, e.overview, e.air_date, e.runtime,
                  e.tvdb_season_number, e.tvdb_episode_number, e.numbering_status,
                  COALESCE(s.state_code, 'missing') AS state_code, s.progress
           FROM episode e LEFT JOIN item_state s ON s.target_type = 'episode' AND s.target_id = e.id
           WHERE e.media_id = ? AND e.season_number = ? ORDER BY e.episode_number""", (media_id, season_number)).fetchall()
    return [{**dict(r), "state": display_state(r["state_code"], r["air_date"], today)} for r in rows]
