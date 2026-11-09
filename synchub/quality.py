"""Quality checks: how good does a film file look, judged on its own (no original to compare with).

A worker samples a few short slices of the file and measures what a viewer would notice: blockiness, blur, interlacing, frozen frames and decode
errors (the `quality` job op, a `scan`-kind job). The worker only reports numbers. SyncHub works out the score, the grade and the plain-language
findings here, so the rules can be tuned without updating a worker, and the stored numbers can be scored again.

The thresholds below are starting points, not measured facts. Bitrate per pixel, decode errors, interlacing and frozen frames are reliable signals.
Block and blur values vary with the film (grain, soft photography), so they only ever lower the score a little and are named as advisory.
Bump CHECK_VERSION whenever the scoring changes; results from an older version show as "re-check available".
"""
import json
import logging
import os
import statistics

from synchub import db, jobs, settings
from synchub import versions as extra_versions
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

REF = "quality_check"
CHECK_VERSION = 2
GRADES = (("good", 80), ("ok", 65), ("poor", 45), ("bad", 0))
LIVE = ("queued", "running")
JOB_STATUS = {"queued": "queued", "running": "running", "done": "done", "failed": "failed", "needs_review": "failed", "cancelled": "cancelled"}
SOURCES = ("manual", "download", "cleanup", "convert")
MODES = ("quality", "damage")
PRIORITY = {"manual": 70, "download": 110, "cleanup": 5, "convert": 90}
DAMAGE_IN_FLIGHT = 1                  # whole-file reads are heavy: one at a time
CLEANUP_IN_FLIGHT = 3                 # the background pass never has more than this many checks waiting or running

# How efficient a codec is compared with H.264, so the bits-per-pixel limits mean the same thing for every file.
CODEC_EFFICIENCY = {"hevc": 1.6, "h265": 1.6, "av1": 2.0, "vp9": 1.6, "h264": 1.0, "avc": 1.0, "vc1": 0.9, "mpeg4": 0.7, "msmpeg4": 0.6, "mpeg2video": 0.5, "mpeg1video": 0.4}
# Penalties grow smoothly between these points (value, points lost), so a file just either side of a line is treated almost the same.
BPP_CURVE = ((0.070, 0), (0.045, 10), (0.030, 28), (0.020, 45))
BLOCK_CURVE = ((20.0, 0), (25.0, 12), (30.0, 25))
BLUR_CURVE = ((9.0, 0), (12.0, 10), (16.0, 20))         # per 1080 lines of picture height
MIN_PICTURE_SHARE = 0.9              # a picture this much of the minimum's width counts as meeting it (a 1920x800 scope film meets 1080p)


class QualityError(ValueError):
    """Something a person asked for that cannot be done; the message is meant for the page."""


# ---------------------------------------------------------------- settings
def _int(key, default, low, high, conn=None):
    try:
        return max(low, min(high, int(float(settings.get(key, conn=conn)))))
    except (TypeError, ValueError):
        return default


def samples(conn=None):
    return _int("quality_samples", 10, 1, 24, conn)


def slice_seconds(conn=None):
    return _int("quality_slice_seconds", 30, 10, 120, conn)


def flag_below(conn=None):
    return _int("quality_flag_below", 60, 1, 100, conn)


def min_height(conn=None):
    """The smallest picture height a person wants (0 = no minimum)."""
    return _int("quality_min_height", 1080, 0, 4320, conn)


def on_download(conn=None):
    return settings.get("quality_on_download", conn=conn) == "flag"


def paused(conn=None):
    """True while automatic checks (new downloads and the Library pass) are switched off. A check someone asks for by hand still runs."""
    return settings.get("quality_paused", conn=conn) == "1"


def damage_pass(conn=None):
    """The background pass that reads whole files for decode errors (off by default)."""
    return settings.get("quality_damage_pass", conn=conn) == "1"


def library_pass(conn=None):
    return settings.get("quality_scan_library", conn=conn) == "1"


def newest_first(conn=None):
    return settings.get("quality_scan_order", conn=conn) != "oldest"


def _json(text, default):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


# ---------------------------------------------------------------- scoring
def grade_for(score):
    return next(name for name, floor in GRADES if score >= floor)


def _median(values):
    values = [x for x in values if x is not None]
    return statistics.median(values) if values else None


def _facts(rec):
    """(width, height, fps, video kbps, codec) from a library record; any may be None."""
    info = _json(rec.get("video_info_json"), {})
    width, height = info.get("width"), info.get("height")
    if not (width and height):
        try:
            width, height = (int(x) for x in str(rec.get("resolution") or "").lower().split("x"))
        except ValueError:
            width = height = None
    try:
        fps = float(rec.get("frame_rate") or 0) or None
    except (TypeError, ValueError):
        fps = None
    kbps = info.get("video_kbps") or rec.get("bitrate_kbps")
    return width, height, fps, kbps, str(rec.get("video_codec") or info.get("codec") or "").lower()


def bits_per_pixel(rec):
    """Bits per pixel per frame, scaled so one number means the same for H.264 and HEVC (an H.264-equivalent figure). None when it cannot be worked out."""
    width, height, fps, kbps, codec = _facts(rec)
    if not (width and height and fps and kbps):
        return None
    efficiency = next((value for name, value in CODEC_EFFICIENCY.items() if name in codec), 1.0)
    return round(kbps * 1000 / (width * height * fps) * efficiency, 4)


def _curve(value, points):
    """Points lost for a value, by straight lines between the curve's points; flat beyond the ends."""
    first, last = points[0], points[-1]
    ascending = last[0] > first[0]
    if (value <= first[0]) if ascending else (value >= first[0]):
        return 0.0 if first[1] == 0 else float(first[1])
    if (value >= last[0]) if ascending else (value <= last[0]):
        return float(last[1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if (x0 <= value <= x1) if ascending else (x1 <= value <= x0):
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return 0.0


def below_minimum(rec, min_height):
    """(width, height) when the picture is smaller than the minimum resolution, else None. Judged by width as well as height, so a widescreen film with black bars
    (1920x800) still meets 1080p."""
    if not min_height:
        return None
    width, height, _, _, _ = _facts(rec)
    if not (width and height):
        return None
    need = min_height * 16 / 9 * MIN_PICTURE_SHARE
    return None if max(width, height * 16 / 9) >= need else (width, height)


def resolution_finding(rec, min_height):
    found = below_minimum(rec, min_height)
    if not found:
        return None
    return {"key": "below_minimum", "severity": "bad", "text": f"Below your minimum resolution: the picture is {found[0]}x{found[1]} and you want at least {min_height}p. A bigger copy is needed; stretching this one adds no detail."}


def score(metrics, rec):
    """{'score', 'grade', 'findings'} from what the worker measured and the file's record. Pure: the same inputs always give the same answer."""
    slices = [s for s in (metrics or {}).get("slices") or [] if isinstance(s, dict)]
    findings, penalty = [], 0.0

    def add(key, severity, points, text):
        nonlocal penalty
        penalty += points
        findings.append({"key": key, "severity": severity, "text": text})

    if not slices:
        return {"score": None, "grade": None, "findings": [{"key": "no_data", "severity": "info", "text": "The worker could not measure any part of the file."}]}
    total_seconds = sum(float(s.get("seconds") or 0) for s in slices) or 1.0
    broken = [s for s in slices if (s.get("errors") or 0) > 0]
    if broken:
        frac = len(broken) / len(slices)
        add("decode_errors", "bad", 42 + 38 * frac,
            f"The video had decode errors in {len(broken)} of {len(slices)} sampled parts ({sum(s.get('errors') or 0 for s in broken)} messages in all): the file is damaged or badly encoded.")
    bpp = bits_per_pixel(rec)
    if bpp is not None:
        lost = _curve(bpp, BPP_CURVE)
        if lost >= 1:
            if lost < 4:
                add("low_bitrate", "info", lost, f"Bitrate is a little thin for the picture: {bpp:.3f} bits per pixel (H.264 equivalent). Fine for most films.")
            elif lost < 20:
                add("low_bitrate", "warn", lost, f"Few bits for the picture: {bpp:.3f} bits per pixel (H.264 equivalent). Dark scenes and grain may show blocks.")
            else:
                add("low_bitrate", "bad", lost, f"Far too few bits for the picture: {bpp:.3f} bits per pixel (H.264 equivalent). Dark scenes and grain will look blocky.")
    block = _median(s.get("block") for s in slices)
    if block is not None:
        lost = _curve(block, BLOCK_CURVE)
        if lost >= 1:
            add("blocky", "warn" if lost >= 8 else "info", lost, f"Some blockiness (advisory): block score {block:.1f}; clean films usually stay under {BLOCK_CURVE[0][0]:g}.")
    _, height, _, _, _ = _facts(rec)
    blur = _median(s.get("blur") for s in slices)
    if blur is not None and height:
        per_1080 = blur * 1080 / height
        lost = _curve(per_1080, BLUR_CURVE)
        if lost >= 1:
            add("blurry", "warn" if lost >= 8 else "info", lost, f"{'Soft or blurry picture' if lost >= 8 else 'Slightly soft picture'} (advisory): blur score {per_1080:.1f} per 1080 lines. Some films are shot soft on purpose.")
    interlaced = _median(s.get("interlaced") for s in slices)
    if interlaced is not None and interlaced > 0.5:
        add("interlaced", "warn", 12, f"The picture looks interlaced ({interlaced * 100:.0f}% of frames): expect combing on moving edges unless the player deinterlaces.")
    frozen = sum(float(s.get("freeze_seconds") or 0) for s in slices) / total_seconds
    if frozen > 0.10:
        add("frozen", "bad" if frozen > 0.30 else "warn", 30 if frozen > 0.30 else 15,
            f"The picture was frozen for {frozen * 100:.0f}% of the sampled time: repeated frames, which looks like stutter.")
    value = max(0.0, min(100.0, 100.0 - penalty))
    return {"score": round(value, 1), "grade": grade_for(value), "findings": findings}


# ---------------------------------------------------------------- reading
def _present(row):
    if row is None:
        return None
    d = dict(row)
    d["findings"] = _json(d.pop("findings_json", None), [])
    d["metrics"] = _json(d.pop("metrics_json", None), {})
    return d


def get(conn, check_id):
    return _present(conn.execute("SELECT * FROM quality_check WHERE id = ?", (check_id,)).fetchone())


def latest(conn, file_id, done_only=False, mode="quality"):
    sql = "SELECT * FROM quality_check WHERE library_file_id = ? AND mode = ?" + (" AND status = 'done'" if done_only else "") + " ORDER BY id DESC LIMIT 1"
    return _present(conn.execute(sql, (file_id, mode)).fetchone())


def _record(conn, file_id):
    r = conn.execute("SELECT * FROM library_file WHERE id = ?", (file_id,)).fetchone()
    return dict(r) if r else None


def is_current(check, rec):
    """Does a finished check describe the file as it is now, under today's scoring rules?"""
    return bool(check) and check["status"] == "done" and check["size_bytes"] == rec["size_bytes"] and check["mtime"] == rec["mtime"] and check["check_version"] == CHECK_VERSION


def is_accepted(conn, rec):
    row = conn.execute("SELECT size_bytes, mtime FROM quality_accept WHERE library_file_id = ?", (rec["id"],)).fetchone()
    return bool(row) and row["size_bytes"] == rec["size_bytes"] and row["mtime"] == rec["mtime"]


def state_for(conn, rec, check=None):
    """What to show for one file: {'check', 'result', 'current', 'low', 'accepted', 'small', 'label', 'extra'}. `low` is true for a file under the minimum resolution, or with a
    current check below the flag threshold, that nobody accepted. `extra` holds findings that do not come from the check (the resolution)."""
    check = check if check is not None else latest(conn, rec["id"])
    done = check if check and check["status"] == "done" else (latest(conn, rec["id"], done_only=True) if check else None)
    current = is_current(done, rec)
    accepted = is_accepted(conn, rec)
    small = resolution_finding(rec, min_height(conn))
    scored_low = bool(current and done["score"] is not None and done["score"] < flag_below(conn))
    low = bool((scored_low or small) and not accepted)
    if check and check["status"] in LIVE:
        label = "Checking" if check["status"] == "running" else "Waiting for a worker"
    elif accepted and (scored_low or small):
        label = "Accepted as is"
    elif small:
        label = "Too small, needs a bigger copy"
    elif done is None:
        label = "Not checked"
    elif done["score"] is None:
        label = "No result"
    else:
        label = ("Low quality" if low else {"good": "Good", "ok": "OK", "poor": "Poor", "bad": "Bad"}.get(done["grade"], "")) + ("" if current else " (old)")
    asked = conn.execute("SELECT * FROM quality_request WHERE library_file_id = ? ORDER BY id DESC LIMIT 1", (rec["id"],)).fetchone()
    return {"check": check, "result": done, "current": current, "low": low, "accepted": accepted, "small": bool(small), "label": label, "extra": [small] if small else [],
            "request": ({**dict(asked), "track_label": track_label(dict(asked))} if asked else None)}


# ---------------------------------------------------------------- asking for a check
def request(conn, file_id, by="someone", source="manual", now=None, mode="quality"):
    """Queues a quality check of one library file (mode "damage": reads the whole file for decode errors, no score). Returns the check id, or the id of the one already waiting. Raises QualityError for a sentence on the page."""
    rec = _record(conn, file_id)
    if rec is None:
        raise QualityError("That file is not in the Library.")
    if source not in SOURCES:
        source = "manual"
    if mode not in MODES:
        mode = "quality"
    if rec.get("missing_since"):
        raise QualityError("This file is missing from its share, so it cannot be checked.")
    if source in ("download", "cleanup") and below_minimum(rec, min_height(conn)):
        raise QualityError("Too small: the picture is under your minimum resolution, so it is not checked automatically. It needs a bigger copy. Press Check quality to check it anyway.")
    if rec["analysis_status"] != v.ANALYSIS_COMPLETED or not rec.get("duration_seconds"):
        raise QualityError("The file has not been analysed yet. Scan its folder first.")
    waiting = conn.execute("SELECT id FROM quality_check WHERE library_file_id = ? AND mode = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1", (file_id, mode)).fetchone()
    if waiting:
        return waiting["id"]
    now = now or db.now()
    check_id = conn.execute("INSERT INTO quality_check (library_file_id, size_bytes, mtime, source, status, requested_by, message, created_at, updated_at, mode) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                            (file_id, rec["size_bytes"], rec["mtime"], source, by, "Waiting for a worker.", now, now, mode)).lastrowid
    title = rec.get("recommended_name") or rec["file_name"]
    plan = {"op": "quality", "needs": ["quality", "damage"] if mode == "damage" else ["quality"], "mode": mode, "check_id": check_id, "samples": samples(conn), "seconds": slice_seconds(conn), "duration": rec["duration_seconds"],
            "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "seed": check_id * 7919 + int(rec["size_bytes"] or 0) % 10007}
    job_id = jobs.create(conn, "scan", title=(f"Damage check: {title}" if mode == "damage" else f"Quality check: {title}"), ref=(REF, check_id), source=(rec["root_name"], rec["relative_path"]), plan=plan,
                         priority=PRIORITY[source], max_attempts=2, now=now)
    conn.execute("UPDATE quality_check SET job_id = ? WHERE id = ?", (job_id, check_id))
    logger.info(f"[quality] check {check_id} of {title} queued by {by} ({source})")
    return check_id


def request_for_import(conn, rec, now=None):
    """Called when a new Radarr/Sonarr import has been analysed. Does nothing when the setting is off or the file already has a current check. Never raises."""
    try:
        if not on_download(conn) or paused(conn) or is_current(latest(conn, rec["id"], done_only=True), rec):
            return None
        return request(conn, rec["id"], by="download", source="download", now=now)
    except (QualityError, jobs.JobError) as exc:
        logger.info(f"[quality] no check for the new file {rec.get('file_name')}: {exc}")
        return None


# ---------------------------------------------------------------- results
def sync(conn, job_id, now=None):
    """Brings a check in step with its job. Returns True when something changed."""
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
        rec = _record(conn, row["library_file_id"])
        metrics = job["result"] if isinstance(job["result"], dict) else {}
        if rec is None or rec["size_bytes"] != row["size_bytes"] or rec["mtime"] != row["mtime"]:
            values.update(status="failed", message="The file changed while it was being checked. Check it again.", finished_at=now)
        elif row.get("mode") == "damage":
            if metrics.get("mode") != "damage":
                values.update(status="failed", message="This worker is too old for a damage check. Update it on the Workers page.", finished_at=now)
            else:
                errors = int(metrics.get("errors") or 0)
                found = [{"severity": "bad", "text": f"Damaged: {errors} decode error{'s' if errors != 1 else ''} when the whole file was read" + (f" ({metrics['sample'][0][:120]})" if metrics.get("sample") else "")}] if errors else []
                values.update(check_version=CHECK_VERSION, score=None, grade=None, findings_json=json.dumps(found), metrics_json=json.dumps(metrics), samples=0, finished_at=now,
                              message=f"Damaged: {errors} decode error(s)." if errors else "Whole file decoded without errors.")
        else:
            outcome = score(metrics, rec)
            values.update(check_version=CHECK_VERSION, score=outcome["score"], grade=outcome["grade"], findings_json=json.dumps(outcome["findings"]), metrics_json=json.dumps(metrics),
                          samples=len(metrics.get("slices") or []), finished_at=now,
                          message=(f"Score {outcome['score']:g} ({outcome['grade']})." if outcome["score"] is not None else "No result."))
    elif target in ("failed", "cancelled"):
        values.update(message=(job["error"] or job["review_reason"] or job["message"] or ("Cancelled." if target == "cancelled" else "The worker could not check the file."))[:400], finished_at=now)
    elif target == "running":
        values["message"] = job.get("message") or "A worker is checking the file."
    conn.execute(f"UPDATE quality_check SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", [*values.values(), row["id"]])
    return True


def rescore_stale(conn):
    """Scores checks made under older rules again from the numbers the worker sent, so a change to the rules needs no new check. Returns how many were updated."""
    done = 0
    for row in conn.execute("SELECT q.id, q.metrics_json, q.size_bytes, q.mtime, f.* FROM quality_check q JOIN library_file f ON f.id = q.library_file_id "
                            "WHERE q.status = 'done' AND q.mode = 'quality' AND q.check_version < ? AND q.metrics_json IS NOT NULL", (CHECK_VERSION,)).fetchall():
        rec = dict(row)
        metrics = _json(rec.get("metrics_json"), {})
        if not metrics.get("slices"):
            continue
        outcome = score(metrics, rec)
        conn.execute("UPDATE quality_check SET check_version = ?, score = ?, grade = ?, findings_json = ?, message = ?, updated_at = ? WHERE id = ?",
                     (CHECK_VERSION, outcome["score"], outcome["grade"], json.dumps(outcome["findings"]),
                      f"Score {outcome['score']:g} ({outcome['grade']})." if outcome["score"] is not None else "No result.", db.now(), row["id"]))
        done += 1
    return done


def stop_all(conn, by="someone"):
    """Cancels every check that is waiting or running. A waiting one stops at once; a running one stops at its next report. Returns how many were told to stop."""
    stopped = 0
    for r in conn.execute("SELECT job_id FROM quality_check WHERE status IN ('queued', 'running') AND job_id IS NOT NULL").fetchall():
        try:
            jobs.cancel(conn, r["job_id"], by=by)
            stopped += 1
        except jobs.JobError:
            pass
    sync_active(conn)
    return stopped


def clear_results(conn, root=None):
    """Removes the stored scores, flags and 'accept as is' marks of finished checks, for one share or all. The Library list is not touched.
    Checks that are waiting or running are left alone (use stop_all). Returns how many checks were removed."""
    where, params = "status NOT IN ('queued', 'running')", []
    if root:
        where += " AND library_file_id IN (SELECT id FROM library_file WHERE root_name = ?)"
        params.append(root)
    removed = conn.execute(f"DELETE FROM quality_check WHERE {where}", params).rowcount
    if root:
        conn.execute("DELETE FROM quality_accept WHERE library_file_id IN (SELECT id FROM library_file WHERE root_name = ?)", (root,))
    else:
        conn.execute("DELETE FROM quality_accept")
    return removed


def sync_active(conn):
    for r in conn.execute("SELECT job_id FROM quality_check WHERE status IN ('queued', 'running') AND job_id IS NOT NULL").fetchall():
        sync(conn, r["job_id"])
    release_waiting(conn)


def accept(conn, file_id, by="someone"):
    rec = _record(conn, file_id)
    if rec is None:
        raise QualityError("That file is not in the Library.")
    conn.execute("INSERT INTO quality_accept (library_file_id, size_bytes, mtime, accepted_by) VALUES (?, ?, ?, ?) ON CONFLICT(library_file_id) DO UPDATE SET "
                 "size_bytes = excluded.size_bytes, mtime = excluded.mtime, accepted_by = excluded.accepted_by, accepted_at = ?", (file_id, rec["size_bytes"], rec["mtime"], by, db.now()))


def unaccept(conn, file_id):
    conn.execute("DELETE FROM quality_accept WHERE library_file_id = ?", (file_id,))


# ---------------------------------------------------------------- the background pass
def due_files(conn, limit, mode="quality"):
    """Files with no current check of this kind and none waiting, in the order the setting gives."""
    order = "DESC" if newest_first(conn) else "ASC"
    wanted, floor = int(limit), min_height(conn)
    rows = conn.execute(f"""
        SELECT f.* FROM library_file f
        WHERE f.missing_since IS NULL AND f.analysis_status = 'completed' AND f.duration_seconds > 0 AND f.video_codec IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM quality_check q WHERE q.library_file_id = f.id AND q.mode = :mode AND q.status IN ('queued', 'running'))
          AND NOT EXISTS (SELECT 1 FROM quality_check q WHERE q.library_file_id = f.id AND q.mode = :mode AND q.status = 'done' AND q.size_bytes = f.size_bytes AND q.mtime = f.mtime AND q.check_version = :version)
          AND NOT EXISTS (SELECT 1 FROM quality_check q WHERE q.library_file_id = f.id AND q.mode = :mode AND q.status = 'failed' AND q.size_bytes = f.size_bytes AND q.mtime = f.mtime AND q.created_at > datetime('now', '-3 days'))
        ORDER BY f.first_seen_at {order}, f.id {order}""", {"mode": mode, "version": CHECK_VERSION}).fetchall()
    out = []
    for r in rows:                                   # files under the minimum resolution are not checked by the background passes: they need a bigger copy, not a score
        rec = dict(r)
        if mode == "quality" and below_minimum(rec, floor):
            continue
        out.append(rec)
        if len(out) >= wanted:
            break
    return out


def waiting_cleanup(conn, mode="quality"):
    return conn.execute("SELECT COUNT(*) FROM quality_check WHERE source = 'cleanup' AND mode = ? AND status IN ('queued', 'running')", (mode,)).fetchone()[0]


def someone_watching(client=None):
    """True when anyone is playing something in Jellyfin right now. False when Jellyfin cannot be asked: a missing answer must not stop the pass for good."""
    try:
        if client is None:
            if settings.get("disable_jellyfin_connection") == "1" or not settings.get("jellyfin_url"):
                return False
            from synchub.clients.jellyfin import JellyfinClient
            client = JellyfinClient()
        from synchub import sessions
        return any(r["playing"] for r in sessions.live(client))
    except Exception as exc:
        logger.info(f"[quality] could not ask Jellyfin who is watching: {exc}")
        return False


def library_step(client=None):
    """One turn of the Library clean-up passes: queues a few unchecked files (and, when its own pass is on, a few whole-file damage checks), but not while anyone is
    watching, not while paused and not when a pass is off. Returns how many were queued."""
    return _pass("quality", client) + _pass("damage", client)


def _pass(mode, client=None):
    with db.connection() as conn:
        if paused(conn) or not (library_pass(conn) if mode == "quality" else damage_pass(conn)):
            return 0
        room = (CLEANUP_IN_FLIGHT if mode == "quality" else DAMAGE_IN_FLIGHT) - waiting_cleanup(conn, mode)
    if room <= 0 or someone_watching(client):
        return 0
    queued = 0
    with db.connection() as conn:
        for rec in due_files(conn, room, mode):
            try:
                request(conn, rec["id"], by="clean-up", source="cleanup", mode=mode)
                queued += 1
            except (QualityError, jobs.JobError) as exc:
                logger.info(f"[quality] skipped {rec['file_name']}: {exc}")
    return queued


# ---------------------------------------------------------------- check first, then convert
def check_first(conn=None):
    """Does an approved conversion wait for the file's quality check? On unless switched off in Options."""
    return settings.get("quality_check_first", conn=conn) != "0"


def wait_for_check(conn, file_id, by="someone"):
    """Asks for the checks a conversion waits for and records the wait. Returns True when the conversion must wait, False when the file is already checked (or cannot be)."""
    rec = _record(conn, file_id)
    if rec is None or not check_first(conn):
        return False
    if conn.execute("SELECT 1 FROM convert_wait WHERE library_file_id = ?", (file_id,)).fetchone():
        return True
    need_quality = not is_current(latest(conn, file_id, done_only=True), rec)
    need_damage = damage_pass(conn) and not is_current(latest(conn, file_id, done_only=True, mode="damage"), rec)
    if not (need_quality or need_damage):
        return False
    try:
        q = request(conn, file_id, by=by, source="convert") if need_quality else None
        d = request(conn, file_id, by=by, source="convert", mode="damage") if need_damage else None
    except (QualityError, jobs.JobError) as exc:
        logger.info(f"[quality] no check before converting file {file_id}: {exc}")
        return False
    conn.execute("INSERT INTO convert_wait (library_file_id, created_at, requested_by, quality_check_id, damage_check_id) VALUES (?, ?, ?, ?, ?)", (file_id, db.now(), by, q, d))
    return True


def waiting_to_convert(conn, file_id):
    return conn.execute("SELECT * FROM convert_wait WHERE library_file_id = ?", (file_id,)).fetchone() is not None


def release_waiting(conn, cfg=None):
    """Gives a conversion job to every waiting file whose checks are done and passed; holds the low quality and damaged ones; sends back the ones whose check
    did not finish. Returns (queued, held)."""
    from synchub.library import migrate, scan, store
    cfg = cfg or scan.config(conn)
    queued = held = 0
    for w in conn.execute("SELECT * FROM convert_wait").fetchall():
        fid, ids = w["library_file_id"], [i for i in (w["quality_check_id"], w["damage_check_id"]) if i]
        rows = [get(conn, i) for i in ids]
        if any(r and r["status"] in LIVE for r in rows):
            continue
        conn.execute("DELETE FROM convert_wait WHERE library_file_id = ?", (fid,))
        rec = _record(conn, fid)
        if rec is None or rec["approval_status"] != v.APPROVAL_APPROVED or rec["migration_status"] != v.MIGRATION_PENDING:
            continue
        if any(r and r["status"] != "done" for r in rows):
            store.set_approval(conn, fid, v.APPROVAL_PENDING, "quality check", note="The quality check did not finish, so the file was not converted. Approve it again.")
        elif is_held(conn, fid):
            store.set_approval(conn, fid, v.APPROVAL_PENDING, "quality check", note="Held from conversion: low quality or damaged. It can still be renamed and moved.")
            held += 1
        else:
            try:
                queued += 1 if migrate.queue(conn, fid, cfg, w["requested_by"] or "quality check", checked=True) is not None else 0
            except ValueError as exc:
                logger.info(f"[quality] file {fid} not queued after its check: {exc}")
    return queued, held


# ---------------------------------------------------------------- lists and the movie page
def low_quality(conn, limit=50, match=None, root=None, offset=0):
    """Files flagged low quality, worst first: under the minimum resolution, or a current check below the flag threshold, and not accepted. Each row carries `findings`.
    `match` (text in the path) and `root` (a share) narrow the list; `offset` pages through it."""
    out = _flagged(conn, match, root)
    out = out[int(offset):int(offset) + int(limit)]
    for d in out:
        d["request"] = latest_request(conn, d["id"])
    return out


def is_held(conn, file_id):
    """Is this one file held from conversion (flagged low quality or damaged, and not accepted)?"""
    return bool(_flagged(conn, file_id=file_id))


def held_ids(conn):
    """The ids of the files held from conversion: flagged low quality (or damaged) and not accepted. Such a file may still be renamed and moved, never converted."""
    from synchub.library import cache
    return cache.get(conn, "held_ids", lambda: frozenset(d["id"] for d in _flagged(conn)))


def _flagged(conn, match=None, root=None, file_id=None):
    """The flagged files, narrowed by `match` and `root`. The whole list is worked out once and kept until a file, a check, an accept or a setting changes."""
    if file_id is not None:
        return _flagged_rows(conn, file_id=file_id)
    from synchub.library import cache
    needle = (match or "").lower()
    everything = cache.get(conn, "flagged", lambda: _flagged_rows(conn))
    return [dict(d) for d in everything
            if not (root and d.get("root_name") != root) and not (needle and needle not in (d["source_path"] + " " + d["file_name"]).lower())]


def _flagged_rows(conn, file_id=None):
    below, wanted = flag_below(conn), min_height(conn)
    rows = conn.execute("""
        SELECT f.*, q.id AS check_id, q.score AS q_score, q.findings_json AS q_findings, q.finished_at AS q_finished, q.check_version AS q_version, q.size_bytes AS q_size, q.mtime AS q_mtime,
               (SELECT 1 FROM quality_accept a WHERE a.library_file_id = f.id AND a.size_bytes = f.size_bytes AND a.mtime = f.mtime) AS accepted,
               (SELECT z.findings_json FROM quality_check z WHERE z.library_file_id = f.id AND z.mode = 'damage' AND z.status = 'done' AND z.size_bytes = f.size_bytes AND z.mtime = f.mtime
                ORDER BY z.id DESC LIMIT 1) AS damage_findings
        FROM library_file f
        LEFT JOIN quality_check q ON q.id = (SELECT MAX(id) FROM quality_check WHERE library_file_id = f.id AND mode = 'quality' AND status = 'done')
        WHERE f.missing_since IS NULL AND f.analysis_status = 'completed'""" + (" AND f.id = ?" if file_id is not None else ""),
        (file_id,) if file_id is not None else ()).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.pop("accepted"):
            continue
        findings = _json(d.pop("q_findings"), [])
        current = d["check_id"] is not None and d["q_size"] == d["size_bytes"] and d["q_mtime"] == d["mtime"] and d["q_version"] == CHECK_VERSION
        scored_low = bool(current and d["q_score"] is not None and d["q_score"] < below)
        small = resolution_finding(d, wanted)
        damaged = _json(d.pop("damage_findings"), [])
        if not (scored_low or small or damaged):
            continue
        d["findings"] = damaged + ([small] if small else []) + (findings if current else [])
        d["sort_score"] = d["q_score"] if current and d["q_score"] is not None else (0 if small or damaged else 100)
        out.append(d)
    out.sort(key=lambda d: (d["sort_score"], d["id"]))
    return out


TRACK_WORDS = {"searching": "Radarr is searching", "waiting": "Waiting: Radarr has not started a download yet", "queued": "Queued in the download client", "downloading": "Downloading",
               "importing": "Importing", "imported": "Imported, check it in Jellyfin", "problem": "Problem", "nothing_found": "Nothing found"}
TRACK_DONE = ("imported", "nothing_found")


def track_label(row):
    """One line for a better-copy request's progress ('Downloading 42%, 12 min left'), or '' when it is not tracked."""
    state = (row or {}).get("track_state")
    if not state:
        return ""
    text = TRACK_WORDS.get(state, state)
    if state == "downloading" and row.get("track_percent") is not None:
        text += f" {row['track_percent']}%" + (f", {row['track_eta']} left" if row.get("track_eta") else "")
    if row.get("track_detail") and state in ("problem", "waiting", "nothing_found"):
        text += f": {row['track_detail']}"
    return text


def latest_request(conn, file_id):
    row = conn.execute("SELECT * FROM quality_request WHERE library_file_id = ? ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["track_label"] = track_label(d)
    return d


def low_count(conn):
    return len(held_ids(conn))


def counts(conn):
    row = conn.execute("SELECT SUM(status = 'queued') AS queued, SUM(status = 'running') AS running FROM quality_check").fetchone()
    return {"waiting": row["queued"] or 0, "running": row["running"] or 0, "low": low_count(conn)}


def folder_of(path):
    return os.path.dirname(v.clean_relative(path)).casefold()


def _stem(name):
    return os.path.splitext(os.path.basename(name))[0].casefold()


def _title_key(text):
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def files_for_movie(conn, media_id):
    """The Library files of a film (see `find_movie_files`)."""
    return find_movie_files(conn, media_id)[0]


def find_movie_files(conn, media_id):
    """([Library files of a film], [Jellyfin paths no storage mapping covers]). Every Jellyfin item linked to the film gives a main file's path; SyncHub turns each into
    a share and a path with the share's Jellyfin mappings (storage page). Its other files are the ones in the same folder named after it (`Heat (1995) - 1080p.mkv`
    beside `Heat (1995).mkv`), or every file in the folder when the folder is the film's own (its name starts with the film's title). A loose file in a shared
    folder like `Old Movies/` matches only itself. Any other Library file whose cleaned name starts with the film's title and year is found by name too
    (`found_by` is 'name' on those rows), wherever it sits."""
    from synchub import storage
    items = conn.execute("""SELECT t.path, m.title, m.year FROM link l JOIN src_jellyfin_item t ON t.item_id = l.source_key JOIN media m ON m.id = l.entity_id
                            WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = ? AND t.removed_at IS NULL AND t.path IS NOT NULL
                            ORDER BY t.item_id""", (media_id,)).fetchall()
    roots, cache, found, unmapped = storage.list_all(conn), {}, {}, []
    for item in items:
        mapped = storage.from_app_path(roots, item["path"], "jellyfin")
        if mapped is None:
            unmapped.append(item["path"])
            continue
        root, rel = mapped
        if root not in cache:
            cache[root] = [dict(r) for r in conn.execute("SELECT * FROM library_file WHERE root_name = ? AND missing_since IS NULL ORDER BY size_bytes DESC", (root,))]
        folder, stem = folder_of(rel), _stem(rel)
        own_folder = bool(folder) and _title_key(item["title"]) and _title_key(os.path.basename(folder)).startswith(_title_key(item["title"]))
        for r in cache[root]:
            if folder_of(r["relative_path"]) != folder:
                continue
            name = _stem(r["relative_path"])
            if v.clean_relative(r["relative_path"]).casefold() == v.clean_relative(rel).casefold() or name == stem or name.startswith(stem + " - ") or own_folder:
                found.setdefault(r["id"], r)
    media = conn.execute("SELECT title, year FROM media WHERE id = ?", (media_id,)).fetchone()
    wanted = _title_key(media["title"]) + str(media["year"] or "") if media and media["year"] else ""
    if wanted:
        for row in conn.execute("SELECT * FROM library_file WHERE missing_since IS NULL AND media_type LIKE 'Movie%' ORDER BY size_bytes DESC"):
            r = dict(row)
            if r["id"] not in found and _title_key(_stem(r["relative_path"])).startswith(wanted):
                found[r["id"]] = dict(r, found_by="name")
    return sorted(found.values(), key=lambda r: -(r["size_bytes"] or 0)), unmapped


def role_of(conn, rec):
    """What a file is to the film: 'original' (a copy or conversion of it was made in another share), 'converted' (it is that copy), 'remote' (a 1080p extra version) or None."""
    dest = rec.get("migration_destination_path") or ""
    if rec.get("migration_status") == v.MIGRATION_COMPLETED and ":" in dest and dest.split(":", 1)[0] != rec.get("root_name") and rec.get("migrated_how") in ("copy", "convert"):
        return "original"
    if extra_versions.is_version_file(rec["file_name"]):
        return "remote"
    here = f"{rec.get('root_name')}:{rec.get('relative_path')}"
    if conn.execute("SELECT 1 FROM library_file WHERE lower(migration_destination_path) = lower(?) AND migration_status = ? AND id != ? LIMIT 1", (here, v.MIGRATION_COMPLETED, rec["id"])).fetchone():
        return "converted"
    return None


def describe_file(conn, rec):
    """One file as the movie page shows it."""
    from synchub import storage
    from synchub.library import hide
    width, height, fps, kbps, codec = _facts(rec)
    role = role_of(conn, rec)
    root = storage.by_name(conn, rec.get("root_name") or "")
    folder = folder_of(rec.get("relative_path") or "")
    info = _json(rec.get("video_info_json"), {})
    return {"id": rec["id"], "name": rec["file_name"], "size_bytes": rec["size_bytes"], "container": rec.get("container"), "codec": codec or None, "width": width, "height": height,
            "fps": fps, "video_kbps": kbps, "bits_per_pixel": bits_per_pixel(rec), "bit_depth": info.get("bit_depth"), "picture": extra_versions.picture_label(rec),
            "audio": rec.get("audio_codecs"), "audio_languages": rec.get("audio_languages"), "subtitles": rec.get("subtitle_languages"), "integrity": rec.get("integrity_status"),
            "is_extra": extra_versions.is_version_file(rec["file_name"]), "found_by": rec.get("found_by"), "root": rec.get("root_name"), "path": rec.get("relative_path"), "analysed": rec["analysis_status"] == v.ANALYSIS_COMPLETED, "quality": state_for(conn, rec),
            "role": role, "hidden": bool(rec.get("source_hidden_at")), "hide_message": rec.get("source_hide_message"), "windows_path": hide.windows_path(root, folder),
            "unix_path": f"{rec.get('root_name')}:{folder}" if folder else None, "can_hide": role == "original" and len(str(rec.get("relative_path") or "").split("/")) >= 3}


def copies(conn, media_id):
    """For the movie page: every file of the film, the full quality copy (the best file) and any smaller remote copies. `missing_remote` is true when the full copy is above
    1080p, extra versions are switched on and no smaller copy exists."""
    found, unmapped = find_movie_files(conn, media_id)
    files = [describe_file(conn, r) for r in found]
    if not files:
        return {"files": [], "main": None, "remote": [], "missing_remote": False, "full_label": None, "unmapped": unmapped}
    ranked = sorted(files, key=lambda f: ((f["height"] or 0), f["role"] != "original", f["size_bytes"] or 0), reverse=True)   # the converted copy outranks its own original
    main = next((f for f in ranked if not f["is_extra"]), ranked[0])
    remote = [f for f in ranked if f is not main and (f["is_extra"] or (f["height"] or 0) <= 1080) and (f["size_bytes"] or 0) < (main["size_bytes"] or 0)]
    missing = bool((main["height"] or 0) > 1080 and not remote and extra_versions.load(conn).on)
    label = f"{main['height']}p {main['picture']}" if main["height"] else main["picture"]
    return {"files": ranked, "main": main, "remote": remote, "missing_remote": missing, "full_label": label, "unmapped": unmapped}
