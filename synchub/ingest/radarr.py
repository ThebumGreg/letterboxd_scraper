from synchub import db
from synchub.ingest.common import mark_unseen, touch
from synchub.text import clean_id


def sync_library(conn, client, now=None):
    now = now or db.now()
    movies = client.movies()
    seen = []
    for m in movies:
        touch(conn, "src_radarr_movie", ("radarr_id",), {
            "radarr_id": str(m["id"]), "tmdb_id": clean_id(m.get("tmdbId")), "imdb_id": clean_id(m.get("imdbId")),
            "title": m.get("title"), "year": m.get("year") or None,
            "monitored": 1 if m.get("monitored") else 0, "has_file": 1 if m.get("hasFile") else 0,
        }, now)
        seen.append(str(m["id"]))
    return {"seen": len(seen), "removed": mark_unseen(conn, "src_radarr_movie", ("radarr_id",), seen, now)}


def sync_queue(conn, client, now=None):
    now = now or db.now()
    seen = []
    for rec in client.queue():
        if not rec.get("movieId"):
            continue
        queue_id = str(rec["id"])
        size, left = rec.get("size") or 0, rec.get("sizeleft") or 0
        touch(conn, "src_arr_queue", ("service", "queue_id"), {
            "service": "radarr", "queue_id": queue_id, "kind": "movie", "target_id": str(rec["movieId"]),
            "title": rec.get("title"), "status": rec.get("trackedDownloadState") or rec.get("status"),
            "progress": round(1 - left / size, 4) if size else None, "size": size, "time_left": rec.get("timeleft"),
        }, now)
        seen.append((("radarr", queue_id)))
    removed = mark_unseen(conn, "src_arr_queue", ("service", "queue_id"), seen, now, scope_sql="service = 'radarr'", allow_empty=True)
    return {"seen": len(seen), "removed": removed}
