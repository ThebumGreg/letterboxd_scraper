"""The download pipeline: a file Radarr or Sonarr just imported is looked at by a worker and, when the rules call for it, replaced in place.

An import webhook leaves one `download_import` row. A worker scans the file's folder (the Library's scan, so the analysis and the track rules
are the Library's own), SyncHub decides from the analysis whether anything should change, and a `download` job converts the file on the worker and
swaps the result in under the same base name (see the `replace` plan in the agent). The original stays on the worker for `originals_retention_days`.
When the job is done, Radarr or Sonarr is asked to rescan the title and Jellyfin to scan its library.

`download_pipeline` says how far it goes on its own: `off` records nothing, `review` stops at "awaiting approval" for every file, `auto` converts
files whose plan is safe (tracks removed or a lossless remux with English audio kept) and stops for the rest.
"""
import json
import logging
import os
import posixpath

from synchub import db, jobs, settings, storage
from synchub.library import scan, store
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

REF = "download_import"
EVENTS = {"radarr": ("Download",), "sonarr": ("Download", "ImportComplete")}
LIVE = ("pending", "scanning", "awaiting_approval", "queued", "converting")
JOB_STATUS = {"queued": "queued", "running": "converting", "needs_review": "blocked", "done": "replaced", "failed": "failed", "cancelled": "skipped", "skipped": "skipped"}
DUPLICATE_WINDOW_SECONDS = 600           # the same file reported twice (Sonarr sends both Download and ImportComplete)
WORK_MARGIN_BYTES = 2 * 1024 ** 3
PIPELINE_MODES = ("off", "review", "auto")


def mode(conn=None):
    value = settings.get("download_pipeline", conn=conn)
    return value if value in PIPELINE_MODES else "off"


def retention_days(conn=None):
    try:
        return max(0, int(settings.get("originals_retention_days", conn=conn)))
    except ValueError:
        return 30


# ---------------------------------------------------------------- reading the webhook
def files_in(service, payload):
    """[{'path', 'size', 'item_id', 'title'}] for the files an import event says arrived. `path` is as Radarr/Sonarr sees it."""
    payload = payload if isinstance(payload, dict) else {}
    owner = payload.get("movie") if service == "radarr" else payload.get("series")
    owner = owner if isinstance(owner, dict) else {}
    folder = str(owner.get("folderPath") or owner.get("path") or "")
    found = payload.get("episodeFiles") if service == "sonarr" and isinstance(payload.get("episodeFiles"), list) else None
    entries = found or [payload.get("movieFile" if service == "radarr" else "episodeFile")]
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path and entry.get("relativePath") and folder:
            path = posixpath.join(folder, str(entry["relativePath"]))
        if path:
            out.append({"path": path, "size": entry.get("size"), "item_id": owner.get("id"), "title": owner.get("title")})
    return out


def locate(conn, service, path):
    """(root name, relative path) for a path as Radarr/Sonarr sees it, using the paths set on the Storage page. None when no share covers it."""
    path = str(path).replace("\\", "/")
    best = None
    for root in storage.list_all(conn):
        base = (root.get(f"{service}_path") or "").rstrip("/")
        if base and path.startswith(base + "/") and (best is None or len(base) > len(best[1])):
            best = (root["name"], base)
    if best is None:
        return None
    try:
        return best[0], storage.clean_rel(path[len(best[1]) + 1:])
    except storage.StorageError:
        return None


def record_event(conn, service, event, payload, now=None):
    """Notes the files an import event brought in. Returns the ids of the rows made (none when the pipeline is off or the event is not an import)."""
    if mode(conn) == "off" or event not in EVENTS.get(service, ()):
        return []
    now, made = now or db.now(), []
    for item in files_in(service, payload):
        located = locate(conn, service, item["path"])
        if located is None:
            made.append(_insert(conn, service, event, item, None, None, "blocked",
                                f"No share has a {service.title()} path that covers {item['path']}. Set it on the Storage page, then press Check again.", now))
            continue
        root, rel = located
        key = v.record_key(root, rel)
        if _duplicate(conn, key, item.get("size"), now):
            continue
        status, message = "pending", "Waiting for a worker to look at the file."
        if "/" not in rel:
            status, message = "blocked", "The file sits at the top of its share, so a scan of its folder would read the whole share. Move it into a folder first."
        made.append(_insert(conn, service, event, item, root, rel, status, message, now))
    return made


def _insert(conn, service, event, item, root, rel, status, message, now):
    key = v.record_key(root, rel) if root else f"unmapped:{item['path']}"
    try:
        size = int(item["size"]) if item.get("size") is not None else None
    except (TypeError, ValueError):
        size = None
    return conn.execute(
        "INSERT INTO download_import (service, service_item_id, title, root_name, relative_path, path_key, event, status, message, size_bytes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (service, item.get("item_id"), item.get("title"), root or "", rel or item["path"], key, event, status, message, size, now, now)).lastrowid


def _duplicate(conn, key, size, now):
    """Whether this import is one we already have: still being handled, reported a moment ago, or our own replacement coming back."""
    last = conn.execute("SELECT * FROM download_import WHERE path_key = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    if last is None:
        return False
    if last["status"] in LIVE:
        return True
    if last["status"] == "replaced":
        result = _result(last)
        if size is not None and size == result.get("bytes"):
            return True
    return (db_seconds(now) - db_seconds(last["created_at"])) < DUPLICATE_WINDOW_SECONDS


def db_seconds(text):
    from datetime import datetime, timezone
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _result(row):
    try:
        value = json.loads(row["result_json"] or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _set(conn, intake_id, now=None, **values):
    values["updated_at"] = now or db.now()
    conn.execute(f"UPDATE download_import SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", [*values.values(), intake_id])


def get(conn, intake_id):
    r = conn.execute("SELECT * FROM download_import WHERE id = ?", (intake_id,)).fetchone()
    return dict(r) if r else None


def busy(conn, record):
    """Whether a download is being handled for this Library record (the Library must not queue a competing job for it)."""
    return conn.execute("SELECT 1 FROM download_import WHERE path_key = ? AND status IN ('queued', 'converting')", (record["path_key"],)).fetchone() is not None


# ---------------------------------------------------------------- moving each import along
def process(conn, now=None):
    """Advances every live import one step: start the scans that are due, read the finished ones. Cheap when there is nothing to do."""
    if conn.execute("SELECT 1 FROM download_import WHERE status IN ('pending', 'scanning') LIMIT 1").fetchone() is None:
        return
    now = now or db.now()
    for row in conn.execute("SELECT * FROM download_import WHERE status = 'scanning' ORDER BY id").fetchall():
        _read_scan(conn, dict(row), now)
    pending = [dict(r) for r in conn.execute("SELECT * FROM download_import WHERE status = 'pending' ORDER BY id")]
    started = set()
    for row in pending:
        folder = (row["root_name"], posixpath.dirname(row["relative_path"]))
        if folder in started:
            continue
        group = [r for r in pending if (r["root_name"], posixpath.dirname(r["relative_path"])) == folder]
        try:
            run_id = scan.start_scan(conn, folder[0], folder[1], by="downloads")
        except v.LibraryError as exc:
            if "already" in str(exc):
                continue                                    # a scan of that folder is running; this one is picked up when it ends
            for r in group:
                _set(conn, r["id"], now, status="blocked", message=str(exc))
            continue
        started.add(folder)
        for r in group:
            _set(conn, r["id"], now, status="scanning", scan_run_id=run_id, message="A worker is looking at the folder.")


def _read_scan(conn, row, now):
    run = conn.execute("SELECT * FROM library_run WHERE id = ?", (row["scan_run_id"],)).fetchone()
    if run is None:
        return _set(conn, row["id"], now, status="failed", message="The scan of this folder is gone. Press Check again.")
    described = scan.describe_run(conn, run)
    if described["active"]:
        return
    if described["status"] in ("failed", "cancelled"):
        return _set(conn, row["id"], now, status="failed", message=f"The scan of the folder {described['status']}: {described.get('error') or 'see the Workers page'}. Press Check again.")
    rec = store.by_key(conn, row["path_key"])
    if rec is None or rec["missing_since"]:
        return _set(conn, row["id"], now, status="failed", message="The scan did not find the file. Is the share path right on the Storage page?")
    decide(conn, row, dict(rec), now)


def risks(rec, plans):
    """Reasons a person should look before this file is replaced; empty when the plan is a plain track clean-up or a lossless remux."""
    from synchub.library import recommend
    reasons = []
    if rec.get("integrity_status") in v.INT_BAD:
        reasons.append(f"The file check said: {rec['integrity_status']}.")
    cats = recommend.categories(rec)
    if (cats - recommend.HANDLED) or (rec["review_status"] == v.REVIEW_MANUAL and not cats):
        reasons.append("The scan flagged this file for a playback check.")
    if rec.get("english_audio_status") != v.ENGLISH_PRESENT:
        reasons.append("English audio could not be confirmed, so removing tracks needs your eye.")
    for plan in plans:
        if (plan.get("video") or {}).get("action") == "transcode":
            reasons.append("The video would be re-encoded (lossy).")
        if plan.get("audio_convert"):
            reasons.append("Audio would be re-encoded.")
    return reasons


def decide(conn, row, rec, now=None):
    """What to do with an analysed import: nothing, ask, or queue the replacement."""
    now = now or db.now()
    if rec["analysis_status"] != v.ANALYSIS_COMPLETED or rec.get("unreadable_reason"):
        why = rec.get("unreadable_reason") or rec.get("audit_error") or rec.get("integrity_status") or "the analysis did not finish"
        return _set(conn, row["id"], now, record_id=rec["id"], status="blocked", message=f"Could not analyse the file: {why}")
    plans = [p for p in v.json_list(rec.get("proposed_track_changes_json")) if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")]
    if not plans:
        return _set(conn, row["id"], now, record_id=rec["id"], status="nothing_to_do", message="Nothing to change: the tracks and format are already fine.", finished_at=now)
    reasons = risks(rec, plans)
    if mode(conn) == "auto" and not reasons:
        _set(conn, row["id"], now, record_id=rec["id"], message="")
        return queue(conn, row["id"], "downloads (automatic)", now)
    why = " ".join(reasons) or "Waiting for your approval (review mode)."
    _set(conn, row["id"], now, record_id=rec["id"], status="awaiting_approval", message=why)


def queue(conn, intake_id, by, now=None):
    """Queues the replacement job for an analysed import. Returns the job id. Raises ValueError with a message for the page."""
    row = get(conn, intake_id)
    if row is None:
        raise ValueError("That import is gone.")
    if row["status"] not in ("awaiting_approval", "scanning", "pending"):
        raise ValueError(f"This import is {row['status'].replace('_', ' ')}, so it cannot be approved now.")
    rec = store.get_record(conn, row["record_id"]) if row["record_id"] else None
    if rec is None or rec["analysis_status"] != v.ANALYSIS_COMPLETED:
        raise ValueError("The file has not been analysed yet.")
    if rec["path_key"] != row["path_key"]:
        raise ValueError("The record no longer matches the file.")
    plans = [p for p in v.json_list(rec.get("proposed_track_changes_json")) if isinstance(p, dict) and p.get("op") in ("keep_tracks", "convert")]
    if not plans:
        raise ValueError("There is nothing to change in this file.")
    change = plans[0]
    dest_rel = row["relative_path"]
    extension = change.get("target_extension")
    if extension and not dest_rel.lower().endswith(extension.lower()):
        dest_rel = os.path.splitext(dest_rel)[0] + extension
    now = now or db.now()
    plan = {"op": "convert", "replace": True, "change": change, "revision": rec["source_revision"], "duration": rec.get("duration_seconds"),
            "source_size": rec["size_bytes"], "source_mtime": rec["mtime"], "originals_retention_days": retention_days(conn), "import": row["id"]}
    job_id = jobs.create(conn, "download", title=f"Replace: {rec['file_name']}", ref=(REF, row["id"]), source=(row["root_name"], row["relative_path"]), dest=(row["root_name"], dest_rel),
                         plan=plan, policy={"originals_retention_days": plan["originals_retention_days"]}, need_bytes=(rec["size_bytes"] or 0) * 2 + WORK_MARGIN_BYTES)
    _set(conn, row["id"], now, status="queued", job_id=job_id, decided_by=by, message="Queued for a worker.")
    return job_id


def approve(conn, intake_id, by):
    return queue(conn, intake_id, by)


def skip(conn, intake_id, by):
    row = get(conn, intake_id)
    if row is None or row["status"] not in ("pending", "awaiting_approval", "blocked", "failed"):
        raise ValueError("That import cannot be skipped now.")
    _set(conn, intake_id, status="skipped", decided_by=by, message=f"Left as it is ({by}).", finished_at=db.now())


def retry(conn, intake_id):
    """Looks at a blocked or failed import again, from the scan."""
    row = get(conn, intake_id)
    if row is None or row["status"] not in ("blocked", "failed", "skipped", "nothing_to_do"):
        raise ValueError("That import is already being handled.")
    if not row["root_name"]:
        located = locate(conn, row["service"], row["relative_path"])
        if located is None:
            raise ValueError(f"No share has a {row['service'].title()} path that covers {row['relative_path']} yet.")
        root, rel = located
        conn.execute("UPDATE download_import SET root_name = ?, relative_path = ?, path_key = ? WHERE id = ?", (root, rel, v.record_key(root, rel), intake_id))
    _set(conn, intake_id, status="pending", message="Waiting for a worker to look at the file.", finished_at=None, record_id=None, scan_run_id=None)


# ---------------------------------------------------------------- following the job
def sync(conn, job_id, now=None):
    """Makes the import follow its job. Returns True when this call is the one that finished it (Jellyfin should be told)."""
    job = jobs.get(conn, job_id)
    if job is None or job["ref_type"] != REF:
        return False
    row = get(conn, job["ref_id"])
    if row is None or row["job_id"] != job_id:
        return False
    target, now = JOB_STATUS.get(job["status"], "queued"), now or db.now()
    if row["status"] == target:
        return False
    values = {"status": target}
    if target == "replaced":
        result = job["result"] if isinstance(job["result"], dict) else {}
        values.update(result_json=json.dumps(result)[:4000], rescan_status="due", finished_at=now,
                      message=f"Replaced in place; {result.get('kept_original') and 'the original is kept on the worker' or 'the original was not kept'}.")
    elif target in ("failed", "blocked", "skipped"):
        values.update(message=(job["error"] or job["review_reason"] or job["message"] or ("Cancelled." if job["status"] == "cancelled" else ""))[:500], finished_at=now)
    elif target == "converting":
        values.update(message="A worker is converting it.")
    else:
        values.update(message="Queued for a worker.", finished_at=None)
    _set(conn, row["id"], now, **values)
    return target == "replaced"


def sync_active(conn):
    """Brings every import in step with its newest job (a job re-queued after its worker went silent, a retry from the Workers page...). Returns how many were finished."""
    rows = conn.execute("SELECT j.id FROM job j JOIN download_import d ON d.job_id = j.id WHERE j.ref_type = ? AND d.status IN ('queued', 'converting', 'blocked', 'failed')", (REF,)).fetchall()
    return sum(1 for r in rows if sync(conn, r["id"]))


# ---------------------------------------------------------------- telling Radarr / Sonarr
def rescan_due():
    """Asks Radarr/Sonarr to rescan every title whose file was just replaced. Makes network calls: call it with no transaction open. Never raises."""
    with db.connection() as conn:
        due = [dict(r) for r in conn.execute("SELECT * FROM download_import WHERE rescan_status = 'due' ORDER BY id")]
    for row in due:
        status, message = _rescan(row)
        with db.connection() as conn:
            conn.execute("UPDATE download_import SET rescan_status = ?, rescan_message = ?, updated_at = ? WHERE id = ?", (status, message, db.now(), row["id"]))
    return len(due)


def _rescan(row):
    try:
        if not row["service_item_id"]:
            return "failed", f"{row['service'].title()} did not say which title this was, so it was not asked to rescan."
        from synchub.clients.arr import RadarrClient, SonarrClient
        if row["service"] == "radarr":
            RadarrClient().rescan_movie(row["service_item_id"])
        else:
            SonarrClient().rescan_series(row["service_item_id"])
    except Exception as exc:
        logger.warning(f"[downloads] could not ask {row['service']} to rescan {row['title']}: {exc}")
        return "failed", str(exc)[:300]
    return "done", f"{row['service'].title()} was asked to rescan."


def ask_rescan_again(conn, intake_id):
    row = get(conn, intake_id)
    if row is None or row["status"] != "replaced":
        raise ValueError("Only a replaced file can be rescanned.")
    conn.execute("UPDATE download_import SET rescan_status = 'due', updated_at = ? WHERE id = ?", (db.now(), intake_id))


# ---------------------------------------------------------------- what the page shows
def recent(conn, limit=25):
    rows = [dict(r) for r in conn.execute("SELECT * FROM download_import ORDER BY id DESC LIMIT ?", (int(limit),))]
    for row in rows:
        row["name"] = posixpath.basename(row["relative_path"])
        row["when"] = (row["updated_at"] or "")[:16]
        row["result"] = _result(row)
    return rows


def summary(conn):
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM download_import GROUP BY status")}
    return {"mode": mode(conn), "awaiting": counts.get("awaiting_approval", 0), "working": sum(counts.get(s, 0) for s in ("pending", "scanning", "queued", "converting")),
            "replaced": counts.get("replaced", 0), "problems": counts.get("blocked", 0) + counts.get("failed", 0)}
