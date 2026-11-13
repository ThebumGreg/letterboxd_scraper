"""The originals workers keep after replacing a file in place: what is held where, and the jobs that restore one, keep it longer or delete it now.

A worker reports the list with its poll (workers.originals). Each action is a `library` job pinned to the worker that holds the file, with
`plan.op = "originals"`, so it is logged, cancellable and shows on the Workers page like any other job.
"""
from datetime import datetime, timezone

from synchub import jobs, workers

REF = "original"
ACTIONS = ("restore", "extend", "purge")
OPEN = ("queued", "running", "needs_review")


class OriginalsError(Exception):
    """The action cannot be done; the message says why in plain words."""


def _days_left(expires_at, now=None):
    try:
        until = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return round((until - (now or datetime.now(timezone.utc))).total_seconds() / 86400, 1)


def _open_actions(conn):
    """{(worker id, folder): action} for the originals jobs that are still going."""
    out = {}
    for j in conn.execute("SELECT j.id, j.status, j.plan, j.pinned_worker_id FROM job j WHERE j.ref_type = ? AND j.status IN ('queued', 'running', 'needs_review')", (REF,)):
        plan = jobs._load(j["plan"], {})
        out[(j["pinned_worker_id"], plan.get("original"))] = {"action": plan.get("action"), "status": j["status"], "job": j["id"]}
    return out


def listing(conn, now=None):
    """Every original held by any worker, newest first, with the worker's name and what is already being done to it."""
    pending, out, held = _open_actions(conn), [], 0
    for w in workers.list_all(conn):
        for item in w["originals"]:
            entry = dict(item, worker_id=w["id"], worker=w["name"], online=w["online"], days_left=_days_left(item.get("expires_at"), now), pending=pending.get((w["id"], item["id"])))
            held += int(item.get("size") or 0)
            out.append(entry)
    out.sort(key=lambda e: e.get("saved_at") or "", reverse=True)
    return {"items": out, "bytes": held, "workers": [w["name"] for w in workers.list_all(conn) if w["originals"]]}


def conversions(conn):
    """Library conversions (a copy converted onto another share) whose original is still on its source share: the cleanup list for when the conversions are trusted.
    Each row says whether the original is waiting for its timer, kept on purpose, being deleted, or failed."""
    rows = conn.execute("SELECT id, root_name, relative_path, file_name, size_bytes, migration_destination_path, migrated_dest_size, source_delete_status, source_delete_due_at, source_delete_message "
                        "FROM library_file WHERE migration_status = 'completed' AND migrated_how = 'convert' AND source_deleted_at IS NULL "
                        "AND COALESCE(source_delete_status, '') != 'deleted' AND migration_destination_path IS NOT NULL ORDER BY id DESC LIMIT 500").fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if r["migration_destination_path"] == f"{r['root_name']}:{r['relative_path']}":
            continue
        out.append(r)
    return {"items": out, "bytes": sum(int(r["size_bytes"] or 0) for r in out)}


def queue(conn, worker_id, folder, action, days=None, now=None):
    """Queues one action for one original. Returns the job id."""
    if action not in ACTIONS:
        raise OriginalsError("Unknown action.")
    worker = workers.get(conn, worker_id)
    item = next((i for i in (worker["originals"] if worker else []) if i.get("id") == folder), None)
    if item is None:
        raise OriginalsError("That worker does not list this original any more. Reload the page.")
    if (worker_id, folder) in _open_actions(conn):
        raise OriginalsError("Something is already queued for this original.")
    plan = {"op": "originals", "action": action, "original": folder}
    source = dest = None
    if action == "restore":
        if item.get("kind") != "original" or not item.get("root") or not item.get("rel") or not item.get("replaced_with"):
            raise OriginalsError("This one cannot be restored: it is not a kept original, or its original location is not known.")
        source, dest = (item["root"], item["replaced_with"]), (item["root"], item["rel"])
    if action == "extend":
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= 3650:
            raise OriginalsError("Keep it for between 1 and 3650 days.")
        plan["days"] = days
    title = {"restore": "Restore original", "extend": "Keep original longer", "purge": "Delete original"}[action] + f": {item.get('name') or folder}"
    return jobs.create(conn, "library", title=title, ref=(REF, worker_id), source=source, dest=dest, plan=plan, pinned_worker_id=worker_id, need_bytes=0, now=now)
