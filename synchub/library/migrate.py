"""Processing: an approved Library file becomes a job, a worker converts it and brings it into the destination share, and Jellyfin is told.

Approving a file queues one `library` job (source share + path, destination share + path, and a plan). A worker claims it when it is free
(see jobs.claim), does the work, and the record's `migration_*` columns follow the job: `completed` is set only once the converted file has
been copied into the destination share and verified. Nothing here touches a media file; the worker does that.
"""
import copy
import dataclasses
import json
import logging
import os

from synchub import db, jobs, settings
from synchub.library import audio, layout, profiles, size_policy, store
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


def apply_subtitle_policy(change, policy):
    """The change with the subtitle policy applied. `keep_all` puts back every subtitle the analysis would have removed, except those MKV cannot hold."""
    change = copy.deepcopy(change)
    if policy != "keep_all":
        return change
    saved = [t for t in change.get("remove_subtitle") or [] if not t.get("reason")]
    change["keep_subtitle"] = sorted(set(change.get("keep_subtitle") or []) | {t["index"] for t in saved})
    change["remove_subtitle"] = [t for t in change.get("remove_subtitle") or [] if t.get("reason")]
    return change


def approved_removals(rec, profile=None):
    """{track index: True when MKV cannot hold it} for the tracks an approved record's job removes: the approved track changes with the chosen profile's
    subtitle policy applied. A rename, or a profile that only moves the file, removes nothing."""
    if (rec.get("approved_mode") == "rename_only") or (profile and profile["move_only"]):
        return {}
    changes = [p for p in _json_list(rec.get("approved_track_changes_json")) if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")]
    if not changes:
        return {}
    change = apply_subtitle_policy(changes[0], profile["subtitles"] if profile else "remove_non_english")
    return {t["index"]: bool(t.get("reason")) for t in (change.get("remove_audio") or []) + (change.get("remove_subtitle") or [])}


def _changes_nothing(change):
    return (change.get("op") != "convert" and not change.get("remove_audio") and not change.get("remove_subtitle") and change.get("default_audio") is None
            and not change.get("audio_add") and not change.get("audio_convert"))


def _is_video(codec, wanted):
    codec = (codec or "").lower()
    return codec.startswith("hevc") or codec.startswith("h265") if wanted == "hevc" else codec.startswith("h264") or codec.startswith("avc")


def plan_for(rec, cfg, profile=None, mode=None, size=None, rules=None):
    """(destination root, destination relative path, plan) for an approved record. Raises layout.LayoutError.

    `mode` 'rename_only' renames the file inside its own share and touches nothing else. Otherwise the profile decides: a move-only profile copies
    the file exactly; the others apply their subtitle policy to the approved track changes and may re-encode the video."""
    dest_rel = layout.relative_display(rec, cfg)
    source_ext = os.path.splitext(rec["relative_path"])[1]
    mode = mode if mode is not None else rec.get("approved_mode")
    plan = {"revision": rec["source_revision"], "action": rec["approved_action"], "duration": rec.get("duration_seconds")}
    if mode == "rename_only":
        dest_rel = os.path.splitext(dest_rel)[0] + source_ext
        plan.update(op="rename", source_size=rec.get("size_bytes"), source_mtime=rec.get("mtime"))
        return rec["root_name"], dest_rel, plan
    if profile and profile["move_only"]:
        plan["op"] = "copy"
        return cfg.destination_root, os.path.splitext(dest_rel)[0] + source_ext, plan
    changes = [p for p in _json_list(rec.get("approved_track_changes_json")) if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")]
    change = apply_subtitle_policy(changes[0], profile["subtitles"] if profile else "remove_non_english") if changes else None
    wants = profile["video_codec"] if profile else "keep"
    if wants != "keep" and not _is_video(rec.get("video_codec"), wants):
        change = change or {}
        change.update(op="convert", container="matroska", video={"action": "transcode", "from": (rec.get("video_codec") or "").lower(), "codec": wants})
        change.setdefault("audio_convert", {})
        if not dest_rel.lower().endswith(".mkv"):
            change["target_extension"] = ".mkv"
    audio_note = None
    if rules:                                                         # the profile's audio rule (add a compatible track, or convert the main one)
        change, audio_note = audio.apply(rec, change, rules.get("audio_mode"))
    if change is not None and _changes_nothing(change):
        change = None
    decision = size_policy.decide(rec, [change] if change else [], size) if size is not None else None
    compress = decision is not None and decision.action == size_policy.COMPRESS
    if decision is not None and decision.action in (size_policy.REVIEW, size_policy.BLOCKED):
        plan["size_policy_skipped"] = decision.summary               # not compressed; the plan says why
    if change is None and rec["approved_action"] == v.ACTION_COPY_RENAME and not compress:
        plan["op"] = "copy"
        return cfg.destination_root, dest_rel, plan
    change = change or {}
    plan.update(op="convert", change=change)
    extension = change.get("target_extension")
    if extension and not dest_rel.lower().endswith(extension.lower()):
        dest_rel = os.path.splitext(dest_rel)[0] + extension
    if profile and (profile["video_codec"] != "keep" or profile["audio_kbps"] != 192):
        plan.update(profile=profiles.for_plan(profile), needs=[profiles.FEATURE])
    if change.get("audio_add") or any(isinstance(c, dict) and c.get("kbps") for c in (change.get("audio_convert") or {}).values()):
        plan["needs"] = sorted(set(plan.get("needs") or []) | {"audio_add"})
    if audio_note:
        plan["audio_note"] = audio_note
    if compress:
        plan, dest_rel = size_policy.apply(plan, decision, dest_rel)
    return cfg.destination_root, dest_rel, plan


def active_job(conn, record_id):
    r = conn.execute("SELECT id FROM job WHERE ref_type = ? AND ref_id = ? AND status IN ('queued', 'running', 'needs_review') ORDER BY id DESC LIMIT 1", (REF, record_id)).fetchone()
    return r["id"] if r else None


def profile_for(conn, rec):
    """The profile an approved record is converted with: the one chosen for the file, else the one chosen at its scan, else the default."""
    return profiles.effective(conn, rec)


TITLES = {"copy": "Copy", "convert": "Convert", "rename": "Rename"}


def queue(conn, record_id, cfg, by="someone", force=False):
    """Queues the processing job for an approved record. Returns the job id, or None when there is nothing to queue (not approved, already
    migrated at this revision, cancelled and not yet approved or queued again (`force`), or a job is already waiting)."""
    rec = store.get_record(conn, record_id)
    if rec is None or rec["approval_status"] != v.APPROVAL_APPROVED:
        return None
    if rec["migration_status"] == v.MIGRATION_COMPLETED and rec["migrated_source_revision"] == rec["source_revision"]:
        return None
    if rec.get("migration_cancelled_at") and not force:
        return None
    if active_job(conn, record_id) is not None:
        return None
    from synchub import downloads
    if downloads.busy(conn, rec):                                # the download pipeline is replacing this very file in place
        return None
    try:
        profile = profile_for(conn, rec)
        rules = profiles.rules(conn, profile)
        dest_root, dest_rel, plan = plan_for(rec, dataclasses.replace(cfg, clean_names=bool(rules["clean_names"])), profile, size=size_policy.load(conn, profile), rules=rules)
    except layout.LayoutError as exc:
        raise ValueError(f"cannot plan the destination: {exc}")
    need = 0 if plan["op"] == "rename" else (rec["size_bytes"] or 0) * (2 if plan["op"] == "convert" else 1) + WORK_MARGIN_BYTES
    job_id = jobs.create(conn, "library", title=f"{TITLES[plan['op']]}: {v.final_name(rec)}", ref=(REF, record_id),
                         source=(rec["root_name"], rec["relative_path"]), dest=(dest_root, dest_rel), plan=plan, need_bytes=need)
    if rec.get("migration_cancelled_at"):
        store._update(conn, record_id, {"migration_cancelled_at": None})
    store.event(conn, record_id, "user", "migration_queued", None, {"by": by, "job": job_id, "op": plan["op"], "dest": f"{dest_root}:{dest_rel}",
                                                                     "profile": (plan.get("profile") or {}).get("name")})
    return job_id


def requeue(conn, record_id, cfg, by="someone"):
    """Queues a file again that was cancelled or failed. Raises ValueError with a message when it cannot be queued."""
    rec = store.get_record(conn, record_id)
    if rec is None or rec["approval_status"] != v.APPROVAL_APPROVED:
        raise ValueError("Approve the file first.")
    if rec["migration_status"] not in (v.MIGRATION_PENDING, v.MIGRATION_FAILED):
        raise ValueError("This file is already being handled or is finished.")
    job_id = queue(conn, record_id, cfg, by, force=True)
    if job_id is None:
        raise ValueError("A job for this file is already waiting.")
    return job_id


def queue_approved(conn, cfg, by="someone"):
    """Queues every approved file that has no job yet (approved before approvals queued work). A cancelled file is left alone until someone queues it
    again, and so is a failed one: both show on the page with their own Queue again button. Returns (queued, [(id, why)])."""
    queued, skipped = 0, []
    rows = conn.execute("SELECT id FROM library_file WHERE approval_status = 'approved' AND migration_status = 'pending' AND migration_cancelled_at IS NULL AND missing_since IS NULL ORDER BY id").fetchall()
    for r in rows:
        try:
            if queue(conn, r["id"], cfg, by) is not None:
                queued += 1
        except ValueError as exc:
            skipped.append((r["id"], str(exc)))
    return queued, skipped


def redo(conn, record_id, by="someone", dest_removed=False):
    """Takes a decided file back to pending so it can be approved again with other settings (another profile, other tracks). The file itself is not
    touched. Needs the source: once it is deleted, only the converted copy is left. A finished copy stays where it is and the worker never overwrites it,
    so the person must say they removed it (`dest_removed`). Raises ValueError with a message for the page."""
    rec = store.get_record(conn, record_id)
    if rec is None:
        raise ValueError("There is no such file.")
    if rec["migration_status"] == v.MIGRATION_IN_PROGRESS:
        raise v.StateError("A worker is converting this file. Cancel the job on the Workers page first.")
    if rec["source_delete_status"] == "deleted":
        raise ValueError("The source was deleted after the copy was verified, so there is nothing left to convert again.")
    if rec.get("missing_since"):
        raise ValueError("The source file is not where it was at the last scan. Scan again first.")
    if rec["migration_status"] == v.MIGRATION_COMPLETED and not dest_removed:
        raise ValueError(f"The converted copy is already at {rec.get('migration_destination_path') or 'its destination'}. A worker never overwrites a file, so remove that copy first, then confirm.")
    store.begin(conn)
    with store.savepoint(conn, "redo"):
        cancel_active(conn, record_id, by)
        # a new revision, so the outcome of the old job can never make this record look finished again
        store._update(conn, record_id, {**store._RESET_APPROVAL, **store._RESET_MIGRATION, "source_revision": rec["source_revision"] + 1})
        store.event(conn, record_id, "user", "redo_requested", None, {"by": by, "was": rec["migration_status"], "profile": rec.get("approved_profile_id")})


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
    cancelled = job["status"] == "cancelled"
    if rec["migration_status"] == target and bool(rec.get("migration_cancelled_at")) == cancelled:
        return False
    values = {"migration_status": target, "migration_cancelled_at": now if cancelled else None}
    if target == v.MIGRATION_IN_PROGRESS:
        values.update(migration_started_at=job["started_at"] or now, migration_finished_at=None, migration_last_error=None)
    elif target == v.MIGRATION_COMPLETED:
        result = job["result"] if isinstance(job["result"], dict) else {}
        verified = result.get("verified")
        values.update(migration_finished_at=now, migration_last_error=None, migration_destination_path=f"{job['dest_root']}:{job['dest_rel']}",
                      migrated_dest_size=result.get("bytes"), migrated_dest_sha256=result.get("sha256"), migrated_how=job["plan"].get("op"),
                      migration_verification_status="verified" if verified in ("sha256", "size") else "not_verified", migration_verification_detail=json.dumps(result)[:2000],
                      migration_verified_at=now, migrated_source_revision=rec["source_revision"])
    elif target in (v.MIGRATION_FAILED, v.MIGRATION_MANUAL):
        values.update(migration_finished_at=now, migration_last_error=(job["error"] or job["review_reason"] or job["message"] or "")[:500])
    else:
        values.update(migration_last_error="Cancelled. It stays cancelled until you queue it again." if cancelled else None)
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
    if target == v.MIGRATION_COMPLETED:
        from synchub.library import cleanup
        cleanup.after_migration(conn, rec["id"], job, now)
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
    cancelled = conn.execute("SELECT COUNT(*) FROM library_file WHERE approval_status = 'approved' AND migration_cancelled_at IS NOT NULL AND migration_status = 'pending'").fetchone()[0]
    unqueued = conn.execute(
        """SELECT COUNT(*) FROM library_file f WHERE f.approval_status = 'approved' AND f.migration_status = 'pending' AND f.migration_cancelled_at IS NULL AND f.missing_since IS NULL
           AND NOT EXISTS (SELECT 1 FROM job j WHERE j.ref_type = 'library_file' AND j.ref_id = f.id AND j.status IN ('queued', 'running', 'needs_review'))""").fetchone()[0]
    return {"queued": counts.get("queued", 0), "running": counts.get("running", 0), "waiting": counts.get("needs_review", 0),
            "failed": latest.get("failed", 0), "completed": conn.execute("SELECT COUNT(*) FROM library_file WHERE migration_status = 'completed'").fetchone()[0],
            "unqueued": unqueued, "cancelled": cancelled}


def attention(conn, limit=20):
    """Approved files that stopped without finishing: cancelled (they stay cancelled until queued again) or failed."""
    return [dict(r) for r in conn.execute(
        "SELECT id, file_name, root_name, migration_status, migration_cancelled_at, migration_last_error FROM library_file WHERE approval_status = 'approved' "
        "AND missing_since IS NULL AND ((migration_status = 'pending' AND migration_cancelled_at IS NOT NULL) OR migration_status = 'failed') ORDER BY id LIMIT ?", (limit,))]


def _op(plan):
    try:
        return (json.loads(plan) if isinstance(plan, str) else plan or {}).get("op")
    except ValueError:
        return None


def jellyfin_updates(conn, job_id):
    """[{"Path", "UpdateType"}] for the files a finished job created or moved, as Jellyfin sees the paths; None when a share has no Jellyfin path set (the caller then asks for a whole refresh)."""
    r = conn.execute("SELECT kind, source_root, source_rel, dest_root, dest_rel, plan FROM job WHERE id = ?", (job_id,)).fetchone()
    if r is None or not r["dest_root"] or not r["dest_rel"]:
        return None

    from synchub import storage

    def seen(root_name, rel):
        root = storage.by_name(conn, root_name)
        base = (root or {}).get("jellyfin_path")
        return f"{base.rstrip('/')}/{rel}" if base and rel else None

    created = seen(r["dest_root"], r["dest_rel"])
    if created is None:
        return None
    updates = [{"Path": created, "UpdateType": "Created"}]
    if r["source_root"] == r["dest_root"] and r["source_rel"] and r["source_rel"] != r["dest_rel"] and _op(r["plan"]) == "rename":
        gone = seen(r["source_root"], r["source_rel"])
        if gone:
            updates.append({"Path": gone, "UpdateType": "Deleted"})
    return updates


def tell_jellyfin(count=1, updates=None):
    """Asks Jellyfin to look at the files that just arrived in the destination share: just those paths when `updates` is given (see jellyfin_updates), else
    (or if that fails) a scan of the whole library. Best effort: the files are in place either way, and Jellyfin finds them on its own scheduled scan.
    Never raises. Call it with no database transaction open."""
    if settings.get("disable_jellyfin_connection") == "1":
        return False
    try:
        from synchub.clients.jellyfin import JellyfinClient
        client = JellyfinClient()
        if updates:
            try:
                client.media_updated(updates)
                logger.info(f"[library] told Jellyfin about {len(updates)} changed path(s)")
                return True
            except Exception as exc:
                logger.warning(f"[library] Jellyfin would not take the path update ({exc}); asking for a library scan instead")
        client.refresh_library()
    except Exception as exc:
        logger.warning(f"[library] could not ask Jellyfin to scan the {count} new file(s): {exc}")
        return False
    logger.info(f"[library] asked Jellyfin to scan {count} newly migrated file(s)")
    return True
