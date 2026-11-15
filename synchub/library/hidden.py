"""Hidden files on the Cleanup page: what was hidden, why, and deleting it for good.

A hidden file (aside.py) is only renamed on its share. Deleting it is a separate, confirmed step: a `delete_hidden` job, which a worker only does after checking that
the film's other (visible) file is still there at the size SyncHub knew. With "Delete hidden files after" set to a number of days, files hidden that long are
deleted too, but only after an administrator notification the day before (hidden_notice_at), so nothing goes without warning. The setting is off by default.
"""
from datetime import datetime, timedelta, timezone

from synchub import db, jobs, settings
from synchub.library import aside, file_labels, store
from synchub.library import vocab as v

NOTICE_HOURS = 24


class HiddenError(ValueError):
    pass


def delete_days(conn=None):
    try:
        return int(settings.get("hidden_delete_after_days", conn=conn))
    except (TypeError, ValueError):
        return 0


def _parse(text):
    try:
        out = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _due(hidden_at, days):
    when = _parse(hidden_at)
    return when + timedelta(days=days) if when and days else None


def listing(conn, now=None):
    """Every hidden file, oldest first, with its film, reason, size, and the days left when automatic deletion is on."""
    days, now = delete_days(conn), _parse(now or db.now())
    rows = conn.execute("SELECT f.id, f.file_name, f.root_name, f.relative_path, f.size_bytes, f.media_id, f.hidden_reason, f.hidden_at, m.title, m.year FROM library_file f "
                        "LEFT JOIN media m ON m.id = f.media_id WHERE f.dup_review = 'aside' ORDER BY f.hidden_at, f.id").fetchall()
    busy = {r["ref_id"]: r["status"] for r in conn.execute("SELECT ref_id, status FROM job WHERE ref_type = ? AND status IN ('queued', 'running', 'needs_review')", (aside.REF,))}
    items = []
    for r in rows:
        r = dict(r)
        due = _due(r["hidden_at"], days)
        r["reason_text"] = aside.REASON_TEXT.get(r["hidden_reason"] or "duplicate", "hidden")
        r["days_left"] = round((due - now).total_seconds() / 86400, 1) if due else None
        r["pending"] = busy.get(r["id"])
        items.append(r)
    return {"items": items, "bytes": sum(int(i["size_bytes"] or 0) for i in items), "days": days}


def request_delete(conn, record_id, now=None):
    """Queues the deletion of one hidden file. Returns the job id."""
    rec = store.get_record(conn, record_id)
    if rec is None or rec.get("dup_review") != "aside":
        raise HiddenError("This file is not hidden, so there is nothing to delete here.")
    if aside._busy(conn, record_id):
        raise HiddenError("A worker is already doing something with this file.")
    keep = None
    if rec.get("media_id"):
        keep = conn.execute("SELECT id, root_name, relative_path, size_bytes FROM library_file WHERE media_id = ? AND id != ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside' "
                            "ORDER BY is_main DESC, size_bytes DESC LIMIT 1", (rec["media_id"], record_id)).fetchone()
    elif rec.get("duplicate_group"):
        keep = conn.execute("SELECT id, root_name, relative_path, size_bytes FROM library_file WHERE duplicate_group = ? AND id != ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside' "
                            "ORDER BY size_bytes DESC LIMIT 1", (rec["duplicate_group"], record_id)).fetchone()
    if keep is None:
        raise HiddenError("The film has no other visible file, so this one is not deleted.")
    root, rel = rec["root_name"], v.clean_relative(rec["relative_path"])
    plan = {"op": "delete_hidden", "source_size": rec["size_bytes"], "keep_size": keep["size_bytes"], "reason": rec.get("hidden_reason")}
    job_id = jobs.create(conn, "library", title=f"Delete hidden file: {rec['file_name']}", ref=(aside.REF, record_id), source=(root, rel + aside.suffix_for(rec.get("hidden_reason"))),
                         dest=(keep["root_name"], v.clean_relative(keep["relative_path"])), plan=plan, need_bytes=0, now=now)
    store.event(conn, record_id, "system", "hidden_delete_queued", None, {"job": job_id})
    return job_id


def mark_deleted(conn, rec, job_id, now=None):
    store._update(conn, rec["id"], {"dup_review": "deleted", "is_old_version": 0, "missing_since": rec.get("missing_since") or now or db.now()})
    store.event(conn, rec["id"], "worker", "hidden_deleted", None, {"job": job_id})
    if rec.get("media_id"):
        file_labels.refresh(conn, [rec["media_id"]])


def sweep(conn, now=None):
    """With automatic deletion on: tells the administrators the day before, then queues the deletions that are due. Returns (noticed, queued)."""
    days = delete_days(conn)
    if not days:
        return 0, 0
    now = now or db.now()
    stamp = _parse(now)
    due_rows, notice, queued = [], [], 0
    for r in conn.execute("SELECT id, file_name, size_bytes, hidden_at, hidden_notice_at FROM library_file WHERE dup_review = 'aside' AND hidden_at IS NOT NULL ORDER BY id").fetchall():
        due = _due(r["hidden_at"], days)
        if due is None:
            continue
        if r["hidden_notice_at"] is None and due - stamp <= timedelta(hours=NOTICE_HOURS):
            notice.append(dict(r))
        elif r["hidden_notice_at"] is not None and due <= stamp and (_parse(r["hidden_notice_at"]) + timedelta(hours=NOTICE_HOURS - 1)) <= stamp:
            due_rows.append(r["id"])
    if notice:
        from synchub.notifications import emit
        gb = sum(int(n["size_bytes"] or 0) for n in notice) / 1073741824
        emit.notify(conn, "hidden_files_due", f"hidden-due:{now[:10]}", {"count": len(notice), "gb": round(gb, 1), "names": [n["file_name"] for n in notice][:5], "days": days}, now=now)
        for n in notice:
            store._update(conn, n["id"], {"hidden_notice_at": now})
    for rid in due_rows:
        try:
            request_delete(conn, rid, now)
            queued += 1
        except HiddenError:
            pass
    return len(notice), queued
