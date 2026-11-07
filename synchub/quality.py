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
CHECK_VERSION = 1
GRADES = (("good", 80), ("ok", 65), ("poor", 45), ("bad", 0))
LIVE = ("queued", "running")
JOB_STATUS = {"queued": "queued", "running": "running", "done": "done", "failed": "failed", "needs_review": "failed", "cancelled": "cancelled"}
SOURCES = ("manual", "download", "cleanup")
PRIORITY = {"manual": 70, "download": 110, "cleanup": 5}
CLEANUP_IN_FLIGHT = 3                 # the background pass never has more than this many checks waiting or running

# How efficient a codec is compared with H.264, so the bits-per-pixel limits mean the same thing for every file.
CODEC_EFFICIENCY = {"hevc": 1.6, "h265": 1.6, "av1": 2.0, "vp9": 1.6, "h264": 1.0, "avc": 1.0, "vc1": 0.9, "mpeg4": 0.7, "msmpeg4": 0.6, "mpeg2video": 0.5, "mpeg1video": 0.4}
BPP_LIMITS = ((0.030, 45, "far too few bits for the picture"), (0.045, 28, "few bits for the picture"), (0.070, 10, "a little low for the picture"))
BLOCK_LIMITS = ((30.0, 25), (20.0, 12))
BLUR_LIMITS = ((14.0, 20), (10.0, 10))         # per 1080 lines of picture height


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


def on_download(conn=None):
    return settings.get("quality_on_download", conn=conn) == "flag"


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
        for limit, points, words in BPP_LIMITS:
            if bpp < limit:
                add("low_bitrate", "bad" if points >= 28 else "warn", points, f"{words.capitalize()}: {bpp:.3f} bits per pixel (H.264 equivalent). Dark scenes and grain will look blocky.")
                break
    block = _median(s.get("block") for s in slices)
    if block is not None:
        for limit, points in BLOCK_LIMITS:
            if block > limit:
                add("blocky", "warn", points, f"Visible blockiness (advisory): block score {block:.1f}, typical clean films stay well below {BLOCK_LIMITS[1][0]:g}.")
                break
    _, height, _, _, _ = _facts(rec)
    blur = _median(s.get("blur") for s in slices)
    if blur is not None and height:
        per_1080 = blur * 1080 / height
        for limit, points in BLUR_LIMITS:
            if per_1080 > limit:
                add("blurry", "warn", points, f"Soft or blurry picture (advisory): blur score {per_1080:.1f} per 1080 lines. Some films are shot soft on purpose.")
                break
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


def latest(conn, file_id, done_only=False):
    sql = "SELECT * FROM quality_check WHERE library_file_id = ?" + (" AND status = 'done'" if done_only else "") + " ORDER BY id DESC LIMIT 1"
    return _present(conn.execute(sql, (file_id,)).fetchone())


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
    """What to show for one file: {'check', 'current', 'low', 'accepted', 'label'}. `low` is only true for a current check below the flag threshold that nobody accepted."""
    check = check if check is not None else latest(conn, rec["id"])
    done = check if check and check["status"] == "done" else (latest(conn, rec["id"], done_only=True) if check else None)
    current = is_current(done, rec)
    accepted = is_accepted(conn, rec)
    low = bool(current and done["score"] is not None and done["score"] < flag_below(conn) and not accepted)
    if check and check["status"] in LIVE:
        label = "Checking" if check["status"] == "running" else "Waiting for a worker"
    elif done is None:
        label = "Not checked"
    elif done["score"] is None:
        label = "No result"
    else:
        label = ("Low quality" if low else {"good": "Good", "ok": "OK", "poor": "Poor", "bad": "Bad"}.get(done["grade"], "")) + ("" if current else " (old)")
        if accepted and done["score"] < flag_below(conn):
            label = "Accepted as is"
    return {"check": check, "result": done, "current": current, "low": low, "accepted": accepted, "label": label}


# ---------------------------------------------------------------- asking for a check
def request(conn, file_id, by="someone", source="manual", now=None):
    """Queues a quality check of one library file. Returns the check id, or the id of the one already waiting. Raises QualityError for a sentence on the page."""
    rec = _record(conn, file_id)
    if rec is None:
        raise QualityError("That file is not in the Library.")
    if source not in SOURCES:
        source = "manual"
    if rec.get("missing_since"):
        raise QualityError("This file is missing from its share, so it cannot be checked.")
    if rec["analysis_status"] != v.ANALYSIS_COMPLETED or not rec.get("duration_seconds"):
        raise QualityError("The file has not been analysed yet. Scan its folder first.")
    waiting = conn.execute("SELECT id FROM quality_check WHERE library_file_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    if waiting:
        return waiting["id"]
    now = now or db.now()
    check_id = conn.execute("INSERT INTO quality_check (library_file_id, size_bytes, mtime, source, status, requested_by, message, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                            (file_id, rec["size_bytes"], rec["mtime"], source, by, "Waiting for a worker.", now, now)).lastrowid
    title = rec.get("recommended_name") or rec["file_name"]
    plan = {"op": "quality", "needs": ["quality"], "check_id": check_id, "samples": samples(conn), "seconds": slice_seconds(conn), "duration": rec["duration_seconds"],
            "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "seed": check_id * 7919 + int(rec["size_bytes"] or 0) % 10007}
    job_id = jobs.create(conn, "scan", title=f"Quality check: {title}", ref=(REF, check_id), source=(rec["root_name"], rec["relative_path"]), plan=plan,
                         priority=PRIORITY[source], max_attempts=2, now=now)
    conn.execute("UPDATE quality_check SET job_id = ? WHERE id = ?", (job_id, check_id))
    logger.info(f"[quality] check {check_id} of {title} queued by {by} ({source})")
    return check_id


def request_for_import(conn, rec, now=None):
    """Called when a new Radarr/Sonarr import has been analysed. Does nothing when the setting is off or the file already has a current check. Never raises."""
    try:
        if not on_download(conn) or is_current(latest(conn, rec["id"], done_only=True), rec):
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


def sync_active(conn):
    for r in conn.execute("SELECT job_id FROM quality_check WHERE status IN ('queued', 'running') AND job_id IS NOT NULL").fetchall():
        sync(conn, r["job_id"])


def accept(conn, file_id, by="someone"):
    rec = _record(conn, file_id)
    if rec is None:
        raise QualityError("That file is not in the Library.")
    conn.execute("INSERT INTO quality_accept (library_file_id, size_bytes, mtime, accepted_by) VALUES (?, ?, ?, ?) ON CONFLICT(library_file_id) DO UPDATE SET "
                 "size_bytes = excluded.size_bytes, mtime = excluded.mtime, accepted_by = excluded.accepted_by, accepted_at = ?", (file_id, rec["size_bytes"], rec["mtime"], by, db.now()))


def unaccept(conn, file_id):
    conn.execute("DELETE FROM quality_accept WHERE library_file_id = ?", (file_id,))


# ---------------------------------------------------------------- the background pass
def due_files(conn, limit):
    """Files with no current check and none waiting, in the order the setting gives."""
    order = "DESC" if newest_first(conn) else "ASC"
    rows = conn.execute(f"""
        SELECT f.* FROM library_file f
        WHERE f.missing_since IS NULL AND f.analysis_status = 'completed' AND f.duration_seconds > 0 AND f.video_codec IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM quality_check q WHERE q.library_file_id = f.id AND q.status IN ('queued', 'running'))
          AND NOT EXISTS (SELECT 1 FROM quality_check q WHERE q.library_file_id = f.id AND q.status = 'done' AND q.size_bytes = f.size_bytes AND q.mtime = f.mtime AND q.check_version = ?)
          AND NOT EXISTS (SELECT 1 FROM quality_check q WHERE q.library_file_id = f.id AND q.status = 'failed' AND q.size_bytes = f.size_bytes AND q.mtime = f.mtime AND q.created_at > datetime('now', '-3 days'))
        ORDER BY f.first_seen_at {order}, f.id {order} LIMIT ?""", (CHECK_VERSION, int(limit))).fetchall()
    return [dict(r) for r in rows]


def waiting_cleanup(conn):
    return conn.execute("SELECT COUNT(*) FROM quality_check WHERE source = 'cleanup' AND status IN ('queued', 'running')").fetchone()[0]


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
    """One turn of the Library clean-up pass: queues a few unchecked files, but not while anyone is watching and not when the pass is off. Returns how many were queued."""
    with db.connection() as conn:
        if not library_pass(conn):
            return 0
        room = CLEANUP_IN_FLIGHT - waiting_cleanup(conn)
    if room <= 0 or someone_watching(client):
        return 0
    queued = 0
    with db.connection() as conn:
        for rec in due_files(conn, room):
            try:
                request(conn, rec["id"], by="clean-up", source="cleanup")
                queued += 1
            except (QualityError, jobs.JobError) as exc:
                logger.info(f"[quality] skipped {rec['file_name']}: {exc}")
    return queued


# ---------------------------------------------------------------- lists and the movie page
def low_quality(conn, limit=50):
    """Files whose current check is below the flag threshold and that nobody accepted, worst first."""
    below = flag_below(conn)
    rows = conn.execute("""
        SELECT f.*, q.id AS check_id, q.score AS q_score, q.grade AS q_grade, q.findings_json AS q_findings, q.finished_at AS q_finished
        FROM quality_check q JOIN library_file f ON f.id = q.library_file_id
        WHERE q.status = 'done' AND q.id = (SELECT MAX(id) FROM quality_check WHERE library_file_id = f.id AND status = 'done')
          AND q.check_version = ? AND q.size_bytes = f.size_bytes AND q.mtime = f.mtime AND q.score < ? AND f.missing_since IS NULL
          AND NOT EXISTS (SELECT 1 FROM quality_accept a WHERE a.library_file_id = f.id AND a.size_bytes = f.size_bytes AND a.mtime = f.mtime)
        ORDER BY q.score ASC, f.id LIMIT ?""", (CHECK_VERSION, below, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["findings"] = _json(d.pop("q_findings"), [])
        out.append(d)
    return out


def low_count(conn):
    return len(low_quality(conn, limit=100000))


def counts(conn):
    row = conn.execute("SELECT SUM(status = 'queued') AS queued, SUM(status = 'running') AS running FROM quality_check").fetchone()
    return {"waiting": row["queued"] or 0, "running": row["running"] or 0, "low": low_count(conn)}


def folder_of(path):
    return os.path.dirname(v.clean_relative(path)).casefold()


def files_for_movie(conn, media_id):
    """The Library files of a film: every file in the folder of the Jellyfin item, found through the share's Jellyfin path. [] when the film is not in Jellyfin yet
    or the share has no Jellyfin path on the Storage page."""
    item = conn.execute("""SELECT t.path FROM link l JOIN src_jellyfin_item t ON t.item_id = l.source_key
                           WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = ? AND t.removed_at IS NULL AND t.path IS NOT NULL
                           ORDER BY t.item_id LIMIT 1""", (media_id,)).fetchone()
    if not item:
        return []
    path = str(item["path"]).replace("\\", "/")
    best = None
    for root in conn.execute("SELECT name, jellyfin_path FROM storage_root").fetchall():
        base = (root["jellyfin_path"] or "").rstrip("/")
        if base and path.startswith(base + "/") and (best is None or len(base) > len(best[1])):
            best = (root["name"], base)
    if best is None:
        return []
    folder = folder_of(path[len(best[1]) + 1:])
    if not folder:
        return []                     # a file at the top of a share has no folder of its own; its neighbours are other films
    rows = conn.execute("SELECT * FROM library_file WHERE root_name = ? AND missing_since IS NULL ORDER BY size_bytes DESC", (best[0],)).fetchall()
    return [dict(r) for r in rows if folder_of(r["relative_path"]) == folder]


def describe_file(conn, rec):
    """One file as the movie page shows it."""
    width, height, fps, kbps, codec = _facts(rec)
    info = _json(rec.get("video_info_json"), {})
    return {"id": rec["id"], "name": rec["file_name"], "size_bytes": rec["size_bytes"], "container": rec.get("container"), "codec": codec or None, "width": width, "height": height,
            "fps": fps, "video_kbps": kbps, "bits_per_pixel": bits_per_pixel(rec), "bit_depth": info.get("bit_depth"), "picture": extra_versions.picture_label(rec),
            "audio": rec.get("audio_codecs"), "audio_languages": rec.get("audio_languages"), "subtitles": rec.get("subtitle_languages"), "integrity": rec.get("integrity_status"),
            "is_extra": extra_versions.is_version_file(rec["file_name"]), "analysed": rec["analysis_status"] == v.ANALYSIS_COMPLETED, "quality": state_for(conn, rec)}


def copies(conn, media_id):
    """For the movie page: every file of the film, the full quality copy (the best file) and any smaller remote copies. `missing_remote` is true when the full copy is above
    1080p, extra versions are switched on and no smaller copy exists."""
    files = [describe_file(conn, r) for r in files_for_movie(conn, media_id)]
    if not files:
        return {"files": [], "main": None, "remote": [], "missing_remote": False, "full_label": None}
    ranked = sorted(files, key=lambda f: ((f["height"] or 0), f["size_bytes"] or 0), reverse=True)
    main = next((f for f in ranked if not f["is_extra"]), ranked[0])
    remote = [f for f in ranked if f is not main and (f["is_extra"] or (f["height"] or 0) <= 1080) and (f["size_bytes"] or 0) < (main["size_bytes"] or 0)]
    missing = bool((main["height"] or 0) > 1080 and not remote and extra_versions.load(conn).on)
    label = f"{main['height']}p {main['picture']}" if main["height"] else main["picture"]
    return {"files": ranked, "main": main, "remote": remote, "missing_remote": missing, "full_label": label}
