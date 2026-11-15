"""Compression test: how would this film look after the planned conversion, found out before converting it.

A worker encodes a few short clips of the film (default 4 of 15 seconds, spread over the film) with exactly the settings the real conversion would use, scores
each against the same window of the film with VMAF (CPU libvmaf) and reports numbers only; nothing is kept on the worker or uploaded. SyncHub works out the
verdict and the size estimate here, so the rules can change without a worker update:

    {"op": "compress_test", "encoder": "cpu", "hdr": "sdr", "height": 1080, "floor": 93.0, "mean": 94.1, "min": 90.8, "elapsed": 190.2,
     "clips": [{"start": 812.0, "length": 15.0, "source_kbps": 31200, "encoded_kbps": 11800, "vmaf": 95.2}, ...], "skipped": null}

A single random window can land on an easy scene, so the verdict looks at the average and at the worst clip:
pass = average at or above the floor and no clip more than `margin` under it; borderline = average passes but one clip is further under; fail = average under the floor.
HDR and Dolby Vision pictures are not scored (VMAF is not meaningful on them as they are): the clips are still encoded, so the size estimate is real.
"""
import json
import logging

from synchub import db, jobs, settings
from synchub.library import profiles, size_policy
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

REF = "compression_test"
GIB = 1024 ** 3
JOB_STATUS = {"queued": "queued", "running": "running", "done": "done", "failed": "failed", "needs_review": "failed", "cancelled": "cancelled"}
VERDICT_LABELS = {"pass": "Pass", "borderline": "Borderline", "fail": "Fail"}
VERDICT_TEXT = {
    "pass": "The picture should look the same as the original.",
    "borderline": "The average passes, but at least one part would look visibly worse than the original.",
    "fail": "The converted picture would look visibly worse than the original.",
}
PRIORITY = 80                       # someone pressed a button and is waiting, but a download or a conversion still goes first
NEED_BYTES = 2 * GIB


class CompressionTestError(ValueError):
    """A sentence for the page."""


# ---------------------------------------------------------------- settings
def _int(key, default, low, high, conn=None):
    try:
        return max(low, min(high, int(float(settings.get(key, conn=conn)))))
    except (TypeError, ValueError):
        return default


def clip_count(conn=None):
    return _int("ctest_clips", 4, 2, 8, conn)


def clip_seconds(conn=None):
    return _int("ctest_seconds", 15, 10, 30, conn)


def margin(conn=None):
    return _int("ctest_margin", 8, 0, 30, conn)


# ---------------------------------------------------------------- the rules
def verdict_for(mean, worst, floor, allowed=8):
    """pass, borderline or fail; None when nothing was scored."""
    if mean is None or floor is None:
        return None
    if mean < floor:
        return "fail"
    if worst is not None and worst < floor - allowed:
        return "borderline"
    return "pass"


def estimate_gb(clips, numbers, profile, video_kbps=None):
    """Whole-file size the clips predict, or None. A bitrate-targeted encode (the usual rule) lands near its target on every clip, so the clips' own bitrate is the
    estimate; a constant-quality encode scales the file's average bitrate by how much the clips shrank."""
    done = [c for c in clips if c.get("encoded_kbps") and c.get("length")]
    duration = (numbers or {}).get("duration")
    if not done or not duration:
        return None
    weight = sum(c["length"] for c in done)
    encoded = sum(c["encoded_kbps"] * c["length"] for c in done) / weight
    if (profile or {}).get("rate_mode") != "target":
        source = sum((c.get("source_kbps") or 0) * c["length"] for c in done) / weight
        if not source or not video_kbps:
            return None
        encoded = video_kbps * encoded / source
    other = ((numbers.get("audio_kbps") or 0) + (numbers.get("subtitle_kbps") or 0))
    return round((encoded + other) * 1000 / 8 * duration / GIB, 1)


def _profile_key(profile):
    return "|".join(str(profile.get(k)) for k in ("name", "crf", "preset", "rate_mode", "maxrate_kbps", "gpu", "ten_bit"))


def _plan_for(conn, rec):
    """(plan, profile, size decision) for the conversion that would run now. Raises CompressionTestError when no compression is planned."""
    plans = [p for p in v.json_list(rec.get("approved_track_changes_json") if rec.get("approval_status") == v.APPROVAL_APPROVED and rec.get("approved_track_changes_json") else rec.get("proposed_track_changes_json"))
             if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")]
    profile = profiles.effective(conn, rec)
    size = size_policy.decide(rec, plans, size_policy.load(conn, profile))
    if size.profile is None:
        raise CompressionTestError("The size rules would leave this file alone, so there is nothing to test. " + (size.summary or ""))
    base = {"change": dict(plans[0]) if plans else {}}
    if profile["video_codec"] == "hevc":
        base["profile"] = profiles.for_plan(profile)
    plan, _ = size_policy.apply(base, size, None)
    return plan, profile, size


# ---------------------------------------------------------------- asking for one
def request(conn, file_id, by="someone", now=None):
    """Queues a compression test of one library file. Returns its id (the one already waiting when there is one). Raises CompressionTestError."""
    row = conn.execute("SELECT * FROM library_file WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise CompressionTestError("That file is not in the Library.")
    rec = dict(row)
    if rec.get("missing_since"):
        raise CompressionTestError("This file is missing from its share, so it cannot be tested.")
    if rec["analysis_status"] != v.ANALYSIS_COMPLETED or not rec.get("duration_seconds"):
        raise CompressionTestError("The file has not been analysed yet. Scan its folder first.")
    waiting = conn.execute("SELECT id FROM compression_test WHERE library_file_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    if waiting:
        return waiting["id"]
    plan, profile, size = _plan_for(conn, rec)
    now = now or db.now()
    clips, seconds, allowed = clip_count(conn), clip_seconds(conn), margin(conn)
    test_id = conn.execute(
        "INSERT INTO compression_test (library_file_id, size_bytes, mtime, profile_key, profile_name, status, clips, clip_seconds, margin, from_gb, requested_by, message, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, 'Waiting for a worker.', ?, ?)",
        (file_id, rec["size_bytes"], rec["mtime"], _profile_key(plan["profile"]), profile["name"], clips, seconds, allowed, size.numbers.get("size_gb"), by, now, now)).lastrowid
    title = rec.get("recommended_name") or rec["file_name"]
    plan.update(op="compress_test", needs=[*[n for n in plan.get("needs", []) if n != "preview"], "compress_test"], clips=clips, seconds=seconds, duration=rec["duration_seconds"],
                source_size=rec["size_bytes"], source_mtime=rec["mtime"], seed=test_id * 7919 + int(rec["size_bytes"] or 0) % 10007, test_id=test_id)
    job_id = jobs.create(conn, "scan", title=f"Compression test: {title}", ref=(REF, test_id), source=(rec["root_name"], rec["relative_path"]), plan=plan, priority=PRIORITY,
                         need_bytes=NEED_BYTES, max_attempts=2, now=now)
    conn.execute("UPDATE compression_test SET job_id = ? WHERE id = ?", (job_id, test_id))
    logger.info(f"[compression_test] test {test_id} of {title} queued by {by} ({clips} clips of {seconds} s, {profile['name']})")
    return test_id


# ---------------------------------------------------------------- following the job
def get(conn, test_id):
    r = conn.execute("SELECT * FROM compression_test WHERE id = ?", (test_id,)).fetchone()
    return dict(r) if r else None


def sync(conn, job_id, now=None):
    """Brings a test in step with its job. Returns True when something changed."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    row = get(conn, job["ref_id"])
    if row is None or row["job_id"] != job_id or row["status"] == "done":
        return False
    target, now = JOB_STATUS.get(job["status"], "queued"), now or db.now()
    if row["status"] == target and target != "done":
        return False
    values = {"status": target, "updated_at": now}
    if target == "done":
        result = job["result"] if isinstance(job["result"], dict) else {}
        rec = conn.execute("SELECT size_bytes, mtime, video_info_json FROM library_file WHERE id = ?", (row["library_file_id"],)).fetchone()
        if rec is None or rec["size_bytes"] != row["size_bytes"] or rec["mtime"] != row["mtime"]:
            values.update(status="failed", message="The file changed while it was being tested. Test it again.", finished_at=now)
        elif result.get("op") != "compress_test" or not result.get("clips"):
            values.update(status="failed", message="The worker sent no clips back. Update the worker on the Workers page and test again.", finished_at=now)
        else:
            plan = job["plan"] if isinstance(job.get("plan"), dict) else {}
            info = _json(rec["video_info_json"])
            mean, worst, floor = result.get("mean"), result.get("min"), result.get("floor")
            verdict = verdict_for(mean, worst, floor, row["margin"] if row["margin"] is not None else 8)
            est = estimate_gb(result["clips"], plan.get("size_policy"), plan.get("profile"), info.get("video_kbps"))
            values.update(mean_vmaf=mean, min_vmaf=worst, floor=floor, verdict=verdict, est_gb=est, result_json=json.dumps(result), finished_at=now,
                          message=f"{VERDICT_LABELS[verdict]}: VMAF {mean:g}, worst {worst:g}." if verdict else (result.get("skipped") or "Not scored."))
    elif target in ("failed", "cancelled"):
        values.update(message=(job["error"] or job["review_reason"] or job["message"] or ("Cancelled." if target == "cancelled" else "The worker could not run the test."))[:400], finished_at=now)
    elif target == "running":
        values["message"] = job.get("message") or "A worker is testing the compression."
    conn.execute(f"UPDATE compression_test SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", [*values.values(), row["id"]])
    return True


def sync_active(conn):
    for r in conn.execute("SELECT job_id FROM compression_test WHERE status IN ('queued', 'running') AND job_id IS NOT NULL").fetchall():
        sync(conn, r["job_id"])


# ---------------------------------------------------------------- reading
def _json(text):
    if isinstance(text, dict):
        return text
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _clock(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def describe(conn, rec):
    """What the Picture quality card shows: the latest test of this file (None when there is none), with the clips laid out and whether it still applies."""
    row = conn.execute("SELECT * FROM compression_test WHERE library_file_id = ? ORDER BY id DESC LIMIT 1", (rec["id"],)).fetchone()
    if row is None:
        return None
    row = dict(row)
    out = {"id": row["id"], "status": row["status"], "message": row["message"], "profile": row["profile_name"], "clips": row["clips"], "clip_seconds": row["clip_seconds"],
           "when": (row["finished_at"] or row["created_at"] or "")[:10], "verdict": row["verdict"], "verdict_label": VERDICT_LABELS.get(row["verdict"]), "verdict_text": VERDICT_TEXT.get(row["verdict"]),
           "mean": row["mean_vmaf"], "min": row["min_vmaf"], "floor": row["floor"], "margin": row["margin"], "from_gb": row["from_gb"], "est_gb": row["est_gb"]}
    result = _json(row["result_json"])
    out["parts"] = [{"at": _clock(c.get("start")), "vmaf": c.get("vmaf"), "source_mbps": round((c.get("source_kbps") or 0) / 1000, 1), "encoded_mbps": round((c.get("encoded_kbps") or 0) / 1000, 1)}
                    for c in result.get("clips") or []]
    scored = [p for p in out["parts"] if p["vmaf"] is not None]
    out["worst_at"] = min(scored, key=lambda p: p["vmaf"])["at"] if scored else None
    out["skipped"], out["encoder"], out["elapsed"], out["hdr"] = result.get("skipped"), result.get("encoder"), result.get("elapsed"), result.get("hdr")
    current = row["size_bytes"] == rec.get("size_bytes") and row["mtime"] == rec.get("mtime")
    if current and row["status"] == "done":
        try:
            current = _profile_key(_plan_for(conn, rec)[0]["profile"]) == row["profile_key"]
        except CompressionTestError:
            current = False
    out["current"] = current
    return out
