"""What is using a worker's work folder, and the requests to delete job folders from it.

The agent measures its work folder every few minutes (job folders, kept originals, logs, anything else) and sends the result with its poll. SyncHub keeps the
latest measurement per worker (a setting, not the status summary, so it cannot crowd that out) and shows it on the Workers page. A person can ask for job
folders to be deleted one by one or in bulk; the ids wait in a small list that the next poll answer carries to the worker (`purge`), and are dropped from the
list once the worker no longer holds the folder.

Safety is on both sides. SyncHub refuses a job that is running, a folder whose result SyncHub has not received yet, and (in bulk) a job waiting for a decision, a
queued job or one that stopped part-way. The agent refuses again for a job it is running. Only job folders are ever deleted; anything else in the work folder
is listed as "Other" and left alone. Kept originals are managed on the Originals page.
"""
import json

from synchub import db, jobs, settings, workers

USAGE_KEY = "worker_usage:{}"
PURGE_KEY = "worker_purge:{}"
MAX_JOBS = 40
FAILED_STATES = ("failed", "cancelled", "leftover")                  # what "failed" means in bulk; "review" (waiting for you) and "interrupted" (may resume) are never bulk-deleted
DEFAULT_FAILED_DAYS = 3


class WorkDriveError(Exception):
    """The request cannot be done; the message says why in plain words."""


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def clean_usage(usage):
    """Only the known fields, numbers as numbers, at most MAX_JOBS job rows."""
    if not isinstance(usage, dict):
        return None
    rows = []
    for item in (usage.get("jobs") or [])[:MAX_JOBS]:
        if isinstance(item, dict) and isinstance(item.get("job_id"), int):
            rows.append({"job_id": item["job_id"], "bytes": int(_num(item.get("bytes")) or 0), "state": str(item.get("state") or "")[:20], "title": str(item.get("title") or "")[:120],
                         "age_days": _num(item.get("age_days")) or 0, "keep_until": str(item.get("keep_until") or "")[:40] or None, "unreported": bool(item.get("unreported"))})
    out = {k: _num(usage.get(k)) for k in ("total_bytes", "used_bytes", "jobs_bytes", "jobs_count", "originals_bytes", "logs_bytes", "other_bytes", "jobs_unlisted")}
    out.update(at=str(usage.get("at") or "")[:40], jobs=rows)
    return out


def store_usage(conn, worker_id, usage):
    cleaned = clean_usage(usage)
    if cleaned is not None:
        settings.set_value(USAGE_KEY.format(worker_id), json.dumps(cleaned), conn=conn)


def pending(conn, worker_id):
    got = workers._load(settings.get(PURGE_KEY.format(worker_id), "", conn=conn), [])
    return [i for i in got if isinstance(i, int)] if isinstance(got, list) else []


def _save_pending(conn, worker_id, ids):
    settings.set_value(PURGE_KEY.format(worker_id), json.dumps(sorted(set(ids))) if ids else "", conn=conn)


def for_worker(conn, worker_id, holding):
    """The job ids the worker should delete now: those asked for that it still holds. Ones it no longer holds are forgotten."""
    ids = pending(conn, worker_id)
    if not ids:
        return []
    held = [i for i in ids if isinstance(holding, list) and i in holding]
    if held != ids:
        _save_pending(conn, worker_id, held)
    return held


def _job_rows(conn, ids):
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: r for r in conn.execute(f"SELECT id, title, status, worker_id, kind FROM job WHERE id IN ({marks})", list(ids))}


def _why_not(item, row, worker_id):
    """Why this folder may not be deleted by hand, or None."""
    if item["state"] == "running" or (row is not None and row["status"] == "running" and row["worker_id"] == worker_id):
        return "It is running."
    if item.get("unreported"):
        return "SyncHub has not received its result yet."
    return None


def _bulk_kind(item, row, days):
    """'finished' or 'failed_old' when a bulk action would take this folder, else None."""
    if item["state"] == "finished":
        return "finished"
    if item["state"] in FAILED_STATES and item["age_days"] >= days and (row is None or row["status"] in ("failed", "cancelled", "done", "skipped")):
        return "failed_old"
    return None


def overview(conn, worker_id, failed_days=DEFAULT_FAILED_DAYS):
    """The Work drive panel: the latest measurement, each job folder joined to its job, what is already being deleted and what a bulk action would free."""
    usage = workers._load(settings.get(USAGE_KEY.format(worker_id), "", conn=conn), {})
    if not usage:
        return {"known": False}
    waiting = set(pending(conn, worker_id))
    rows = _job_rows(conn, [j["job_id"] for j in usage["jobs"]])
    items, bulk = [], {"finished": [0, 0], "failed_old": [0, 0]}
    for item in usage["jobs"]:
        row = rows.get(item["job_id"])
        why = _why_not(item, row, worker_id)
        kind = _bulk_kind(item, row, failed_days) if why is None else None
        if kind:
            bulk[kind][0] += 1
            bulk[kind][1] += item["bytes"]
        items.append({**item, "title": (row["title"] if row else None) or item["title"] or f"Job {item['job_id']}", "job_status": row["status"] if row else None,
                      "can_delete": why is None, "why_not": why, "deleting": item["job_id"] in waiting})
    return {"known": True, **{k: v for k, v in usage.items() if k != "jobs"}, "jobs": items, "pending": len(waiting), "failed_days": failed_days,
            "bulk": {k: {"count": v[0], "bytes": v[1]} for k, v in bulk.items()}}


def request(conn, worker_id, ids=None, which=None, failed_days=DEFAULT_FAILED_DAYS):
    """Queues the deletion of job folders: the given ids, or everything a bulk action ('finished' or 'failed_old') covers. Returns {'count', 'bytes'}."""
    view = overview(conn, worker_id, failed_days)
    if not view["known"]:
        raise WorkDriveError("This worker has not measured its work folder yet. Wait a few minutes and reload.")
    items = {i["job_id"]: i for i in view["jobs"]}
    if which:
        if which not in ("finished", "failed_old"):
            raise WorkDriveError("Unknown bulk action.")
        rows = _job_rows(conn, list(items))
        chosen = [i for i in items.values() if i["can_delete"] and _bulk_kind(i, rows.get(i["job_id"]), failed_days) == which]
    else:
        wanted = [i for i in (ids or []) if isinstance(i, int)]
        if not wanted:
            raise WorkDriveError("Choose at least one job folder.")
        chosen = []
        for job_id in wanted:
            item = items.get(job_id)
            if item is None:
                raise WorkDriveError(f"Job {job_id} is not in this worker's list any more. Reload the page.")
            if not item["can_delete"]:
                raise WorkDriveError(f"Job {job_id} cannot be deleted now: {item['why_not']}")
            chosen.append(item)
    if not chosen:
        raise WorkDriveError("There is nothing to delete.")
    _save_pending(conn, worker_id, pending(conn, worker_id) + [i["job_id"] for i in chosen])
    return {"count": len(chosen), "bytes": sum(i["bytes"] for i in chosen)}
