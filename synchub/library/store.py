"""The Library's database layer: the original tool's rules for storing analysis results and recording decisions, over `library_file`.

Who writes what: scans write identity, signature, analysis results and the *initial* review status; people write review and approval
decisions; the processing jobs (a later phase) write the `migration_*` columns. A scan that finds a file changed bumps its revision and
resets its review, approval and migration, so an approval can never outlive the file it was given for.
"""
import json
import os
import sqlite3
from contextlib import contextmanager

from synchub import db
from synchub.library import vocab as v

ANALYSIS_COLUMNS = [
    "analysis_status", "analysis_run_id", "analyzed_at", "analysis_logic_version",
    "integrity_mode", "integrity_status", "integrity_scope", "audit_error", "cache_status",
    "container", "duration_seconds", "bitrate_kbps", "video_codec", "resolution",
    "frame_rate", "audio_codecs", "audio_languages", "subtitle_languages",
    "video_streams", "audio_streams", "subtitle_streams", "streams_json",
    "media_type", "recommended_name", "show_title", "season_number",
    "naming_confidence", "naming_score", "naming_flags",
    "duplicate_group", "duplicate_status",
    "default_audio_language", "audio_detail", "audio_flags", "audio_recommendation",
    "subtitle_detail", "subtitle_flags", "subtitle_recommendation",
    "recommended_action", "base_action", "action_reason", "unreadable_reason",
    "english_audio_status", "proposed_track_changes_json",
    "warnings_json", "errors_json",
    "review_categories", "review_reason", "review_reasons_json", "findings_json",
]
# What depends on other files (a name collision, a duplicate, a file already at the destination): recomputed without looking at the file again.
DERIVED_COLUMNS = ["recommended_action", "base_action", "action_reason", "naming_flags", "review_categories", "review_reason", "review_reasons_json",
                   "duplicate_group", "duplicate_status"]

_RESET_APPROVAL = {"approval_status": v.APPROVAL_PENDING, "approved_action": None, "approved_name": None, "approved_track_changes_json": None,
                   "approval_decided_at": None, "approval_decided_by": None, "approval_note": None}
_RESET_REVIEW = {"review_decided_at": None, "review_decided_by": None, "review_note": None}
_RESET_MIGRATION = {"migration_status": v.MIGRATION_PENDING, "migration_last_error": None, "migration_started_at": None, "migration_finished_at": None,
                    "migration_verification_status": None, "migration_verification_detail": None, "migration_verified_at": None}


@contextmanager
def savepoint(conn, name="lib"):
    """A part of a transaction that can be undone on its own (and starts the transaction if none is open)."""
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        raise
    finally:
        conn.execute(f"RELEASE {name}")


def begin(conn):
    """Takes the write lock now, so a batch of changes is one transaction rather than one per statement."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def event(conn, record_id, actor, event_type, run_id=None, detail=None, now=None):
    conn.execute("INSERT INTO library_event (media_file_id, created_at, actor, run_id, event_type, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                 (record_id, now or db.now(), actor, run_id, event_type, json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None))


def get_record(conn, record_id):
    row = conn.execute("SELECT * FROM library_file WHERE id = ?", (record_id,)).fetchone()
    return dict(row) if row else None


def by_key(conn, path_key):
    row = conn.execute("SELECT * FROM library_file WHERE path_key = ?", (path_key,)).fetchone()
    return dict(row) if row else None


def events_for(conn, record_id, limit=50):
    out = []
    for r in conn.execute("SELECT * FROM library_event WHERE media_file_id = ? ORDER BY id DESC LIMIT ?", (record_id, limit)):
        e = dict(r)
        try:
            e["detail"] = json.loads(e["detail_json"]) if e["detail_json"] else None
        except ValueError:
            e["detail"] = None
        out.append(e)
    return out


def _insert(conn, rec, source_dir, run_id, now):
    values = {c: rec.get(c) for c in ANALYSIS_COLUMNS}
    values.update(path_key=rec["path_key"], root_name=rec["root_name"], relative_path=rec["relative_path"], source_path=rec["source_path"], source_dir=source_dir,
                  file_name=rec["file_name"], size_bytes=rec["size_bytes"], mtime=rec["mtime"], sha256=rec.get("sha256"), partial_signature=rec.get("partial_signature"),
                  first_seen_at=now, last_seen_at=now, last_seen_run_id=run_id, decision_fingerprint=v.decision_fingerprint(rec),
                  review_status=rec.get("review_status", v.REVIEW_NOT_REQUIRED))
    return conn.execute(f"INSERT INTO library_file ({', '.join(values)}) VALUES ({', '.join(':' + c for c in values)})", values).lastrowid


def _update(conn, record_id, values):
    conn.execute(f"UPDATE library_file SET {', '.join(f'{c} = :{c}' for c in values)} WHERE id = :__id", {**values, "__id": record_id})


def _kept_or_reset(row, new_review, fingerprint, changed):
    """For a file that has NOT changed: what happens to a person's decisions when its analysis result did. Returns (values, resets, decision_changed)."""
    decision_changed = not changed and row["decision_fingerprint"] is not None and row["decision_fingerprint"] != fingerprint
    values, resets = {}, []
    review = row["review_status"]
    if review == v.REVIEW_REVIEWED and not decision_changed:
        values["review_status"] = v.REVIEW_REVIEWED                 # the person's review stands
    else:
        values["review_status"] = new_review
        if review == v.REVIEW_REVIEWED:
            values.update(_RESET_REVIEW)
            resets.append("review")
    new_concern = row["review_status"] == v.REVIEW_NOT_REQUIRED and new_review == v.REVIEW_MANUAL
    if row["approval_status"] == v.APPROVAL_APPROVED and (decision_changed or new_concern):
        values.update(_RESET_APPROVAL)
        resets.append("approval")
    return values, resets, decision_changed


def _upsert_one(conn, rec, source_dir, run_id, tolerance, now, tally):
    row = conn.execute("SELECT * FROM library_file WHERE path_key = ?", (rec["path_key"],)).fetchone()
    if row is None:
        record_id = _insert(conn, rec, source_dir, run_id, now)
        tally["inserted"] += 1
        return record_id
    record_id = row["id"]
    seen = {"last_seen_at": now, "last_seen_run_id": run_id, "missing_since": None}

    # A migration in progress owns the record: a scan only notes that the file is still there.
    if row["migration_status"] == v.MIGRATION_IN_PROGRESS:
        _update(conn, record_id, seen)
        event(conn, record_id, "scan", "analysis_skipped_migration_in_progress", run_id, None, now)
        tally["locked"] += 1
        return record_id

    # An unreadable file that already has a real analysis keeps it (a passing network error must not wipe approvals); only the flag is set.
    placeholder = row["unreadable_reason"] is not None and row["size_bytes"] == 0 and row["mtime"] == 0
    now_unreadable = rec.get("unreadable_reason") is not None
    if now_unreadable and not placeholder:
        _update(conn, record_id, {**seen, "unreadable_reason": rec["unreadable_reason"]})
        event(conn, record_id, "scan", "source_unreadable", run_id, {"reason": rec["unreadable_reason"]}, now)
        tally["unreadable_flagged"] += 1
        return record_id

    new_hash = rec.get("sha256")
    signature_changed = False if (placeholder or now_unreadable) else (row["size_bytes"] != rec["size_bytes"] or abs(row["mtime"] - rec["mtime"]) > tolerance)
    hash_changed = bool(not signature_changed and new_hash and row["sha256"] and new_hash != row["sha256"])        # content changed, modified date kept
    changed = signature_changed or hash_changed
    fingerprint = v.decision_fingerprint(rec)

    values = {c: rec.get(c) for c in ANALYSIS_COLUMNS}
    values.update(seen)
    values.update(source_path=rec["source_path"], source_dir=source_dir, file_name=rec["file_name"], decision_fingerprint=fingerprint,
                  root_name=rec["root_name"], relative_path=rec["relative_path"])
    if placeholder:
        values.update(size_bytes=rec["size_bytes"], mtime=rec["mtime"])
    new_review = rec.get("review_status", v.REVIEW_NOT_REQUIRED)
    resets = []
    if changed:
        values.update(size_bytes=rec["size_bytes"], mtime=rec["mtime"], sha256=new_hash, partial_signature=rec.get("partial_signature"),
                      source_revision=row["source_revision"] + 1, signature_changed_at=now, review_status=new_review, **_RESET_REVIEW, **_RESET_APPROVAL)
        resets += ["review", "approval"]
        if row["migration_status"] != v.MIGRATION_PENDING or row["migration_verification_status"] is not None:
            values.update(_RESET_MIGRATION)
            resets.append(f"migration (was {row['migration_status']})")
        event(conn, record_id, "scan", "source_changed", run_id, {
            "reason": "size/mtime changed" if signature_changed else "content hash changed",
            "old": {"size": row["size_bytes"], "mtime": row["mtime"], "sha256": row["sha256"], "revision": row["source_revision"]},
            "new": {"size": rec["size_bytes"], "mtime": rec["mtime"], "sha256": new_hash, "revision": row["source_revision"] + 1}, "reset": resets}, now)
        tally["changed"] += 1
    else:
        values["sha256"] = new_hash or row["sha256"]
        values["partial_signature"] = rec.get("partial_signature") or row["partial_signature"]
        extra, resets, decision_changed = _kept_or_reset(row, new_review, fingerprint, changed)
        values.update(extra)
        if resets or decision_changed:
            event(conn, record_id, "scan", "decision_changed", run_id, {"old_fingerprint": row["decision_fingerprint"], "new_fingerprint": fingerprint, "reset": resets}, now)
        tally["unchanged"] += 1
    if "approval" in resets:
        tally["approvals_reset"] += 1
    _update(conn, record_id, values)
    return record_id


def new_tally():
    return {"inserted": 0, "unchanged": 0, "changed": 0, "locked": 0, "approvals_reset": 0, "unreadable_flagged": 0, "failed": []}


def upsert_records(conn, records, source_dir, run_id, tolerance=v.MTIME_TOLERANCE_SECONDS, now=None):
    """Inserts or updates one row per record, never two. All in one transaction; each record in its own savepoint, so one bad record is
    reported in tally['failed'] without losing the others."""
    now = now or db.now()
    tally = new_tally()
    begin(conn)
    for rec in records:
        try:
            with savepoint(conn, "rec"):
                _upsert_one(conn, rec, source_dir, run_id, tolerance, now, tally)
        except (sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError) as exc:
            tally["failed"].append((rec.get("relative_path", "?"), f"{type(exc).__name__}: {exc}"))
    return tally


def touch_seen(conn, path_keys, run_id, now=None):
    """The files a scan found unchanged: still there, and seen by this run."""
    now = now or db.now()
    begin(conn)
    conn.executemany("UPDATE library_file SET last_seen_at = ?, last_seen_run_id = ?, missing_since = NULL WHERE path_key = ?",
                     [(now, run_id, k) for k in path_keys])


def mark_missing(conn, run_id, root, prefix, now=None, skip=None):
    """Files under root/prefix this run did not see are marked missing (never deleted from the database). `skip(relative)` is true for
    files in folders the scan deliberately left out. Returns how many were marked."""
    now = now or db.now()
    begin(conn)
    unseen = [r for r in conn.execute("SELECT id, relative_path FROM library_file WHERE root_name = ? AND missing_since IS NULL AND COALESCE(last_seen_run_id, '') != ?",
                                      (root, run_id)) if v.under(r["relative_path"], prefix) and not (skip and skip(r["relative_path"]))]
    conn.executemany("UPDATE library_file SET missing_since = ? WHERE id = ?", [(now, r["id"]) for r in unseen])
    for r in unseen:
        event(conn, r["id"], "scan", "source_missing", run_id, None, now)
    return len(unseen)


def apply_derived(conn, row, derived, run_id=None, now=None):
    """Stores findings that depend on other files (a collision, a duplicate) for a file that has not changed. Decisions are kept unless
    the result of the analysis changed, exactly as in a scan. Returns whether anything was written."""
    now = now or db.now()
    fingerprint = v.decision_fingerprint({**row, **derived})
    extra, resets, decision_changed = _kept_or_reset(row, derived["new_review"], fingerprint, False)
    values = {c: derived[c] for c in DERIVED_COLUMNS}
    values.update(extra, decision_fingerprint=fingerprint)
    if all(row.get(c) == values[c] for c in values):
        return False
    _update(conn, row["id"], values)
    if resets or decision_changed:
        event(conn, row["id"], "scan", "decision_changed", run_id, {"old_fingerprint": row["decision_fingerprint"], "new_fingerprint": fingerprint, "reset": resets,
                                                                     "why": "findings that depend on other files changed"}, now)
    return True


# ---------------------------------------------------------------- decisions
def _apply_keep_streams(proposal_json, keep_streams):
    """The proposal with the reviewer's keep-overrides applied (None if nothing is left to remove). A reviewer can keep tracks the proposal
    would have removed, never remove more than it listed."""
    if not keep_streams:
        return proposal_json
    keep = {int(i) for i in keep_streams}
    result = []
    for plan in json.loads(proposal_json):
        if plan.get("op") not in ("keep_tracks", "convert"):
            result.append(plan)
            continue
        removable = {t["index"] for t in plan["remove_audio"] + plan["remove_subtitle"] if not t.get("reason")}      # a track MKV cannot hold cannot be kept
        if not keep <= removable:
            raise ValueError(f"streams {sorted(keep - removable)} are not in the proposed removals")
        for remove_key, keep_key in (("remove_audio", "keep_audio"), ("remove_subtitle", "keep_subtitle")):
            saved = [t["index"] for t in plan[remove_key] if t["index"] in keep]
            plan[keep_key] = sorted(plan[keep_key] + saved)
            plan[remove_key] = [t for t in plan[remove_key] if t["index"] not in keep]
        if plan["remove_audio"] or plan["remove_subtitle"] or plan["op"] == "convert":
            result.append(plan)
    return json.dumps(result) if result else None


def set_approval(conn, record_id, status, by, action=None, note=None, mark_reviewed=False, name=None, accept_track_changes=False, keep_streams=None):
    """Records a person's decision. Approving needs a finished analysis and a migratable action; a file that needs manual review can only
    be approved together with that review (mark_reviewed). `name` edits the filename (same extension, no collision with another approved
    file); `accept_track_changes` approves the proposed track changes; `keep_streams` keeps tracks the proposal would have removed."""
    if status not in (v.APPROVAL_PENDING, v.APPROVAL_APPROVED, v.APPROVAL_REJECTED):
        raise ValueError(f"invalid approval status: {status}")
    now = db.now()
    with savepoint(conn, "approval"):
        row = conn.execute("SELECT * FROM library_file WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise v.StateError(f"no record {record_id}")
        if row["migration_status"] == v.MIGRATION_IN_PROGRESS:
            raise v.StateError("cannot change approval while migration is in progress")
        values = {"approval_status": status, "approval_decided_at": now, "approval_decided_by": by, "approval_note": note,
                  "approved_action": None, "approved_name": None, "approved_track_changes_json": None}   # every decision starts clean
        if status == v.APPROVAL_PENDING:
            values.update(approval_decided_at=None, approval_decided_by=None)
        if status == v.APPROVAL_APPROVED:
            default = row["recommended_action"] if row["recommended_action"] in v.MIGRATABLE_ACTIONS else row["base_action"]
            chosen = action or default
            if chosen not in v.MIGRATABLE_ACTIONS:
                raise ValueError(f"no migratable action to approve (got '{chosen}'); choose one of {v.MIGRATABLE_ACTIONS}")
            if row["analysis_status"] != v.ANALYSIS_COMPLETED:
                raise ValueError("analysis is not completed; scan again first")
            if row["review_status"] == v.REVIEW_MANUAL:
                if not mark_reviewed:
                    raise ValueError("record requires manual review; approve it together with the review")
                values.update(review_status=v.REVIEW_REVIEWED, review_decided_at=now, review_decided_by=by, review_note=note)
            values["approved_action"] = chosen
            if accept_track_changes:
                if not row["proposed_track_changes_json"] or row["proposed_track_changes_json"] == "[]":
                    raise ValueError("this record has no proposed track changes to accept")
                values["approved_track_changes_json"] = _apply_keep_streams(row["proposed_track_changes_json"], keep_streams)
            base = row["recommended_name"] or ""
            name = name.strip() if name else None
            if name and name != base:
                problems = v.filename_problems(name)
                if os.path.splitext(name)[1].lower() != os.path.splitext(base)[1].lower():
                    problems.append(f"extension must stay '{os.path.splitext(base)[1]}'")
                if problems:
                    raise ValueError("invalid name: " + "; ".join(problems))
                values["approved_name"] = name
            final = values.get("approved_name") or base
            clash = conn.execute(
                "SELECT id FROM library_file WHERE id != ? AND approval_status = 'approved' AND lower(COALESCE(approved_name, recommended_name)) = lower(?) "
                "AND COALESCE(media_type, '') = COALESCE(?, '') AND COALESCE(show_title, '') = COALESCE(?, '') AND COALESCE(season_number, -1) = COALESCE(?, -1)",
                (record_id, final, row["media_type"], row["show_title"], row["season_number"])).fetchone()
            if clash:
                raise ValueError(f"name '{final}' collides with approved record {clash[0]}; edit the name or reject one of them")
        _update(conn, record_id, values)
        event(conn, record_id, "user", f"approval_{status}", None, {"by": by, "action": values["approved_action"], "name": values.get("approved_name"), "note": note,
                                                                   "track_changes": bool(values.get("approved_track_changes_json"))}, now)
