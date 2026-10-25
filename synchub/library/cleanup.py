"""After a file has been brought into the library: delete the source later, and tell Radarr/Sonarr where the file went.

**Source deletion** is off by default (`source_delete_after_days` = 0). When it is 7, 14 or 30, a verified copy or conversion arms a timer on the
record. When the timer runs out, a `delete_source` job goes to a worker, which checks that the destination still holds the verified file and that
the source is still the file that was copied, and only then deletes the source (and empties the folders it leaves behind, never the share
itself). A person can keep a file's source for good, and turning the setting back to Off stops every pending deletion.

**Radarr and Sonarr** keep their own idea of where a title's file is. `arr_after_migration` says what they are told: nothing, the new location,
stop monitoring, or (Radarr only) forget the title without touching any file. `arr_apply_when` says whether that happens right after the copy or
only when the source is deleted. A rename inside a share is different: the file never leaves the share, so the new location is always passed on
(unless the setting is "nothing").
"""
import json
import logging
import posixpath
import re
from datetime import datetime, timedelta

from synchub import db, jobs, settings, storage
from synchub.library import store
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

SOURCE_REF = "library_source"
SOURCE_STATUS = {"queued": "queued", "running": "queued", "needs_review": "failed", "done": "deleted", "failed": "failed", "cancelled": "held", "skipped": "held"}
EPISODE = re.compile(r"[sS](\d{1,3})[eE](\d{1,4})")


def source_days(conn=None):
    try:
        return int(settings.get("source_delete_after_days", conn=conn))
    except ValueError:
        return 0


def arr_mode(conn=None):
    return settings.get("arr_after_migration", conn=conn)


def _later(now, days):
    return (datetime.strptime(now, "%Y-%m-%d %H:%M:%S") + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _same_file(job):
    return job["source_root"] == job["dest_root"] and v.clean_relative(job["source_rel"]).casefold() == v.clean_relative(job["dest_rel"]).casefold()


# ---------------------------------------------------------------- when a migration finishes
def after_migration(conn, record_id, job, now=None):
    """Called when a record's job has finished and been verified: moves a renamed record to its new path, arms the source deletion and
    the Radarr/Sonarr message according to the settings."""
    now = now or db.now()
    how = job["plan"].get("op")
    if how == "rename":
        store.move_record(conn, record_id, job["dest_rel"])
    days = source_days(conn)
    values = {"source_delete_status": None, "source_delete_due_at": None, "source_delete_message": None, "source_deleted_at": None, "arr_status": None, "arr_message": None}
    deleting = how in ("copy", "convert") and not _same_file(job) and not job["plan"].get("replace") and days > 0
    if deleting:
        values.update(source_delete_status="waiting", source_delete_due_at=_later(now, days), source_delete_message=f"The source is deleted {days} days after the copy was verified.")
    mode = arr_mode(conn)
    if mode != "none":
        if how == "rename" or settings.get("arr_apply_when", conn=conn) == "after_copy":
            values.update(arr_status="due", arr_message="Waiting to tell Radarr/Sonarr.")
        elif deleting:
            values.update(arr_status="waiting", arr_message="Radarr/Sonarr are told when the source is deleted.")
        else:
            values.update(arr_status="skipped", arr_message="Set to tell Radarr/Sonarr when the source is deleted, but source deletion is off.")
    store._update(conn, record_id, values)
    store.event(conn, record_id, "worker", "migration_followup", None, {"source_delete": values["source_delete_status"], "arr": values["arr_status"], "due": values["source_delete_due_at"]}, now)


# ---------------------------------------------------------------- deleting the source
def sweep(conn, now=None):
    """Queues the deletion job of every source whose timer has run out. Does nothing while the setting is Off. Returns how many were queued."""
    if source_days(conn) == 0:
        return 0
    now = now or db.now()
    due = conn.execute("SELECT id FROM library_file WHERE source_delete_status = 'waiting' AND source_delete_due_at <= ? AND migration_status = 'completed' "
                       "AND migrated_source_revision = source_revision ORDER BY id", (now,)).fetchall()
    return sum(1 for r in due if queue_delete(conn, r["id"], now) is not None)


def queue_delete(conn, record_id, now=None):
    rec = store.get_record(conn, record_id)
    if rec is None or rec["source_delete_status"] != "waiting" or not rec.get("migration_destination_path") or ":" not in rec["migration_destination_path"]:
        return None
    dest_root, dest_rel = rec["migration_destination_path"].split(":", 1)
    plan = {"op": "delete_source", "revision": rec["source_revision"], "source_size": rec["size_bytes"], "source_mtime": rec["mtime"],
            "dest_size": rec.get("migrated_dest_size"), "dest_sha256": rec.get("migrated_dest_sha256")}
    job_id = jobs.create(conn, "library", title=f"Delete source: {rec['file_name']}", ref=(SOURCE_REF, record_id), source=(rec["root_name"], rec["relative_path"]),
                         dest=(dest_root, dest_rel), plan=plan, need_bytes=0)
    store._update(conn, record_id, {"source_delete_status": "queued", "source_delete_message": "Queued for a worker."})
    store.event(conn, record_id, "system", "source_delete_queued", None, {"job": job_id})
    return job_id


def hold(conn, record_id, by):
    """Keeps this file's source for good (until released)."""
    rec = store.get_record(conn, record_id)
    if rec is None or rec["source_delete_status"] not in ("waiting", "failed", "held"):
        raise ValueError("There is no pending deletion for this file.")
    store._update(conn, record_id, {"source_delete_status": "held", "source_delete_message": f"Kept by {by}. The source is not deleted.", "source_delete_due_at": None})
    store.event(conn, record_id, "user", "source_delete_held", None, {"by": by})


def release(conn, record_id, by):
    """Starts the timer again, from now, for a source that was kept or whose deletion failed."""
    days = source_days(conn)
    rec = store.get_record(conn, record_id)
    if rec is None or rec["source_delete_status"] not in ("held", "failed"):
        raise ValueError("That file's source is not being kept.")
    if days == 0:
        raise ValueError("Source deletion is Off in the settings. Choose 7, 14 or 30 days first.")
    store._update(conn, record_id, {"source_delete_status": "waiting", "source_delete_due_at": _later(db.now(), days),
                                    "source_delete_message": f"The source is deleted {days} days from now."})
    store.event(conn, record_id, "user", "source_delete_released", None, {"by": by, "days": days})


def sync(conn, job_id, now=None):
    """Makes the record follow its deletion job."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != SOURCE_REF:
        return False
    rec = store.get_record(conn, job["ref_id"])
    if rec is None or job["plan"].get("revision") != rec["source_revision"]:
        return False
    target, now = SOURCE_STATUS.get(job["status"], "queued"), now or db.now()
    if rec["source_delete_status"] == target:
        return False
    values = {"source_delete_status": target}
    if target == "deleted":
        result = job["result"] if isinstance(job["result"], dict) else {}
        values.update(source_deleted_at=now, source_delete_message="The source was deleted" + (" (it was already gone)." if result.get("already_gone") else "."))
        if rec.get("arr_status") == "waiting":
            values.update(arr_status="due", arr_message="Waiting to tell Radarr/Sonarr.")
    elif target == "failed":
        values["source_delete_message"] = (job["error"] or job["review_reason"] or job["message"] or "The deletion did not finish.")[:500]
    elif target == "held":
        values["source_delete_message"] = "The deletion was cancelled. The source is kept."
    else:
        values["source_delete_message"] = "Queued for a worker."
    store._update(conn, rec["id"], values)
    store.event(conn, rec["id"], "worker", f"source_delete_{target}", None, {"job": job_id, "error": job["error"]}, now)
    return target == "deleted"


def sync_active(conn):
    rows = conn.execute("SELECT j.id FROM job j JOIN library_file f ON f.id = j.ref_id WHERE j.ref_type = ? AND f.source_delete_status IN ('queued', 'failed') "
                        "AND j.id = (SELECT MAX(id) FROM job WHERE ref_type = ? AND ref_id = f.id)", (SOURCE_REF, SOURCE_REF)).fetchall()
    return sum(1 for r in rows if sync(conn, r["id"]))


# ---------------------------------------------------------------- Radarr and Sonarr
def _abs(root, service, rel):
    base = (root or {}).get(f"{service}_path") or ""
    return posixpath.join(base.rstrip("/"), v.clean_relative(rel)) if base else None


def _inside(path, folder):
    path, folder = (path or "").rstrip("/").casefold(), (folder or "").rstrip("/").casefold()
    return bool(folder) and (path == folder or path.startswith(folder + "/"))


def _context(conn, rec, job):
    """What the Radarr/Sonarr step needs, gathered while a database connection is open."""
    service = "sonarr" if rec.get("media_type") == "TV Episode" else "radarr"
    src_root, dst_root = storage.by_name(conn, job["source_root"]), storage.by_name(conn, job["dest_root"])
    others = 0
    if service == "sonarr" and rec.get("show_title"):
        others = conn.execute("SELECT COUNT(*) FROM library_file WHERE id != ? AND show_title = ? AND root_name = ? AND missing_since IS NULL AND migration_status != 'completed'",
                              (rec["id"], rec["show_title"], rec["root_name"])).fetchone()[0]
    episode = EPISODE.search(rec.get("recommended_name") or rec.get("file_name") or "")
    return {"service": service, "how": job["plan"].get("op"), "mode": arr_mode(conn), "title": rec.get("show_title") or rec.get("recommended_name"),
            "source": _abs(src_root, service, job["source_rel"]), "dest": _abs(dst_root, service, job["dest_rel"]),
            "episode": (int(episode.group(1)), int(episode.group(2))) if episode else None, "others_in_old_folder": others}


def run_arr(ctx, radarr=None, sonarr=None):
    """Does the Radarr/Sonarr step for one finished file. Returns (status, message). Makes network calls: no transaction may be open. Never raises."""
    try:
        if not ctx["source"] or (ctx["mode"] == "update_location" or ctx["how"] == "rename") and not ctx["dest"]:
            return "skipped", f"Set the {ctx['service'].title()} path of the share on the Storage page so {ctx['service'].title()} can be told."
        return (_radarr if ctx["service"] == "radarr" else _sonarr)(ctx, radarr if ctx["service"] == "radarr" else sonarr)
    except Exception as exc:
        logger.warning(f"[library] could not update {ctx['service']} for {ctx['title']}: {exc}")
        return "failed", str(exc)[:300]


def _radarr(ctx, client=None):
    from synchub.clients.arr import RadarrClient
    client = client or RadarrClient()
    movie = next((m for m in client.movies() if _inside(ctx["source"], m.get("path")) or _inside(ctx["dest"], m.get("path"))), None)
    if movie is None:
        return "skipped", "Radarr does not have a movie for this file, so there was nothing to update."
    if ctx["mode"] == "remove" and ctx["how"] != "rename":
        client.delete_movie(movie["id"], delete_files=False)
        return "done", f"Radarr no longer tracks “{movie.get('title')}”. No file was deleted."
    if ctx["mode"] == "unmonitor" and ctx["how"] != "rename":
        client.update_movie(movie["id"], dict(movie, monitored=False))
        return "done", f"Radarr no longer monitors “{movie.get('title')}”."
    folder = posixpath.dirname(ctx["dest"])
    if movie.get("path", "").rstrip("/") != folder.rstrip("/"):
        client.update_movie(movie["id"], dict(movie, path=folder))
    client.rescan_movie(movie["id"])
    return "done", f"Radarr now looks for “{movie.get('title')}” in {folder}."


def _sonarr(ctx, client=None):
    from synchub.clients.arr import SonarrClient
    client = client or SonarrClient()
    series = next((s for s in client.series() if _inside(ctx["source"], s.get("path"))), None)
    if series is None:
        return "skipped", "Sonarr does not have a series for this file, so there was nothing to update."
    if ctx["mode"] in ("unmonitor", "remove") and ctx["how"] != "rename":
        if not ctx["episode"]:
            return "skipped", "The episode number could not be read from the file name, so Sonarr was left alone."
        found = next((e for e in client.episodes(series["id"]) if (e.get("seasonNumber"), e.get("episodeNumber")) == ctx["episode"]), None)
        if found is None:
            return "skipped", "Sonarr has no such episode."
        client.monitor_episodes([found["id"]], False)
        note = " (Sonarr cannot forget one episode without deleting its file, so it no longer monitors it)" if ctx["mode"] == "remove" else ""
        return "done", f"Sonarr no longer monitors S{ctx['episode'][0]:02d}E{ctx['episode'][1]:02d}{note}."
    show_folder = posixpath.dirname(posixpath.dirname(ctx["dest"]))
    if series.get("path", "").rstrip("/").casefold() != show_folder.rstrip("/").casefold():
        if ctx["others_in_old_folder"]:
            return "skipped", f"{ctx['others_in_old_folder']} other episode(s) of this show are still in the old folder, so Sonarr's folder was not moved. Rescan it yourself once they are done."
        client.update_series(series["id"], dict(series, path=show_folder), move_files=False)
    client.rescan_series(series["id"])
    return "done", f"Sonarr now looks for “{series.get('title')}” in {show_folder}."


def arr_due(radarr=None, sonarr=None):
    """Tells Radarr/Sonarr about every finished file that is due. Makes network calls: call it with no transaction open. Returns how many were handled."""
    with db.connection() as conn:
        due = [dict(r) for r in conn.execute("SELECT * FROM library_file WHERE arr_status = 'due' ORDER BY id")]
        work = []
        for rec in due:
            job = conn.execute("SELECT * FROM job WHERE ref_type = 'library_file' AND ref_id = ? AND status = 'done' ORDER BY id DESC LIMIT 1", (rec["id"],)).fetchone()
            job = jobs.row(job) if job else None
            work.append((rec, _context(conn, rec, job) if job else None))
    for rec, ctx in work:
        status, message = run_arr(ctx, radarr, sonarr) if ctx else ("skipped", "The finished job could not be found.")
        with db.connection() as conn:
            store._update(conn, rec["id"], {"arr_status": status, "arr_message": message})
            store.event(conn, rec["id"], "system", f"arr_{status}", None, {"message": message})
    return len(work)


# ---------------------------------------------------------------- what the pages show
def summary(conn):
    counts = {r["source_delete_status"]: r["n"] for r in conn.execute("SELECT source_delete_status, COUNT(*) AS n FROM library_file WHERE source_delete_status IS NOT NULL GROUP BY source_delete_status")}
    nxt = conn.execute("SELECT MIN(source_delete_due_at) FROM library_file WHERE source_delete_status = 'waiting'").fetchone()[0]
    arr = {r["arr_status"]: r["n"] for r in conn.execute("SELECT arr_status, COUNT(*) AS n FROM library_file WHERE arr_status IS NOT NULL GROUP BY arr_status")}
    return {"days": source_days(conn), "waiting": counts.get("waiting", 0), "queued": counts.get("queued", 0), "deleted": counts.get("deleted", 0), "held": counts.get("held", 0),
            "failed": counts.get("failed", 0), "next_due": nxt, "arr_mode": arr_mode(conn), "arr_when": settings.get("arr_apply_when", conn=conn),
            "arr_failed": arr.get("failed", 0), "arr_waiting": arr.get("waiting", 0) + arr.get("due", 0)}
