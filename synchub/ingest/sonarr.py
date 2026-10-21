import logging

from synchub import db
from synchub.ingest.common import mark_unseen, touch
from synchub.text import clean_id

logger = logging.getLogger(__name__)


def sync_library(conn, client, now=None):
    """Series and all their episodes. A series whose episode list cannot be fetched keeps its previous episode rows."""
    now = now or db.now()
    series_list = client.series()
    seen_series, episodes_seen, errors = [], 0, []
    for s in series_list:
        sid = str(s["id"])
        touch(conn, "src_sonarr_series", ("sonarr_id",), {
            "sonarr_id": sid, "tvdb_id": clean_id(s.get("tvdbId")), "tmdb_id": clean_id(s.get("tmdbId")), "imdb_id": clean_id(s.get("imdbId")),
            "title": s.get("title"), "year": s.get("year") or None, "monitored": 1 if s.get("monitored") else 0,
        }, now)
        seen_series.append(sid)
        try:
            episodes = client.episodes(s["id"])
        except Exception as exc:
            logger.warning(f"Sonarr episodes for series {sid} could not be fetched: {exc}")
            errors.append(sid)
            continue
        seen_eps = []
        for ep in episodes:
            eid = str(ep["id"])
            touch(conn, "src_sonarr_episode", ("sonarr_episode_id",), {
                "sonarr_episode_id": eid, "sonarr_id": sid, "season_number": ep.get("seasonNumber"), "episode_number": ep.get("episodeNumber"),
                "tvdb_episode_id": clean_id(ep.get("tvdbId")), "title": ep.get("title"), "air_date": ep.get("airDate") or None,
                "monitored": 1 if ep.get("monitored") else 0, "has_file": 1 if ep.get("hasFile") else 0,
            }, now)
            seen_eps.append(eid)
        episodes_seen += len(seen_eps)
        mark_unseen(conn, "src_sonarr_episode", ("sonarr_episode_id",), seen_eps, now, scope_sql="sonarr_id = ?", scope_params=(sid,))
    removed = mark_unseen(conn, "src_sonarr_series", ("sonarr_id",), seen_series, now)
    conn.execute("UPDATE src_sonarr_episode SET removed_at = ? WHERE removed_at IS NULL AND sonarr_id IN (SELECT sonarr_id FROM src_sonarr_series WHERE removed_at IS NOT NULL)", (now,))
    return {"series": len(seen_series), "episodes": episodes_seen, "removed": removed, "episode_fetch_errors": errors}


def sync_queue(conn, client, now=None):
    now = now or db.now()
    seen = []
    for rec in client.queue():
        if not rec.get("episodeId"):
            continue
        queue_id = str(rec["id"])
        size, left = rec.get("size") or 0, rec.get("sizeleft") or 0
        touch(conn, "src_arr_queue", ("service", "queue_id"), {
            "service": "sonarr", "queue_id": queue_id, "kind": "episode", "target_id": str(rec["episodeId"]),
            "title": rec.get("title"), "status": rec.get("trackedDownloadState") or rec.get("status"),
            "progress": round(1 - left / size, 4) if size else None, "size": size, "time_left": rec.get("timeleft"),
        }, now)
        seen.append(("sonarr", queue_id))
    removed = mark_unseen(conn, "src_arr_queue", ("service", "queue_id"), seen, now, scope_sql="service = 'sonarr'", allow_empty=True)
    return {"seen": len(seen), "removed": removed}
