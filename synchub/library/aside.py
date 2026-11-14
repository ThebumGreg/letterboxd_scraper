"""Setting an exact duplicate aside, and putting it back.

An exact duplicate (the same SHA-256 as another file) can be renamed on the share to `<name>.synchub-duplicate`. Jellyfin, Radarr and Sonarr only see video file
names, so they ignore it; nothing is deleted and "Put it back" renames it again. It is an ordinary `rename` job (the worker needs the "aside" feature, so an older
worker never gets one), and it never touches the file kept as the primary candidate or the last copy of a film.
"""
from synchub import db, jobs
from synchub.library import file_labels, store
from synchub.library import vocab as v

REF = "library_aside"
SUFFIX = ".synchub-duplicate"
NEEDS = ["aside"]


class AsideError(ValueError):
    pass


def _busy(conn, record_id):
    return conn.execute("SELECT 1 FROM job WHERE ref_type = ? AND ref_id = ? AND status IN ('queued', 'running', 'needs_review')", (REF, record_id)).fetchone() is not None


def request(conn, record_id, undo=False, now=None):
    """Queues the rename. Returns the job id. Raises AsideError with a plain reason when it is not allowed."""
    rec = store.get_record(conn, record_id)
    if rec is None:
        raise AsideError("That file is not in the Library.")
    if _busy(conn, record_id):
        raise AsideError("A worker is already doing something with this file.")
    root, rel = rec["root_name"], v.clean_relative(rec["relative_path"])
    if undo:
        if rec.get("dup_review") != "aside":
            raise AsideError("This file is not set aside.")
        source, dest, title = (root, rel + SUFFIX), (root, rel), f"Put back: {rec['file_name']}"
    else:
        if rec.get("missing_since"):
            raise AsideError("This file was not found at its place when the share was last scanned.")
        if not rec.get("duplicate_group") or rec.get("duplicate_status") != v.DUP_OTHER:
            raise AsideError("Only an exact duplicate that is not the copy to keep can be set aside.")
        others = conn.execute("SELECT COUNT(*) FROM library_file WHERE duplicate_group = ? AND id != ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside'",
                              (rec["duplicate_group"], record_id)).fetchone()[0]
        if others < 1:
            raise AsideError("This is the last copy of the file, so it stays.")
        source, dest, title = (root, rel), (root, rel + SUFFIX), f"Set aside duplicate: {rec['file_name']}"
    plan = {"op": "rename", "aside": True, "needs": NEEDS, "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "undo": undo}
    job_id = jobs.create(conn, "library", title=title, ref=(REF, record_id), source=source, dest=dest, plan=plan, need_bytes=0, now=now)
    store.event(conn, record_id, "system", "aside_undo_queued" if undo else "aside_queued", None, {"job": job_id})
    return job_id


def sync(conn, job_id, now=None):
    """Makes the record follow its job. Returns True when Jellyfin should be asked to scan."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    rec = store.get_record(conn, job["ref_id"])
    if rec is None or job["status"] != "done":
        if rec is not None and job["status"] in ("failed", "cancelled", "needs_review"):
            store.event(conn, rec["id"], "worker", f"aside_{job['status']}", None, {"job": job_id, "why": (job["error"] or job["review_reason"] or job["message"] or "")[:300]})
        return False
    undo = bool((job["plan"] or {}).get("undo"))
    store._update(conn, rec["id"], {"dup_review": None if undo else "aside"})
    store.event(conn, rec["id"], "worker", "aside_undone" if undo else "aside_done", None, {"job": job_id})
    if rec.get("media_id"):
        file_labels.refresh(conn, [rec["media_id"]])
    return True
