"""Processing: an approved Library file becomes a job, a worker converts it and brings it into the destination share, and Jellyfin is told.

Approving a file queues one `library` job (source share + path, destination share + path, and a plan). A worker claims it when it is free
(see jobs.claim), does the work, and the record's `migration_*` columns follow the job: `completed` is set only once the converted file has
been copied into the destination share and verified. Nothing here touches a media file; the worker does that.
"""
import json
import logging
import os

from synchub import db, jobs, settings
from synchub.library import layout, store
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

REF = "library_file"
ACTIVE = ("queued", "running", "needs_review")
# What the record says for each job status (a cancelled or skipped job leaves the file as it was, so it is pending again).
RECORD_STATUS = {"queued": v.MIGRATION_PENDING, "running": v.MIGRATION_IN_PROGRESS, "needs_review": v.MIGRATION_MANUAL, "done": v.MIGRATION_COMPLETED,
                 "failed": v.MIGRATION_FAILED, "cancelled": v.MIGRATION_PENDING, "skipped": v.MIGRATION_PENDING}
WORK_MARGIN_BYTES = 2 * 1024 ** 3        # room a worker needs beyond the file itself (the converted copy sits next to the source copy)


def _json_list(text):
    try:
        value = json.loads(text) if text else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def plan_for(rec, cfg):
    """(destination root, destination relative path, plan) for an approved record. Raises layout.LayoutError."""
    dest_rel = layout.relative_display(rec, cfg)
    changes = [p for p in _json_list(rec.get("approved_track_changes_json")) if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")]
    plan = {"revision": rec["source_revision"], "action": rec["approved_action"], "duration": rec.get("duration_seconds")}
    if rec["approved_action"] == v.ACTION_COPY_RENAME and not changes:
        plan["op"] = "copy"
    else:
        change = changes[0] if changes else {}
        plan.update(op="convert", change=change)
        extension = change.get("target_extension")
        if extension and not dest_rel.lower().endswith(extension.lower()):
            dest_rel = os.path.splitext(dest_rel)[0] + extension
    return cfg.destination_root, dest_rel, plan


def active_job(conn, record_id):
    r = conn.execute("SELECT id FROM job WHERE ref_type = ? AND ref_id = ? AND status IN ('queued', 'running', 'needs_review') ORDER BY id DESC LIMIT 1", (REF, record_id)).fetchone()
    return r["id"] if r else None


def queue(conn, record_id, cfg, by="someone"):
    """Queues the processing job for an approved record. Returns the job id, or None when there is nothing to queue (not approved, already
    migrated at this revision, or a job is already waiting)."""
    rec = store.get_record(conn, record_id)
    if rec is None or rec["approval_status"] != v.APPROVAL_APPROVED:
        return None
    if rec["migration_status"] == v.MIGRATION_COMPLETED and rec["migrated_source_revision"] == rec["source_revision"]:
        return None
    if active_job(conn, record_id) is not None:
        return None
    from synchub import downloads
    if downloads.busy(conn, rec):                                # the download pipeline is replacing this very file in place
        return None
    try:
        dest_root, dest_rel, plan = plan_for(rec, cfg)
    except layout.LayoutError as exc:
        raise ValueError(f"cannot plan the destination: {exc}")
    job_id = jobs.create(conn, "library", title=f"{'Convert' if plan['op'] == 'convert' else 'Copy'}: {v.final_name(rec)}", ref=(REF, record_id),
                         source=(rec["root_name"], rec["relative_path"]), dest=(dest_root, dest_rel), plan=plan,
                         need_bytes=(rec["size_bytes"] or 0) * (2 if plan["op"] == "convert" else 1) + WORK_MARGIN_BYTES)
    store.event(conn, record_id, "user", "migration_queued", None, {"by": by, "job": job_id, "op": plan["op"], "dest": f"{dest_root}:{dest_rel}"})
    return job_id


def queue_approved(conn, cfg, by="someone"):
    """Queues every approved file that has no job yet (for files approved before approvals queued work). Returns (queued, [(id, why)])."""
    queued, skipped = 0, []
    rows = conn.execute("SELECT id FROM library_file WHERE approval_status = 'approved' AND migration_status IN ('pending', 'failed') AND missing_since IS NULL ORDER BY id").fetchall()
    for r in rows:
        try:
            if queue(conn, r["id"], cfg, by) is not None:
                queued += 1
        except ValueError as exc:
            skipped.append((r["id"], str(exc)))
    return queued, skipped


def cancel_active(conn, record_id, by="someone"):
    """Stops the waiting job of a record whose approval is being changed. A job a worker is already running cannot be taken back."""
    for r in conn.execute("SELECT id, status FROM job WHERE ref_type = ? AND ref_id = ? AND status IN ('queued', 'running', 'needs_review')", (REF, record_id)).fetchall():
        if r["status"] == "running":
            raise v.StateError("cannot change approval while a worker is converting this file; cancel the job on the Workers page first")
        jobs.cancel(conn, r["id"], by)


def cancel_waiting(conn, record_id):
    """A scan found the file changed: its waiting job is for the old file. (A running one stops by itself; its outcome is ignored, see sync.)"""
    for r in conn.execute("SELECT id FROM job WHERE ref_type = ? AND ref_id = ? AND status IN ('queued', 'needs_review')", (REF, record_id)).fetchall():
        jobs.cancel(conn, r["id"], "scan")


def sync(conn, job_id, now=None):
    """Makes the record follow its job. Returns True when this call is the one that completed it (Jellyfin should be told)."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    rec = store.get_record(conn, job["ref_id"])
    if rec is None or job["plan"].get("revision") != rec["source_revision"]:
        return False                                            # the file changed since: this job's outcome says nothing about it now
    target, now = RECORD_STATUS.get(job["status"], v.MIGRATION_PENDING), now or db.now()
    if rec["migration_status"] == target:
        return False
    values = {"migration_status": target}
    if target == v.MIGRATION_IN_PROGRESS:
        values.update(migration_started_at=job["started_at"] or now, migration_finished_at=None, migration_last_error=None)
    elif target == v.MIGRATION_COMPLETED:
        result = job["result"] if isinstance(job["result"], dict) else {}
        verified = result.get("verified")
        values.update(migration_finished_at=now, migration_last_error=None, migration_destination_path=f"{job['dest_root']}:{job['dest_rel']}",
                      migration_verification_status="verified" if verified in ("sha256", "size") else "not_verified", migration_verification_detail=json.dumps(result)[:2000],
                      migration_verified_at=now, migrated_source_revision=rec["source_revision"])
    elif target in (v.MIGRATION_FAILED, v.MIGRATION_MANUAL):
        values.update(migration_finished_at=now, migration_last_error=(job["error"] or job["review_reason"] or job["message"] or "")[:500])
    else:
        values.update(migration_last_error="Cancelled." if job["status"] == "cancelled" else None)
    store._update(conn, rec["id"], values)
    store.event(conn, rec["id"], "worker", f"migration_{target}", None, {"job": job_id, "status": job["status"], "error": job["error"]}, now)
    if target in (v.MIGRATION_COMPLETED, v.MIGRATION_FAILED):
        count = (conn.execute("SELECT COALESCE(MAX(attempt_number), 0) FROM library_attempt WHERE media_file_id = ?", (rec["id"],)).fetchone()[0] or 0) + 1
        conn.execute("UPDATE library_file SET migration_attempt_count = ? WHERE id = ?", (count, rec["id"]))
        last = conn.execute("SELECT worker_id, command, error FROM job_attempt WHERE job_id = ? ORDER BY attempt_no DESC LIMIT 1", (job_id,)).fetchone()
        worker = conn.execute("SELECT name FROM worker WHERE id = ?", (last["worker_id"],)).fetchone() if last and last["worker_id"] else None
        conn.execute("INSERT INTO library_attempt (media_file_id, attempt_number, source_revision, action, status, job_id, worker, started_at, finished_at, destination_path, error, command, "
                     "verification_status, verification_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (rec["id"], count, rec["source_revision"], rec["approved_action"] or "", "completed" if target == v.MIGRATION_COMPLETED else "failed", job_id,
                      worker["name"] if worker else None, job["started_at"] or now, now, values.get("migration_destination_path"), values.get("migration_last_error"),
                      (last["command"] if last else None) or (job["result"].get("command") if isinstance(job["result"], dict) else None),
                      values.get("migration_verification_status"), values.get("migration_verification_detail")))
    return target == v.MIGRATION_COMPLETED


def sync_active(conn):
    """Brings every record in step with its newest job (a job re-queued after its worker went silent, a retry from the Workers page...).
    Returns the number of records completed by this call."""
    rows = conn.execute(
        """SELECT j.id FROM job j JOIN library_file f ON f.id = j.ref_id
           WHERE j.ref_type = 'library_file' AND j.id = (SELECT MAX(id) FROM job WHERE ref_type = 'library_file' AND ref_id = f.id)
             AND json_extract(j.plan, '$.revision') = f.source_revision
             AND f.migration_status != CASE j.status WHEN 'running' THEN 'in_progress' WHEN 'done' THEN 'completed' WHEN 'failed' THEN 'failed'
                                                     WHEN 'needs_review' THEN 'manual_review' ELSE 'pending' END""").fetchall()
    return sum(1 for r in rows if sync(conn, r["id"]))


def queue_summary(conn):
    """What the Library page shows: files waiting for a worker, being converted, held for a decision, failed, and approved but not queued."""
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT j.status, COUNT(*) AS n FROM job j WHERE j.ref_type = ? GROUP BY j.status", (REF,))}
    # Only a record's newest job counts as its state, so an old failed attempt that was retried is not listed twice.
    latest = {r["status"]: r["n"] for r in conn.execute(
        "SELECT j.status, COUNT(*) AS n FROM job j WHERE j.ref_type = ? AND j.id = (SELECT MAX(id) FROM job WHERE ref_type = ? AND ref_id = j.ref_id) GROUP BY j.status", (REF, REF))}
    unqueued = conn.execute(
        """SELECT COUNT(*) FROM library_file f WHERE f.approval_status = 'approved' AND f.migration_status IN ('pending', 'failed') AND f.missing_since IS NULL
           AND NOT EXISTS (SELECT 1 FROM job j WHERE j.ref_type = 'library_file' AND j.ref_id = f.id AND j.status IN ('queued', 'running', 'needs_review'))""").fetchone()[0]
    return {"queued": counts.get("queued", 0), "running": counts.get("running", 0), "waiting": counts.get("needs_review", 0),
            "failed": latest.get("failed", 0), "completed": conn.execute("SELECT COUNT(*) FROM library_file WHERE migration_status = 'completed'").fetchone()[0],
            "unqueued": unqueued}


def tell_jellyfin(count=1):
    """Asks Jellyfin to look for the files that just arrived in the destination share. Best effort: the files are in place either way, and
    Jellyfin finds them on its own scheduled scan. Never raises. Call it with no database transaction open."""
    if settings.get("disable_jellyfin_connection") == "1":
        return False
    try:
        from synchub.clients.jellyfin import JellyfinClient
        JellyfinClient().refresh_library()
    except Exception as exc:
        logger.warning(f"[library] could not ask Jellyfin to scan the {count} new file(s): {exc}")
        return False
    logger.info(f"[library] asked Jellyfin to scan {count} newly migrated file(s)")
    return True
