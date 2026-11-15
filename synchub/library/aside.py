"""Hiding a file of a film, and putting it back.

A file that is not the film's main file can be renamed on its share to `<name>.<suffix>`: `.synchub-duplicate` for an exact duplicate (the same SHA-256 as another file),
`.synchub-old` for an old version the main file replaced, `.synchub-hidden` for any other reason. Jellyfin, Radarr and Sonarr only see video file names, so they ignore it;
nothing is deleted and "Put it back" renames it again. It is an ordinary `rename` job (the worker needs the "aside" feature, so an older worker never gets one).
A file is never hidden if it is the main file, the copy kept in an exact-duplicate group, or the last visible file of the film. Deleting a hidden file is a separate,
confirmed step on the Cleanup page (hidden.py).
"""
from synchub import jobs
from synchub.library import file_labels, store
from synchub.library import vocab as v

REF = "library_aside"
SUFFIXES = {"duplicate": ".synchub-duplicate", "old_version": ".synchub-old", "other": ".synchub-hidden"}
REASONS = tuple(SUFFIXES)
REASON_TEXT = {"duplicate": "exact duplicate", "old_version": "old version", "other": "hidden by you"}
NEEDS = ["aside"]


class AsideError(ValueError):
    pass


def suffix_for(reason):
    return SUFFIXES.get(reason) or SUFFIXES["duplicate"]


def _busy(conn, record_id):
    return conn.execute("SELECT 1 FROM job WHERE ref_type = ? AND ref_id = ? AND status IN ('queued', 'running', 'needs_review')", (REF, record_id)).fetchone() is not None


def _visible_others(conn, rec):
    """The other files of the same film that are still visible (not hidden, not missing)."""
    if rec.get("media_id"):
        return conn.execute("SELECT COUNT(*) FROM library_file WHERE media_id = ? AND id != ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside'",
                            (rec["media_id"], rec["id"])).fetchone()[0]
    if rec.get("duplicate_group"):
        return conn.execute("SELECT COUNT(*) FROM library_file WHERE duplicate_group = ? AND id != ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside'",
                            (rec["duplicate_group"], rec["id"])).fetchone()[0]
    return 0


def request(conn, record_id, undo=False, reason=None, now=None):
    """Queues the rename. Returns the job id. Raises AsideError with a plain reason when it is not allowed."""
    rec = store.get_record(conn, record_id)
    if rec is None:
        raise AsideError("That file is not in the Library.")
    if _busy(conn, record_id):
        raise AsideError("A worker is already doing something with this file.")
    root, rel = rec["root_name"], v.clean_relative(rec["relative_path"])
    if undo:
        if rec.get("dup_review") != "aside":
            raise AsideError("This file is not hidden.")
        why = rec.get("hidden_reason") or "duplicate"
        source, dest, title = (root, rel + suffix_for(why)), (root, rel), f"Put back: {rec['file_name']}"
    else:
        why = reason or ("duplicate" if rec.get("duplicate_group") and rec.get("duplicate_status") == v.DUP_OTHER else None)
        if why not in REASONS:
            raise AsideError("Say why the file is hidden: exact duplicate, old version or other.")
        if rec.get("missing_since"):
            raise AsideError("This file was not found at its place when the share was last scanned.")
        if rec.get("dup_review") == "aside":
            raise AsideError("This file is already hidden.")
        if why == "duplicate" and (not rec.get("duplicate_group") or rec.get("duplicate_status") != v.DUP_OTHER):
            raise AsideError("Only an exact duplicate that is not the copy to keep can be set aside as a duplicate.")
        if why != "duplicate":
            if not rec.get("media_id"):
                raise AsideError("This file is not linked to a film yet, so SyncHub cannot tell which file replaces it. Press Refresh links first.")
            if rec.get("is_main"):
                raise AsideError("This is the main file you chose. Choose another main file first.")
            from synchub import quality
            main = quality.copies(conn, rec["media_id"]).get("main")
            if main and main["id"] == record_id:
                raise AsideError("This is the film's main file. Make another file the main file first.")
        if _visible_others(conn, rec) < 1:
            raise AsideError("This is the last visible file of the film, so it stays.")
        source, dest = (root, rel), (root, rel + suffix_for(why))
        title = f"Hide {REASON_TEXT[why]}: {rec['file_name']}"
    plan = {"op": "rename", "aside": True, "needs": NEEDS, "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "undo": undo, "reason": why}
    job_id = jobs.create(conn, "library", title=title, ref=(REF, record_id), source=source, dest=dest, plan=plan, need_bytes=0, now=now)
    if not undo and why == "old_version":
        store._update(conn, record_id, {"is_old_version": 1})
        if rec.get("media_id"):
            file_labels.refresh(conn, [rec["media_id"]])
    store.event(conn, record_id, "system", "aside_undo_queued" if undo else "aside_queued", None, {"job": job_id, "reason": why})
    return job_id


def sync(conn, job_id, now=None):
    """Makes the record follow its job. Returns True when Jellyfin should be asked to scan."""
    from synchub import db
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    rec = store.get_record(conn, job["ref_id"])
    if rec is None:
        return False
    plan = job["plan"] or {}
    undo, why = bool(plan.get("undo")), plan.get("reason") or "duplicate"
    if job["status"] != "done":
        if job["status"] in ("failed", "cancelled", "needs_review"):
            store.event(conn, rec["id"], "worker", f"aside_{job['status']}", None, {"job": job_id, "why": (job["error"] or job["review_reason"] or job["message"] or "")[:300]})
            if not undo and why == "old_version" and rec.get("dup_review") != "aside" and job["status"] != "needs_review":
                store._update(conn, rec["id"], {"is_old_version": 0})                  # it was not hidden after all: back to what it was
                if rec.get("media_id"):
                    file_labels.refresh(conn, [rec["media_id"]])
        return False
    if undo:
        store._update(conn, rec["id"], {"dup_review": None, "hidden_reason": None, "hidden_at": None, "hidden_notice_at": None, "is_old_version": 0})
    else:
        store._update(conn, rec["id"], {"dup_review": "aside", "hidden_reason": why, "hidden_at": now or db.now(), "hidden_notice_at": None})
    store.event(conn, rec["id"], "worker", "aside_undone" if undo else "aside_done", None, {"job": job_id, "reason": why})
    if rec.get("media_id"):
        file_labels.refresh(conn, [rec["media_id"]])
    return True
