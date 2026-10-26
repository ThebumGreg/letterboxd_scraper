"""Shows a file's journey through a worker on the Dashboard: Transferring to Worker, Working, Converting, Transferring to Library, then In Library.

While a worker handles a Library or download job, one open `processing_event` (provider `synchub_job`) says which stage the title is in. The state
engine lets it win over "In Library", so a title that is being converted shows its stage, then returns to In Library when the job ends. A title
that is only being renamed or whose source is being deleted shows nothing: nothing about it is in flight.
"""
import json
import logging
import re

from synchub import db, jobs, states
from synchub.library import store

logger = logging.getLogger(__name__)

PROVIDER = "synchub_job"
REFS = ("library_file", "download_import")
STATE_OF = {"assigned": "transferring", "transferring": "transferring", "working": "working", "converting": "encoding", "transferring_library": "transferring_library"}
EPISODE = re.compile(r"[sS](\d{1,3})[eE](\d{1,4})")
MOVIE = re.compile(r"^(.*?)\s*\((\d{4})\)")
WAS_IN_LIBRARY = "was in library"


def target_for(conn, rec):
    """(target_type, id) of the movie or episode a Library record is, or None when it cannot be matched to a title SyncHub knows."""
    name = rec.get("recommended_name") or rec.get("file_name") or ""
    if rec.get("media_type") == "TV Episode":
        match = EPISODE.search(name)
        if not match or not rec.get("show_title"):
            return None
        row = conn.execute("SELECT e.id FROM episode e JOIN media m ON m.id = e.media_id WHERE m.kind = 'show' AND lower(m.title) = lower(?) AND e.season_number = ? AND e.episode_number = ?",
                           (rec["show_title"], int(match.group(1)), int(match.group(2)))).fetchone()
        return ("episode", row[0]) if row else None
    match = MOVIE.match(name)
    if not match:
        return None
    row = conn.execute("SELECT id FROM media WHERE kind = 'movie' AND lower(title) = lower(?) AND (year IS NULL OR year = ?) ORDER BY year IS NULL LIMIT 1",
                       (match.group(1), int(match.group(2)))).fetchone()
    return ("movie", row[0]) if row else None


def _record_of(conn, job):
    if job["ref_type"] == "library_file":
        return store.get_record(conn, job["ref_id"])
    row = conn.execute("SELECT record_id FROM download_import WHERE id = ?", (job["ref_id"],)).fetchone()
    return store.get_record(conn, row[0]) if row and row[0] else None


def update_for_job(conn, job_id, now=None, touched=None):
    """Brings the Dashboard stage of the job's title in line with the job. Cheap when nothing changed. Returns the set of (type, id) whose state changed."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] not in REFS or job["kind"] not in ("library", "download"):
        return set()
    now = now or db.now()
    event = conn.execute("SELECT * FROM processing_event WHERE provider = ? AND external_ref = ? AND ended_at IS NULL", (PROVIDER, str(job_id))).fetchone()
    state = None
    if job["status"] == "running":
        code, _ = jobs.lifecycle(job)
        state = STATE_OF.get(code)
        if job["plan"].get("op") == "rename":
            state = "working" if state else None
    progress = job.get("progress")
    touched = touched if touched is not None else set()
    if state is None:
        if event:
            conn.execute("UPDATE processing_event SET ended_at = ?, updated_at = ? WHERE id = ?", (now, now, event["id"]))
            touched.add((event["target_type"], event["target_id"]))
    else:
        if event and event["state_code"] == state and (event["progress"] or 0) == (progress or 0):
            return set()
        if event:
            conn.execute("UPDATE processing_event SET state_code = ?, progress = ?, updated_at = ? WHERE id = ?", (state, progress, now, event["id"]))
            touched.add((event["target_type"], event["target_id"]))
        else:
            rec = _record_of(conn, job)
            target = target_for(conn, rec) if rec else None
            if target is None:
                return set()
            was = conn.execute("SELECT state_code FROM item_state WHERE target_type = ? AND target_id = ?", target).fetchone()
            conn.execute("INSERT INTO processing_event (target_type, target_id, provider, state_code, progress, detail, external_ref, started_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (*target, PROVIDER, state, progress, WAS_IN_LIBRARY if was and was["state_code"] == "in_library" else job["title"], str(job_id), now, now))
            touched.add(target)
    changed = set()
    if touched:
        states.compute(conn, now=now, only=touched, changed=changed)
    return changed


def close_stale(conn, now=None):
    """Closes the stage of every job that is no longer running (a worker that went silent, a job cancelled or re-queued). Returns the targets touched."""
    now, touched = now or db.now(), set()
    for e in conn.execute("SELECT e.* FROM processing_event e LEFT JOIN job j ON j.id = CAST(e.external_ref AS INTEGER) "
                          "WHERE e.provider = ? AND e.ended_at IS NULL AND (j.id IS NULL OR j.status != 'running')", (PROVIDER,)).fetchall():
        conn.execute("UPDATE processing_event SET ended_at = ?, updated_at = ? WHERE id = ?", (now, now, e["id"]))
        touched.add((e["target_type"], e["target_id"]))
    changed = set()
    if touched:
        states.compute(conn, now=now, only=touched, changed=changed)
    return touched | changed


def refreshed_recently(conn, target, hours=6):
    """Whether a job on this title that started out In Library ended lately: it going back to In Library is not an arrival."""
    return conn.execute("SELECT 1 FROM processing_event WHERE provider = ? AND target_type = ? AND target_id = ? AND detail = ? "
                        "AND COALESCE(ended_at, updated_at) >= datetime('now', ?) LIMIT 1", (PROVIDER, target[0], target[1], WAS_IN_LIBRARY, f"-{int(hours)} hours")).fetchone() is not None
