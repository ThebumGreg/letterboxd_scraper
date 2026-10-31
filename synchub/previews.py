"""Preview clips: a short window of a film, compressed with the planned settings next to the untouched original, to look at before a compression goes ahead.

A worker cuts the window, encodes it with exactly the settings the real job would use and makes the matching unmodified clip, then uploads both here
(`previews/<id>/encoded.mp4` and `source.mp4` in SyncHub's data folder). The review screen plays them side by side with the numbers. A person approves,
tries another window or target, skips or rejects. `review_first` says when a compression must wait for that look (see docs/DESIGN_ENCODING.md section 12).
"""
import json
import logging
import os
import posixpath
import shutil
from datetime import datetime, timedelta, timezone

from synchub import db, jobs, settings
from synchub.library import profiles, size_policy, store
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

REF = "preview"
SUBJECTS = ("download_import", "library_file")
MODES = ("random", "worst")
REVIEW_FIRST = ("always", "until_trusted", "low_confidence_only", "never")
MAX_CLIP_BYTES = 700 * 1024 ** 2             # per clip; the worker keeps its clips well under this
CLIP_NAMES = ("encoded", "source")
JOB_STATUS = {"queued": "queued", "running": "making", "done": "ready", "failed": "failed", "needs_review": "failed", "cancelled": "cancelled"}
BAND_GB = 10
GIB = 1024 ** 3


class PreviewError(ValueError):
    """Something the person can fix or should know, in a sentence."""


# ---------------------------------------------------------------- settings
def review_first(conn=None):
    value = settings.get("review_first", conn=conn)
    return value if value in REVIEW_FIRST else "always"


def _int(key, default, low, high, conn=None):
    try:
        return min(high, max(low, int(settings.get(key, conn=conn))))
    except (TypeError, ValueError):
        return default


def trust_after(conn=None):
    return _int("trust_after", 3, 1, 50, conn)


def clip_seconds(conn=None):
    return _int("preview_seconds", 45, 30, 60, conn)


def keep_days(conn=None):
    return _int("preview_keep_days", 7, 1, 365, conn)


def auto_make(conn=None):
    return settings.get("preview_auto", conn=conn) not in ("0", "", "off", "false", None)


# ---------------------------------------------------------------- what kind of file this is
def signature(rec):
    """Kind of file, for 'ask until I have approved K like it': resolution class, codec family, HDR type, bit depth, size band."""
    info = _json(rec.get("video_info_json"))
    try:
        height = int(info.get("height") or str(rec.get("resolution") or "0x0").lower().split("x")[1])
    except (ValueError, IndexError, TypeError):
        height = 0
    res = "4k" if height >= 1800 else "1080p" if height >= 1000 else "720p" if height >= 700 else "sd"
    codec = (rec.get("video_codec") or "unknown").lower()
    codec = {"h265": "hevc"}.get(codec, codec)
    gb = (rec.get("size_bytes") or 0) / GIB
    band = int(gb // BAND_GB) * BAND_GB
    return f"{res}|{codec}|{info.get('hdr') or 'sdr'}|{int(info.get('bit_depth') or 8)}bit|{band}-{band + BAND_GB}GB"


def approved_count(conn, sig):
    r = conn.execute("SELECT approved FROM preview_trust WHERE signature = ?", (sig,)).fetchone()
    return r["approved"] if r else 0


def trusted(conn, rec):
    return approved_count(conn, signature(rec)) >= trust_after(conn)


def low_confidence(size):
    """A compression that is closer to the edge than usual: HDR, interlaced, or within a quarter of the quality floor."""
    profile, numbers = size.profile or {}, size.numbers or {}
    near = numbers.get("bpp") is not None and numbers.get("bpp_floor") and numbers["bpp"] < numbers["bpp_floor"] * 1.25
    return bool(profile.get("hdr") or profile.get("deinterlace") or near or size.action == size_policy.REVIEW)


def must_review_first(conn, rec, size):
    """Whether this compression has to wait for a person to watch a preview, by the `review_first` setting. Returns a sentence, or '' when it may go ahead."""
    rule = review_first(conn)
    if rule == "never" or size.profile is None:
        return ""
    if rule == "always":
        return "Waiting for you to watch a preview before it is compressed."
    if rule == "until_trusted":
        have, need = approved_count(conn, signature(rec)), trust_after(conn)
        return "" if have >= need else f"Waiting for a preview ({have} of {need} similar files approved so far)."
    return "Waiting for a preview: this one is closer to the edge than usual." if low_confidence(size) else ""


# ---------------------------------------------------------------- files
def root_folder():
    return os.path.join(os.path.dirname(os.path.abspath(db.DB_FILE)), "previews")


def folder(preview_id):
    return os.path.join(root_folder(), str(int(preview_id)))


def clip_path(preview_id, name):
    if name not in CLIP_NAMES:
        raise PreviewError("Unknown clip.")
    return os.path.join(folder(preview_id), f"{name}.mp4")


def store_clip(conn, job_id, worker_id, name, stream, length=None):
    """Saves a clip a worker uploads for its running preview job. Written to a part file and renamed, so a half upload is never played."""
    job = jobs.owned(conn, job_id, worker_id)
    if job["ref_type"] != REF:
        raise jobs.JobError("This job does not make a preview.")
    row = get(conn, job["ref_id"])
    if row is None or row["job_id"] != job_id:
        raise jobs.JobError("That preview is gone.")
    if name not in CLIP_NAMES:
        raise jobs.JobError("Unknown clip.")
    if length is not None and length > MAX_CLIP_BYTES:
        raise jobs.JobError(f"The clip is {length // 1024 ** 2} MB; the limit is {MAX_CLIP_BYTES // 1024 ** 2} MB.")
    os.makedirs(folder(row["id"]), exist_ok=True)
    final = clip_path(row["id"], name)
    part = final + ".part"
    written = 0
    try:
        with open(part, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_CLIP_BYTES:
                    raise jobs.JobError(f"The clip is over the {MAX_CLIP_BYTES // 1024 ** 2} MB limit.")
                out.write(chunk)
        if written == 0:
            raise jobs.JobError("The clip was empty.")
        os.replace(part, final)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return written


def remove_files(preview_id):
    shutil.rmtree(folder(preview_id), ignore_errors=True)


# ---------------------------------------------------------------- reading
def _json(text):
    if isinstance(text, dict):
        return text
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def get(conn, preview_id):
    r = conn.execute("SELECT * FROM preview WHERE id = ?", (preview_id,)).fetchone()
    return dict(r) if r else None


def latest_for(conn, subject_type, subject_id):
    r = conn.execute("SELECT * FROM preview WHERE subject_type = ? AND subject_id = ? ORDER BY id DESC LIMIT 1", (subject_type, subject_id)).fetchone()
    return dict(r) if r else None


def live_for(conn, subject_type, subject_id):
    r = conn.execute("SELECT * FROM preview WHERE subject_type = ? AND subject_id = ? AND status IN ('queued', 'making', 'ready') AND decision IS NULL ORDER BY id DESC LIMIT 1",
                     (subject_type, subject_id)).fetchone()
    return dict(r) if r else None


def describe(row):
    """The row with what the page needs: the stats, the numbers worked out from them and whether the clips are there."""
    row = dict(row)
    stats = _json(row.get("stats_json"))
    clip, plan = stats.get("clip") or {}, stats.get("plan") or {}
    row["stats"], row["plan"], row["clip"], row["notes"], row["reasons"] = stats, plan, clip, stats.get("notes") or [], stats.get("reasons") or []
    row["files"] = {name: os.path.isfile(clip_path(row["id"], name)) for name in CLIP_NAMES} if row["status"] == "ready" else {}
    duration = plan.get("duration") or stats.get("duration")
    row["predicted_gb"] = None
    if clip.get("encoded_video_kbps") and duration:
        other = ((plan.get("audio_kbps") or 0) + (plan.get("subtitle_kbps") or 0)) * 1000 / 8 * duration
        row["predicted_gb"] = round((clip["encoded_video_kbps"] * 1000 / 8 * duration + other) / GIB, 1)
    row["ratio"] = round(clip["encoded_video_kbps"] / clip["source_video_kbps"], 2) if clip.get("encoded_video_kbps") and clip.get("source_video_kbps") else None
    row["name"] = posixpath.basename(row["title"] or "") or f"Preview {row['id']}"
    row["when"] = (row["created_at"] or "")[:16]
    return row


def recent(conn, limit=20):
    return [describe(r) for r in conn.execute("SELECT * FROM preview ORDER BY id DESC LIMIT ?", (int(limit),))]


# ---------------------------------------------------------------- making one
def _subject(conn, subject_type, subject_id):
    """(record, title, source (root, rel)) for the thing a preview is of."""
    if subject_type == "download_import":
        from synchub import downloads
        row = downloads.get(conn, subject_id)
        if row is None:
            raise PreviewError("That import is gone.")
        rec = store.get_record(conn, row["record_id"]) if row["record_id"] else None
        if rec is None or rec["path_key"] != row["path_key"]:
            raise PreviewError("The file has not been analysed yet.")
    elif subject_type == "library_file":
        rec = store.get_record(conn, subject_id)
        if rec is None:
            raise PreviewError("That file is not in the Library.")
    else:
        raise PreviewError("Unknown kind of file.")
    if rec["analysis_status"] != v.ANALYSIS_COMPLETED:
        raise PreviewError("The file has not been analysed yet.")
    return rec, rec["file_name"] if rec.get("file_name") else rec["relative_path"], (rec["root_name"], rec["relative_path"])


def plans_of(rec, approved=False):
    key = "approved_track_changes_json" if approved and rec.get("approved_track_changes_json") else "proposed_track_changes_json"
    return [p for p in v.json_list(rec.get(key)) if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")]


def create(conn, subject_type, subject_id, by, mode="random", seconds=None, now=None):
    """Queues the job that makes a preview. Returns the preview id. Raises PreviewError (a sentence for the page) when the file would not be compressed."""
    if mode not in MODES:
        raise PreviewError("Choose a random window or the most demanding part.")
    rec, title, source = _subject(conn, subject_type, subject_id)
    live = live_for(conn, subject_type, subject_id)
    if live and live["status"] in ("queued", "making"):
        raise PreviewError("A preview of this file is already being made.")
    plans = plans_of(rec, approved=rec.get("approval_status") == v.APPROVAL_APPROVED)
    size = size_policy.decide(rec, plans, size_policy.load(conn, profiles.effective(conn, rec)))
    if size.profile is None:
        raise PreviewError("The size rules would leave this file alone, so there is nothing to preview. " + (size.summary or ""))
    plan, _ = size_policy.apply({"change": dict(plans[0]) if plans else {}}, size, None)
    seconds = int(seconds or clip_seconds(conn))
    now = now or db.now()
    sig = signature(rec)
    stats = {"plan": {**size.numbers, "duration": rec.get("duration_seconds")}, "notes": size.notes, "reasons": size.reasons, "duration": rec.get("duration_seconds")}
    preview_id = conn.execute(
        "INSERT INTO preview (subject_type, subject_id, record_id, title, mode, seconds, target_gb, signature, status, message, stats_json, source_bytes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'Waiting for a worker.', ?, ?, ?, ?)",
        (subject_type, subject_id, rec["id"], title, mode, seconds, size.numbers.get("target_gb"), sig, json.dumps(stats), rec.get("size_bytes"), now, now)).lastrowid
    plan.update(op="preview", needs=[*plan.get("needs", []), "preview"], seconds=seconds, mode=mode, seed=preview_id * 7919 + int(rec.get("size_bytes") or 0) % 10007,
                duration=rec.get("duration_seconds"), preview_id=preview_id, source_size=rec.get("size_bytes"), source_mtime=rec.get("mtime"))
    job_id = jobs.create(conn, "preview", title=f"Preview: {title}", ref=(REF, preview_id), source=source, plan=plan, need_bytes=2 * GIB, max_attempts=2)
    conn.execute("UPDATE preview SET job_id = ? WHERE id = ?", (job_id, preview_id))
    logger.info(f"[previews] preview {preview_id} of {title} queued by {by} ({mode}, {seconds} s)")
    return preview_id


def maybe_make_for(conn, subject_type, subject_id, by="downloads"):
    """Makes a preview for something waiting on one, unless there is one already or the setting is off. Never raises."""
    if not auto_make(conn) or live_for(conn, subject_type, subject_id):
        return None
    try:
        return create(conn, subject_type, subject_id, by)
    except PreviewError as exc:
        logger.info(f"[previews] no automatic preview for {subject_type} {subject_id}: {exc}")
        return None


# ---------------------------------------------------------------- following the job
def sync(conn, job_id, now=None):
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    row = get(conn, job["ref_id"])
    if row is None or row["job_id"] != job_id:
        return False
    target, now = JOB_STATUS.get(job["status"], "queued"), now or db.now()
    if row["status"] == target and target != "ready":
        return False
    if row["status"] in ("ready", "expired") or row["decision"]:
        return False
    values = {"status": target, "updated_at": now}
    if target == "ready":
        result = job["result"] if isinstance(job["result"], dict) else {}
        missing = [n for n in CLIP_NAMES if not os.path.isfile(clip_path(row["id"], n))]
        if missing:
            values.update(status="failed", message="The worker finished but its clips did not arrive. Make another preview.")
        else:
            stats = _json(row["stats_json"])
            stats["clip"] = result
            values.update(stats_json=json.dumps(stats), start_seconds=result.get("start"), encoded_bytes=result.get("encoded_bytes"), source_bytes=result.get("source_clip_bytes") or row["source_bytes"],
                          message="Ready to watch.", expires_at=(datetime.now(timezone.utc) + timedelta(days=keep_days(conn))).strftime("%Y-%m-%d %H:%M:%S"))
    elif target in ("failed", "cancelled"):
        values["message"] = (job["error"] or job["review_reason"] or job["message"] or ("Cancelled." if target == "cancelled" else "The worker could not make the clips."))[:400]
    elif target == "making":
        values["message"] = "A worker is making the clips."
    else:
        values["message"] = "Waiting for a worker."
    conn.execute(f"UPDATE preview SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", [*values.values(), row["id"]])
    return True


def sync_active(conn):
    """Brings previews in step with their jobs (one re-queued after its worker went silent, a job cancelled from the Workers page)."""
    for r in conn.execute("SELECT j.id FROM job j JOIN preview p ON p.job_id = j.id WHERE j.ref_type = ? AND p.status IN ('queued', 'making') AND p.decision IS NULL", (REF,)).fetchall():
        sync(conn, r["id"])


# ---------------------------------------------------------------- deciding
def _trust(conn, sig, delta=None, reset=False):
    if reset:
        conn.execute("INSERT INTO preview_trust (signature, approved) VALUES (?, 0) ON CONFLICT(signature) DO UPDATE SET approved = 0, updated_at = ?", (sig, db.now()))
    elif delta:
        conn.execute("INSERT INTO preview_trust (signature, approved) VALUES (?, ?) ON CONFLICT(signature) DO UPDATE SET approved = approved + ?, updated_at = ?", (sig, delta, delta, db.now()))


def _mark(conn, row, decision, by, message):
    conn.execute("UPDATE preview SET decision = ?, decided_by = ?, decided_at = ?, message = ?, updated_at = ? WHERE id = ?", (decision, by, db.now(), message, db.now(), row["id"]))


def set_target(conn, record_id, target_gb):
    try:
        target = float(target_gb)
    except (TypeError, ValueError):
        raise PreviewError("Type the size target in GB, for example 12.")
    if not 1 <= target <= 200:
        raise PreviewError("The target has to be between 1 and 200 GB.")
    if store.get_record(conn, record_id) is None:
        raise PreviewError("That file is not in the Library.")
    store._update(conn, record_id, {"size_target_gb": round(target, 1)})
    return round(target, 1)


def decide(conn, preview_id, action, by, target_gb=None):
    """What a person does after watching. Returns a sentence for the page.
    approve: the compression goes ahead (a download is queued) and counts toward trusting this kind of file; with `target_gb` the target is changed first and
    it does not count. retarget: a new clip at another target. another: a new window. skip: leave the file. reject: leave it and stop trusting this kind."""
    row = get(conn, preview_id)
    if row is None:
        raise PreviewError("That preview is gone.")
    if row["decision"]:
        raise PreviewError(f"This preview was already answered ({row['decision']}).")
    if action == "approve" and row["status"] in ("queued", "making"):
        raise PreviewError("The clips are not ready yet.")
    subject = (row["subject_type"], row["subject_id"])
    if action == "approve":
        changed = target_gb not in (None, "") and abs(float(target_gb) - float(row["target_gb"] or 0)) > 0.05
        if changed:
            set_target(conn, row["record_id"], target_gb)
        if row["subject_type"] == "download_import":
            from synchub import downloads
            try:
                downloads.approve(conn, row["subject_id"], by)
            except ValueError as exc:
                raise PreviewError(str(exc))
        if changed or row["status"] != "ready":
            _trust(conn, row["signature"], reset=changed)
        else:
            _trust(conn, row["signature"], delta=1)
        _mark(conn, row, "approve", by, "Approved" + (f" at {float(target_gb):g} GB" if changed else "") + f" by {by}.")
        return "Approved. The compression is queued." if row["subject_type"] == "download_import" else "Noted. Approve the file in the Library to compress it" + (f" at {float(target_gb):g} GB." if changed else ".")
    if action in ("retarget", "another"):
        if action == "retarget":
            set_target(conn, row["record_id"], target_gb)
            _trust(conn, row["signature"], reset=True)
        _mark(conn, row, action, by, "A new clip was asked for." if action == "another" else f"Trying {float(target_gb):g} GB.")
        mode = "worst" if action == "another" and row["mode"] == "random" else "random"
        new_id = create(conn, *subject, by, mode=mode if action == "another" else row["mode"], seconds=row["seconds"])
        return f"Making another clip (preview {new_id})."
    if action == "skip":
        if row["subject_type"] == "download_import":
            from synchub import downloads
            try:
                downloads.skip(conn, row["subject_id"], by)
            except ValueError as exc:
                raise PreviewError(str(exc))
        _mark(conn, row, "skip", by, f"Left as it is ({by}).")
        return "Left as it is."
    if action == "reject":
        if row["subject_type"] == "download_import":
            from synchub import downloads
            try:
                downloads.skip(conn, row["subject_id"], by)
            except ValueError:
                pass
        _trust(conn, row["signature"], reset=True)
        _mark(conn, row, "reject", by, f"Rejected by {by}: this kind of file will be previewed again.")
        return "Rejected. Files like this one will be previewed again before they are compressed."
    raise PreviewError("Unknown action.")


# ---------------------------------------------------------------- tidying
def purge(now=None):
    """Removes the clips of previews whose time is up, and of previews that were answered. Runs daily. Returns how many were cleared."""
    now = now or db.now()
    cleared = 0
    with db.connection() as conn:
        rows = conn.execute("SELECT id, status, decision FROM preview WHERE status != 'expired' AND ((expires_at IS NOT NULL AND expires_at <= ?) OR (decision IS NOT NULL AND decided_at <= datetime(?, '-1 day')))", (now, now)).fetchall()
        for r in rows:
            remove_files(r["id"])
            conn.execute("UPDATE preview SET status = 'expired', updated_at = ? WHERE id = ?", (now, r["id"]))
            cleared += 1
        if os.path.isdir(root_folder()):                                    # folders no row knows (a database restored from a backup)
            known = {str(r["id"]) for r in conn.execute("SELECT id FROM preview WHERE status != 'expired'")}
            for name in os.listdir(root_folder()):
                if name not in known:
                    shutil.rmtree(os.path.join(root_folder(), name), ignore_errors=True)
    return cleared


def summary(conn):
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM preview WHERE decision IS NULL GROUP BY status")}
    return {"ready": counts.get("ready", 0), "making": counts.get("queued", 0) + counts.get("making", 0)}
