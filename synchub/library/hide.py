"""Hiding the original of a film from Jellyfin once its copy or conversion in another share is verified.

Jellyfin skips a folder that holds an empty `.ignore` file. So after a verified copy to another share, a `hide_source` job goes to a worker, which checks
the library copy is still whole and writes `.ignore` in the original's folder, but only when that folder belongs to the film (it holds no other video
file). Nothing is deleted: the original stays on disk until it is deleted (see cleanup.py) or the person deletes it. `unhide_source` undoes it.
SyncHub then asks Jellyfin to scan, so the original drops out of Jellyfin and out of the Dashboard's extra-version count.
"""
import logging

from synchub import db, jobs, settings
from synchub.library import store
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

REF = "library_hide"
IGNORE = ".ignore"


def enabled(conn=None):
    return settings.get("hide_source", conn=conn) == "1"


def windows_path(root, rel=None):
    """The path to paste into Windows Explorer for a share (and a folder inside it), built from the share's SMB address and name. None for NFS or local shares."""
    if not root or root.get("protocol") != "smb" or not root.get("host") or not root.get("share"):
        return None
    base = "\\\\" + root["host"] + "\\" + str(root["share"]).strip("/\\").replace("/", "\\")
    rel = str(rel or "").strip("/")
    return base + ("\\" + rel.replace("/", "\\") if rel else "")


def can_hide(rec, job):
    """Whether a finished job leaves an original that can be hidden: a movie copied or converted to another share, in a folder of its own (Movies/Title/file.mkv)."""
    if (job["plan"] or {}).get("op") not in ("copy", "convert") or (job["plan"] or {}).get("replace"):
        return False
    if job["source_root"] == job["dest_root"]:
        return False
    if not str(rec.get("media_type") or "").startswith("Movie"):
        return False
    return len(v.clean_relative(job["source_rel"]).split("/")) >= 3


def queue(conn, record_id, job, now=None, undo=False):
    """Queues a hide_source (or unhide_source) job for a record whose original sits at the job's source. Returns the job id."""
    rec = store.get_record(conn, record_id)
    if rec is None:
        return None
    op = "unhide_source" if undo else "hide_source"
    plan = {"op": op, "revision": rec["source_revision"], "dest_size": rec.get("migrated_dest_size"), "source_size": rec["size_bytes"]}
    job_id = jobs.create(conn, "library", title=f"{'Show' if undo else 'Hide'} original: {rec['file_name']}", ref=(REF, record_id), source=(job["source_root"], job["source_rel"]),
                         dest=(job["dest_root"], job["dest_rel"]), plan=plan, need_bytes=0, now=now)
    store._update(conn, record_id, {"source_hide_message": "Queued for a worker."})
    store.event(conn, record_id, "system", f"{op}_queued", None, {"job": job_id})
    return job_id


def after_migration(conn, record_id, job, now=None):
    """Called when a copy or conversion has been verified: queues the hiding when the setting is on and the original can be hidden."""
    rec = store.get_record(conn, record_id)
    if rec is None or not enabled(conn) or not can_hide(rec, job):
        return None
    return queue(conn, record_id, job, now)


def request(conn, record_id, undo=False):
    """The film page's Hide / Show buttons: queues the job from the record's own migration."""
    rec = store.get_record(conn, record_id)
    if rec is None or rec["migration_status"] != v.MIGRATION_COMPLETED or not rec.get("migration_destination_path") or ":" not in rec["migration_destination_path"]:
        raise ValueError("This file has not been copied to another share, so there is no original to hide.")
    dest_root, dest_rel = rec["migration_destination_path"].split(":", 1)
    job = {"source_root": rec["root_name"], "source_rel": rec["relative_path"], "dest_root": dest_root, "dest_rel": dest_rel, "plan": {"op": rec.get("migrated_how") or "copy"}}
    if dest_root == rec["root_name"]:
        raise ValueError("The file was renamed inside its share, so there is no separate original.")
    if len(v.clean_relative(rec["relative_path"]).split("/")) < 3:
        raise ValueError("This file does not have a folder of its own, so it cannot be hidden with a .ignore file.")
    if undo != bool(rec.get("source_hidden_at")):
        raise ValueError("It is already visible to Jellyfin." if undo else "It is already hidden.")
    return queue(conn, record_id, job, undo=undo)


def sync(conn, job_id, now=None):
    """Makes the record follow its hide/unhide job. Returns True when Jellyfin should be asked to scan."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    rec = store.get_record(conn, job["ref_id"])
    if rec is None:
        return False
    result = job["result"] if isinstance(job["result"], dict) else {}
    op = job["plan"].get("op")
    if job["status"] == "done":
        if op == "unhide_source":
            values = {"source_hidden_at": None, "source_hide_message": "Visible to Jellyfin again."}
        elif result.get("hidden") or result.get("already_gone"):
            values = {"source_hidden_at": now or db.now(), "source_hide_message": "Hidden from Jellyfin." if result.get("hidden") else "The original is already gone."}
        else:
            values = {"source_hide_message": (result.get("skipped") or "Not hidden.")[:300]}
    elif job["status"] in ("failed", "cancelled", "needs_review"):
        values = {"source_hide_message": (job["error"] or job["review_reason"] or job["message"] or "Could not hide the original.")[:300]}
    else:
        return False
    store._update(conn, rec["id"], values)
    store.event(conn, rec["id"], "worker", f"{op}_{job['status']}", None, {"job": job_id})
    return job["status"] == "done" and ("source_hidden_at" in values)


def backfill(conn):
    """Queues the hiding for every earlier copy or conversion whose original is still visible. Returns how many jobs were queued."""
    rows = conn.execute("SELECT id FROM library_file WHERE migration_status = ? AND migrated_how IN ('copy', 'convert') AND source_hidden_at IS NULL AND migration_destination_path LIKE '%:%' "
                        "AND missing_since IS NULL ORDER BY id", (v.MIGRATION_COMPLETED,)).fetchall()
    queued = 0
    for r in rows:
        busy = conn.execute("SELECT 1 FROM job WHERE ref_type = ? AND ref_id = ? AND status IN ('queued', 'running', 'needs_review')", (REF, r["id"])).fetchone()
        rec = store.get_record(conn, r["id"])
        if busy or rec is None or not str(rec.get("media_type") or "").startswith("Movie"):
            continue
        try:
            if request(conn, r["id"]) is not None:
                queued += 1
        except ValueError:
            continue
    return queued
