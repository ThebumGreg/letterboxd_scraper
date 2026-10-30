"""Scans and duplicate searches: what the Library asks the workers to do, and what it does with what they report.

A scan is one job on one worker: the worker lists the files of a share (or a folder in it), SyncHub says which ones it needs to look at
(new, changed, never finished, analysed by older logic), the worker analyses those and reports them, and SyncHub finishes the picture
(files not seen are marked missing; findings that depend on other files are worked out). A scan that is interrupted simply runs again:
everything it already reported is kept, so only the rest is looked at.
"""
import fnmatch
import json
import re
import uuid
from datetime import datetime, timezone

from synchub import db, jobs, settings, storage
from synchub.library import duplicates, findings, store
from synchub.library import vocab as v

RUN_REF = "library_run:"
MAX_INVENTORY_BATCH, MAX_RECORD_BATCH = 5000, 200

# What the worker may set on a record. Everything else (findings that depend on other files, review and approval state) is SyncHub's.
AGENT_FIELDS = (
    "analysis_status", "analyzed_at", "integrity_mode", "integrity_status", "integrity_scope", "audit_error",
    "container", "duration_seconds", "bitrate_kbps", "video_codec", "resolution", "frame_rate", "audio_codecs", "audio_languages", "subtitle_languages",
    "video_streams", "audio_streams", "subtitle_streams", "streams_json", "video_info_json", "media_type", "recommended_name", "show_title", "season_number",
    "naming_confidence", "naming_score", "default_audio_language", "audio_detail", "audio_flags", "audio_recommendation",
    "subtitle_detail", "subtitle_flags", "subtitle_recommendation", "english_audio_status", "proposed_track_changes_json", "warnings_json", "errors_json",
)


# ---------------------------------------------------------------- settings
def config(conn=None):
    """Where approved files go and how they are laid out."""
    return v.LibraryConfig(destination_root=settings.get("library_destination_root", conn=conn) or "media", tv_folder=settings.get("library_tv_folder", conn=conn) or "TV Shows",
                           movie_folder=settings.get("library_movie_folder", conn=conn) or "Movies",
                           naming_style="current" if settings.get("naming_style", conn=conn) == "current" else "jellyfin")


def exclude_list(text):
    return [line.strip() for line in str(text or "").replace("\r", "").split("\n") if line.strip()]


def scan_options(conn=None):
    try:
        tail = int(settings.get("library_tail_seconds", conn=conn))
    except ValueError:
        tail = 30
    mode = settings.get("library_integrity_mode", conn=conn)
    return {"integrity": mode if mode in v.INTEGRITY_MODES else "tail", "tail_seconds": tail, "exclude": exclude_list(settings.get("library_exclude_folders", conn=conn))}


class ExcludeRules:
    """Folders a scan leaves out: a folder name or wildcard ('Extras', '*Sample*', '@eaDir'), or 'share:folder/path'. The same rules run on the worker."""

    def __init__(self, patterns, root_name):
        self.root, self.names, self.paths = root_name.lower(), [], []
        for pattern in patterns or []:
            pattern = str(pattern).strip()
            if not pattern:
                continue
            match = re.match(r"^([A-Za-z0-9_-]{2,}):(.*)$", pattern)
            if match:
                if match.group(1).lower() == self.root:
                    self.paths.append(v.clean_relative(match.group(2)))
            else:
                self.names.append(pattern.casefold())

    def folder(self, rel_dir):
        rel_dir = v.clean_relative(rel_dir)
        name = rel_dir.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatch(name.casefold(), n) for n in self.names) or any(v.under(rel_dir, p) for p in self.paths)

    def path(self, rel_file):
        parts = v.clean_relative(rel_file).split("/")[:-1]
        return any(self.folder("/".join(parts[:i + 1])) for i in range(len(parts)))


# ---------------------------------------------------------------- runs
def _counts(run):
    try:
        return json.loads(run["counts_json"] or "{}")
    except ValueError:
        return {}


def bump(conn, run_id, **delta):
    """Adds to a run's counters (and sets text ones). One transaction at a time, so concurrent jobs of one run do not lose updates."""
    store.begin(conn)
    row = conn.execute("SELECT counts_json FROM library_run WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return
    counts = json.loads(row["counts_json"] or "{}")
    for key, value in delta.items():
        counts[key] = (counts.get(key, 0) + value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    conn.execute("UPDATE library_run SET counts_json = ? WHERE id = ?", (json.dumps(counts), run_id))


def create_run(conn, kind, root, prefix, options, by):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = f"{stamp}_{(root or 'library')}_{uuid.uuid4().hex[:6]}"
    conn.execute("INSERT INTO library_run (id, kind, root_name, prefix, status, options_json, started_by, created_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                 (run_id, kind, root, prefix, json.dumps(options), by, db.now()))
    return run_id


def run_jobs(conn, run_id):
    return [jobs.row(r) for r in conn.execute("SELECT * FROM job WHERE ref_type = ? ORDER BY id", (RUN_REF + run_id,))]


def describe_run(conn, run):
    """A run as the page shows it. Its state comes from its jobs, so a cancelled, failed or finished job is never out of step."""
    run = dict(run)
    run["options"] = json.loads(run["options_json"] or "{}")
    run["counts"] = _counts(run)
    members = run_jobs(conn, run["id"])
    states = {j["status"] for j in members}
    if not members:
        status = run["status"]
    elif "running" in states or ("queued" in states and states & {"done", "needs_review"}):
        status = "running"
    elif states == {"queued"}:
        status = "queued"
    elif "needs_review" in states:
        status = "waiting"
    elif "failed" in states:
        status = "failed"
    elif states & {"cancelled", "skipped"}:
        status = "cancelled"
    else:
        status = "completed_with_errors" if (run["counts"].get("errors") or run["counts"].get("unreadable")) else "completed"
    run["status"] = status
    run["active"] = status in ("queued", "running", "waiting")
    known = [j["progress"] for j in members if j["progress"] is not None]
    run["progress"] = (sum(known) / len(members)) if known and status != "completed" else (1.0 if status in ("completed", "completed_with_errors") else None)
    run["message"] = next((j["message"] for j in members if j["status"] == "running" and j["message"]), None) or next((j["message"] for j in members if j["message"] and j["status"] != "done"), None)
    run["job_ids"] = [j["id"] for j in members]
    run["error"] = run.get("error") or next((j["error"] for j in members if j["status"] == "failed" and j["error"]), None)
    return run


def list_runs(conn, limit=15, active_only=False):
    out = [describe_run(conn, r) for r in conn.execute("SELECT * FROM library_run ORDER BY rowid DESC LIMIT ?", (int(limit) if not active_only else 200,))]
    return [r for r in out if r["active"]] if active_only else out


# ---------------------------------------------------------------- starting work
def start_scan(conn, root, prefix="", by="someone", integrity=None, force=False, exclude=None):
    """Queues a scan of a share, or a folder in it. Returns the run id."""
    root = str(root or "").strip().lower()
    row = storage.by_name(conn, root)
    if row is None:
        raise v.LibraryError(f"There is no share called '{root}'. Add it on the Storage page first.")
    try:
        prefix = storage.clean_rel(prefix) if str(prefix or "").strip().strip("/\\") else ""
    except storage.StorageError as exc:
        raise v.LibraryError(str(exc))
    options = scan_options(conn)
    if integrity is not None:
        if integrity not in v.INTEGRITY_MODES:
            raise v.LibraryError("The integrity check is metadata, tail or full.")
        options["integrity"] = integrity
    if exclude is not None:
        options["exclude"] = exclude_list(exclude) if isinstance(exclude, str) else [str(e).strip() for e in exclude if str(e).strip()]
    for other in list_runs(conn, active_only=True):
        if other["kind"] == "scan" and other["root_name"] == root and (v.under(prefix, other["prefix"]) or v.under(other["prefix"], prefix)):
            raise v.LibraryError(f"A scan of {root}{'/' + other['prefix'] if other['prefix'] else ''} is already {other['status']}. Wait for it, or cancel it on the Workers page.")
    run_id = create_run(conn, "scan", root, prefix, {**options, "force": bool(force)}, by)
    jobs.create(conn, "scan", title=f"Scan {root}" + (f"/{prefix}" if prefix else ""), ref=(RUN_REF + run_id, None), source=(root, prefix),
                plan={"op": "scan", "run_id": run_id, "root": root, "prefix": prefix, "integrity": options["integrity"], "tail_seconds": options["tail_seconds"],
                      "exclude": options["exclude"], "force": bool(force), "logic_version": v.ANALYSIS_LOGIC_VERSION})
    return run_id


def start_duplicate_search(conn, by="someone"):
    """Finds exact duplicates across every share. Reads only files whose content is not known yet. Returns the run id."""
    plans = duplicates.plan_jobs(conn)
    for other in list_runs(conn, active_only=True):
        if other["kind"] == "duplicates":
            raise v.LibraryError(f"A duplicate search is already {other['status']}.")
    cfg = config(conn)
    run_id = create_run(conn, "duplicates", None, "", {"jobs": len(plans), "files": sum(len(g["files"]) for p in plans for g in p["groups"])}, by)
    if not plans:
        groups, changed = duplicates.finish(conn, cfg, run_id)
        bump(conn, run_id, groups=groups, changed=changed)
        conn.execute("UPDATE library_run SET status = 'completed', finished_at = ? WHERE id = ?", (db.now(), run_id))
        return run_id
    for plan in plans:
        roots = sorted({f["root"] for g in plan["groups"] for f in g["files"]})
        count = sum(len(g["files"]) for g in plan["groups"])
        jobs.create(conn, "scan", title=f"Duplicate search: {count} file{'s' if count != 1 else ''}", ref=(RUN_REF + run_id, None),
                    plan={**plan, "run_id": run_id, "roots": roots, "logic_version": v.ANALYSIS_LOGIC_VERSION})
    return run_id


# ---------------------------------------------------------------- what workers report
def _plan(job, op):
    if job["kind"] != "scan" or job["plan"].get("op") != op:
        raise jobs.JobError(f"This job is not a {op} job.")
    return job["plan"]


def _rel(value):
    try:
        return storage.clean_rel(value)
    except storage.StorageError as exc:
        raise jobs.JobError(str(exc))


def inventory(conn, job, files, unreadable=(), now=None):
    """The worker lists files it found. Returns the relative paths SyncHub needs it to analyse; the rest are noted as still there."""
    plan = _plan(job, "scan")
    if len(files) > MAX_INVENTORY_BATCH:
        raise jobs.JobError(f"Send at most {MAX_INVENTORY_BATCH} files at a time.")
    root, run_id, now = plan["root"], plan["run_id"], now or db.now()
    store.begin(conn)
    need, seen, new = [], [], 0
    for item in files:
        try:
            rel, size, mtime = _rel(item["rel"]), int(item["size"]), float(item["mtime"])
        except (KeyError, TypeError, ValueError):
            raise jobs.JobError("Every file needs rel, size and mtime.")
        row = store.by_key(conn, v.record_key(root, rel))
        if row is None:
            need.append(rel)
            new += 1
        elif row["migration_status"] == v.MIGRATION_IN_PROGRESS:
            seen.append(row["path_key"])
        elif (plan.get("force") or row["unreadable_reason"] or row["analysis_status"] != v.ANALYSIS_COMPLETED or row["analysis_logic_version"] != plan.get("logic_version")
              or row["integrity_mode"] != plan.get("integrity") or row["size_bytes"] != size or abs(row["mtime"] - mtime) > v.MTIME_TOLERANCE_SECONDS):
            need.append(rel)
        else:
            seen.append(row["path_key"])
    store.touch_seen(conn, seen, run_id, now)
    source_dir = f"{root}:{plan.get('prefix') or ''}"
    placeholders = []
    for item in unreadable:
        rel = _rel(item.get("rel"))
        placeholders.append(unreadable_record(conn, root, rel, str(item.get("file_name") or rel.rsplit("/", 1)[-1]), str(item.get("unreadable_reason") or "unreadable")[:500], run_id, source_dir))
    tally = store.upsert_records(conn, placeholders, source_dir, run_id, now=now) if placeholders else store.new_tally()
    bump(conn, run_id, found=len(files) + len(unreadable), need=len(need), unchanged=len(seen), new=new, unreadable=len(unreadable), errors=len(tally["failed"]))
    return need


def unreadable_record(conn, root, rel, name, reason, run_id, source_dir):
    """A file that exists but could not be read: kept as a failed analysis that can never be approved while it stays unreadable."""
    row = storage.by_name(conn, root)
    return {"path_key": v.record_key(root, rel), "root_name": root, "relative_path": rel, "source_path": storage.display_path(row, rel) if row else f"{root}:{rel}",
            "file_name": name, "size_bytes": 0, "mtime": 0.0, "analysis_status": v.ANALYSIS_FAILED, "analysis_run_id": run_id, "analyzed_at": db.now(),
            "analysis_logic_version": v.ANALYSIS_LOGIC_VERSION, "integrity_status": "Unreadable", "integrity_scope": "File could not be read", "audit_error": reason,
            "unreadable_reason": reason, "recommended_action": v.ACTION_MANUAL_REVIEW, "action_reason": reason, "duplicate_status": v.DUP_NOT_CHECKED,
            "warnings_json": "[]", "errors_json": json.dumps([reason]), "review_categories": "integrity", "review_reason": f"[integrity] {reason}",
            "review_reasons_json": json.dumps([["integrity", reason]]), "review_status": v.REVIEW_MANUAL, "source_dir": source_dir}


def build_record(conn, plan, rec, existing):
    """A stored record from what a worker reported about one file (plus what SyncHub already knows of the file's duplicates)."""
    root, rel = plan["root"], _rel(rec.get("rel"))
    found = rec.get("findings")
    if not isinstance(found, dict) or not isinstance(found.get("reasons"), list):
        raise jobs.JobError("A record needs its findings.")
    row = storage.by_name(conn, root)
    out = {k: rec.get(k) for k in AGENT_FIELDS}
    try:
        size, mtime = int(rec["size_bytes"]), float(rec["mtime"])
    except (KeyError, TypeError, ValueError):
        raise jobs.JobError("A record needs size_bytes and mtime.")
    out.update(path_key=v.record_key(root, rel), root_name=root, relative_path=rel, source_path=storage.display_path(row, rel) if row else f"{root}:{rel}",
               file_name=str(rec.get("file_name") or rel.rsplit("/", 1)[-1]), size_bytes=size, mtime=mtime, analysis_run_id=plan["run_id"],
               analysis_logic_version=int(rec.get("logic_version") or 0), findings_json=json.dumps(found, ensure_ascii=False),
               source_dir=f"{root}:{plan.get('prefix') or ''}", unreadable_reason=None, sha256=None,
               duplicate_group=existing["duplicate_group"] if existing else None, duplicate_status=(existing["duplicate_status"] if existing else None) or v.DUP_NOT_CHECKED)
    derived = findings.derive(out)
    out.update({k: derived[k] for k in store.DERIVED_COLUMNS})
    out["review_status"] = derived["new_review"]
    return out


def ingest_records(conn, job, records, now=None):
    """Stores the analyses a worker reports. Returns the tally."""
    plan = _plan(job, "scan")
    if len(records) > MAX_RECORD_BATCH:
        raise jobs.JobError(f"Send at most {MAX_RECORD_BATCH} records at a time.")
    store.begin(conn)
    built, problems = [], []
    for rec in records:
        try:
            existing = store.by_key(conn, v.record_key(plan["root"], _rel(rec.get("rel"))))
            built.append(build_record(conn, plan, rec, existing))
        except jobs.JobError as exc:
            problems.append((str(rec.get("rel"))[:200], str(exc)))
    tally = store.upsert_records(conn, built, f"{plan['root']}:{plan.get('prefix') or ''}", plan["run_id"], now=now)
    analyzed = len(built) - len(tally["failed"])
    tally["failed"] += problems
    bump(conn, plan["run_id"], analyzed=analyzed, inserted=tally["inserted"], changed=tally["changed"], approvals_reset=tally["approvals_reset"], locked=tally["locked"],
         errors=len(tally["failed"]))
    return tally


def ingest_hashes(conn, job, results):
    plan = _plan(job, "hash")
    tally = {}
    stored = duplicates.apply_results(conn, results, tally=tally)
    bump(conn, plan["run_id"], hashed=tally.get("hashed", 0), ruled_out=tally.get("ruled_out", 0), errors=tally.get("errors", 0))
    return stored


def finish(conn, job, summary=None, now=None):
    """The worker is done reporting. A scan marks the files it did not see as missing (unless a folder could not be listed) and everything is
    re-examined; a duplicate search forms the groups. Returns a summary."""
    summary = summary or {}
    plan = job["plan"]
    if job["kind"] != "scan" or plan.get("op") not in ("scan", "hash") or not plan.get("run_id"):
        raise jobs.JobError("This job is not a scan or a duplicate search.")
    cfg, now = config(conn), now or db.now()
    run_id = plan["run_id"]
    if plan.get("op") == "scan":
        missing = 0
        walk_errors = summary.get("walk_errors") or []
        if summary.get("complete") and not walk_errors:
            rules = ExcludeRules(plan.get("exclude"), plan["root"])
            missing = store.mark_missing(conn, run_id, plan["root"], plan.get("prefix") or "", now, skip=rules.path)
        bump(conn, run_id, missing=missing, errors=len(walk_errors), walk_errors=len(walk_errors))
        for error in walk_errors[:5]:
            bump(conn, run_id, last_walk_error=str(error)[:300])
    groups, changed = duplicates.finish(conn, cfg, run_id, now)
    bump(conn, run_id, groups=groups, refreshed=changed)
    conn.execute("UPDATE library_run SET finished_at = ? WHERE id = ?", (now, run_id))
    return {"groups": groups, "refreshed": changed}
