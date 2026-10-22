"""The job queue: creating jobs, handing them to workers, following them and settling them.

A job is named by logical roots and relative paths, never by a machine's path. A worker *claims* a job (an atomic update, so two
workers can never get the same one) and keeps a lease alive by reporting progress; a job whose worker goes silent returns to the
queue. What can be retried, what waits for a person and what has failed is decided here, not in the worker.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from synchub import db

logger = logging.getLogger(__name__)

KINDS = ("scan", "library", "download", "preview")
PRIORITY = {"preview": 120, "download": 100, "scan": 50, "library": 10}      # someone waiting beats a backlog
STAGES = ("claimed", "copy_in", "analyze", "plan", "preview", "encode", "verify", "copy_out", "publish", "scan", "done")
TERMINAL = ("done", "failed", "cancelled", "skipped")

LEASE_SECONDS = 90                 # a claimed job whose worker is silent this long goes back to the queue
AFFINITY_SECONDS = 30 * 60         # the worker that holds a job's files gets this long to take it first
RECONCILE_GRACE_SECONDS = 30       # a job just handed out is not "missing" from a worker's list yet
BACKOFF_SECONDS = (60, 300, 900, 3600)

# What happens after a failure, by the error class the worker reports:
#   retry              try again (back-off) until max_attempts, then failed
#   retry_then_review  try again until `limit` attempts, then wait for a person
#   review             wait for a person at once
CLASS_POLICY = {
    "transient": ("retry", None),                  # share offline, network drop, disk full
    "worker_lost": ("retry", None),
    "source_changed": ("retry_then_review", 2),
    "verification_failed": ("retry_then_review", 2),
    "size_target_missed": ("retry_then_review", 2),
    "encode_failed": ("review", None),
}
NO_BACKOFF = {"worker_lost", "source_changed", "verification_failed", "size_target_missed"}


class JobError(Exception):
    """An operation that cannot be done in the job's current state; the message is meant for the user."""


class JobConflict(JobError):
    """A worker reported on a job that is no longer its own (cancelled, re-queued, taken by another worker)."""


def _later(now, seconds):
    return (datetime.strptime(now, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _dump(value):
    return json.dumps(value if value is not None else {})


def _load(text, default):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def row(r):
    """A job as a dict with its JSON columns decoded."""
    job = dict(r)
    for key, default in (("plan", {}), ("policy", {}), ("review_detail", {}), ("decision", {}), ("result", {}), ("required_tags", [])):
        job[key] = _load(job.get(key), default)
    return job


def get(conn, job_id):
    r = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    return row(r) if r else None


# ---------------------------------------------------------------- creating
def create(conn, kind, title=None, ref=None, source=None, dest=None, plan=None, policy=None, priority=None, max_attempts=3,
           pinned_worker_id=None, required_tags=(), need_bytes=None, now=None):
    """Queues a job. `ref` is (type, id); `source` and `dest` are (root, relative path). Returns the job id."""
    if kind not in KINDS:
        raise JobError(f"Unknown job kind '{kind}'.")
    ref_type, ref_id = ref or (None, None)
    (source_root, source_rel), (dest_root, dest_rel) = source or (None, None), dest or (None, None)
    now = now or db.now()
    return conn.execute(
        """INSERT INTO job (kind, title, ref_type, ref_id, source_root, source_rel, dest_root, dest_rel, plan, policy, priority, max_attempts,
                            pinned_worker_id, required_tags, need_bytes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, title, ref_type, ref_id, source_root, source_rel, dest_root, dest_rel, _dump(plan), _dump(policy),
         PRIORITY[kind] if priority is None else int(priority), max(1, int(max_attempts)), pinned_worker_id,
         json.dumps(sorted(set(required_tags))), need_bytes, now, now)).lastrowid


# ---------------------------------------------------------------- handing out
def payload(job):
    """What a worker is told about a job it claimed."""
    return {"id": job["id"], "kind": job["kind"], "title": job["title"], "attempt": job["attempt_no"],
            "plan": job["plan"], "policy": job["policy"], "decision": job["decision"] or None,
            "source": {"root": job["source_root"], "rel": job["source_rel"]} if job["source_root"] is not None else None,
            "dest": {"root": job["dest_root"], "rel": job["dest_rel"]} if job["dest_root"] is not None else None}


def _next_attempt_no(conn, job_id):
    return (conn.execute("SELECT MAX(attempt_no) FROM job_attempt WHERE job_id = ?", (job_id,)).fetchone()[0] or 0) + 1


def claim(conn, worker, free_slots, free_bytes=None, now=None):
    """Gives the worker up to `free_slots` queued jobs it is allowed and able to run. A disabled, paused or draining worker gets none."""
    if not worker["enabled"] or worker["paused"] or worker["draining"]:
        return []
    now = now or db.now()
    busy = conn.execute("SELECT COUNT(*) FROM job WHERE worker_id = ? AND status = 'running'", (worker["id"],)).fetchone()[0]
    room = min(int(free_slots or 0), worker["slots"] - busy)
    if room <= 0:
        return []
    tags = set(worker["tags"])
    claimed = []
    candidates = conn.execute(
        """SELECT * FROM job WHERE status = 'queued' AND cancel_requested = 0 AND (not_before IS NULL OR not_before <= ?)
           AND (pinned_worker_id IS NULL OR pinned_worker_id = ?) ORDER BY priority DESC, id""", (now, worker["id"])).fetchall()
    for r in candidates:
        if len(claimed) >= room:
            break
        job = row(r)
        if not set(job["required_tags"]) <= tags:
            continue
        if job["need_bytes"] and free_bytes is not None and job["need_bytes"] > free_bytes:
            continue
        holder = job["preferred_worker_id"]
        if holder and holder != worker["id"] and job["preferred_until"] and job["preferred_until"] > now:
            continue                                   # another worker holds this job's files and has first call
        attempt_no = _next_attempt_no(conn, job["id"])
        won = conn.execute(
            """UPDATE job SET status = 'running', worker_id = ?, attempts = attempts + 1, lease_until = ?, claimed_at = ?,
                      started_at = COALESCE(started_at, ?), stage = 'claimed', progress = NULL, fps = NULL, speed = NULL, eta_seconds = NULL,
                      message = NULL, cleanup = 0, updated_at = ?
               WHERE id = ? AND status = 'queued'""",
            (worker["id"], _later(now, LEASE_SECONDS), now, now, now, job["id"])).rowcount == 1
        if not won:
            continue                                   # another worker took it between the read and the update
        conn.execute("INSERT INTO job_attempt (job_id, attempt_no, worker_id, started_at) VALUES (?, ?, ?, ?)", (job["id"], attempt_no, worker["id"], now))
        job.update(attempt_no=attempt_no)
        claimed.append(payload(job))
    return claimed


def running_for(conn, worker_id):
    return [r["id"] for r in conn.execute("SELECT id FROM job WHERE worker_id = ? AND status = 'running'", (worker_id,))]


def cancels_for(conn, worker_id):
    return [r["id"] for r in conn.execute("SELECT id FROM job WHERE worker_id = ? AND status = 'running' AND cancel_requested = 1", (worker_id,))]


def cleanups_for(conn, worker_id):
    """Finished or abandoned jobs whose local files the worker may now delete."""
    return [r["id"] for r in conn.execute("SELECT id FROM job WHERE preferred_worker_id = ? AND cleanup = 1", (worker_id,))]


def cleaned(conn, job_id, worker_id):
    conn.execute("UPDATE job SET cleanup = 0, preferred_worker_id = NULL, preferred_until = NULL WHERE id = ? AND preferred_worker_id = ?", (job_id, worker_id))


# ---------------------------------------------------------------- what a worker reports
def _owned(conn, job_id, worker_id):
    job = get(conn, job_id)
    if job is None:
        raise JobConflict("That job no longer exists.")
    if job["status"] != "running" or job["worker_id"] != worker_id:
        raise JobConflict(f"That job is no longer yours (it is {job['status']}). Stop working on it.")
    return job


def progress(conn, job_id, worker_id, stage=None, progress=None, fps=None, speed=None, eta_seconds=None, message=None, now=None):
    """Heartbeat and progress in one. Returns whether a cancel was requested and the lease length."""
    job = _owned(conn, job_id, worker_id)
    now = now or db.now()
    if stage is not None and stage not in STAGES:
        raise JobError(f"Unknown stage '{stage}'.")

    def number(value, low=None, high=None):
        if value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise JobError("Progress values must be numbers.")
        if value != value:                                              # NaN
            return None
        return min(max(value, low), high) if low is not None else value

    conn.execute("UPDATE job SET stage = COALESCE(?, stage), progress = COALESCE(?, progress), fps = ?, speed = ?, eta_seconds = ?, "
                 "message = COALESCE(?, message), lease_until = ?, updated_at = ? WHERE id = ?",
                 (stage, number(progress, 0.0, 1.0), number(fps), number(speed), None if eta_seconds is None else int(number(eta_seconds, 0, 10 ** 9)),
                  None if message is None else str(message)[:300], _later(now, LEASE_SECONDS), now, job_id))
    return {"cancel": bool(job["cancel_requested"]), "lease_seconds": LEASE_SECONDS}


def _close_attempt(conn, job_id, outcome, error_class=None, error=None, command=None, log_tail=None, now=None):
    conn.execute("UPDATE job_attempt SET finished_at = ?, outcome = ?, error_class = ?, error = ?, command = COALESCE(?, command), log_tail = COALESCE(?, log_tail) "
                 "WHERE job_id = ? AND finished_at IS NULL",
                 (now or db.now(), outcome, error_class, None if error is None else str(error)[:2000], command, None if log_tail is None else str(log_tail)[-8000:], job_id))


def complete(conn, job_id, worker_id, result=None, command=None, now=None):
    _owned(conn, job_id, worker_id)
    now = now or db.now()
    _close_attempt(conn, job_id, "done", command=command, now=now)
    conn.execute("UPDATE job SET status = 'done', stage = 'done', progress = 1.0, eta_seconds = 0, result = ?, lease_until = NULL, error_class = NULL, error = NULL, "
                 "review_reason = NULL, finished_at = ?, updated_at = ? WHERE id = ?", (_dump(result if isinstance(result, dict) else {}), now, now, job_id))


def _requeue(conn, job, now, holder=None, back_off=None, message=None):
    conn.execute("UPDATE job SET status = 'queued', worker_id = NULL, lease_until = NULL, not_before = ?, preferred_worker_id = ?, preferred_until = ?, "
                 "message = ?, stage = NULL, updated_at = ? WHERE id = ?",
                 (_later(now, back_off) if back_off else None, holder, _later(now, AFFINITY_SECONDS) if holder else None, message, now, job["id"]))


def _to_review(conn, job, reason, detail, holder, now):
    conn.execute("UPDATE job SET status = 'needs_review', worker_id = NULL, lease_until = NULL, preferred_worker_id = ?, preferred_until = NULL, "
                 "review_reason = ?, review_detail = ?, stage = COALESCE(stage, 'plan'), updated_at = ? WHERE id = ?",
                 (holder, str(reason)[:500], _dump(detail), now, job["id"]))


def _after_failure(attempts, max_attempts, error_class, retryable):
    mode, limit = CLASS_POLICY.get(error_class, ("retry" if retryable else "fail", None))
    if retryable is False and mode != "review":
        return "review" if mode == "retry_then_review" else "failed"
    if mode == "review":
        return "review"
    if mode == "fail":
        return "failed"
    if mode == "retry_then_review":
        return "retry" if attempts < min(limit, max_attempts) else "review"
    return "retry" if attempts < max_attempts else "failed"


def fail(conn, job_id, worker_id, error_class, message, retryable=None, log_tail=None, command=None, keep_state=True, now=None):
    """A worker gave up on a job. Returns what became of it: 'cancelled', 'queued', 'needs_review' or 'failed'."""
    job = _owned(conn, job_id, worker_id)
    now = now or db.now()
    error_class = str(error_class or "transient")[:40]
    holder = worker_id if keep_state else None
    _close_attempt(conn, job_id, "failed", error_class, message, command, log_tail, now)
    if job["cancel_requested"]:
        conn.execute("UPDATE job SET status = 'cancelled', lease_until = NULL, cleanup = ?, preferred_worker_id = ?, finished_at = ?, updated_at = ?, message = 'Cancelled.' WHERE id = ?",
                     (1 if holder else 0, holder, now, now, job_id))
        return "cancelled"
    outcome = _after_failure(job["attempts"], job["max_attempts"], error_class, retryable)
    conn.execute("UPDATE job SET error_class = ?, error = ? WHERE id = ?", (error_class, str(message)[:2000], job_id))
    if outcome == "retry":
        back_off = None if error_class in NO_BACKOFF else BACKOFF_SECONDS[min(job["attempts"] - 1, len(BACKOFF_SECONDS) - 1)]
        _requeue(conn, job, now, holder, back_off, f"Retrying after: {str(message)[:200]} (attempt {job['attempts']} of {job['max_attempts']} failed)")
    elif outcome == "review":
        _to_review(conn, job, f"{error_class}: {message}", {"error_class": error_class, "log_tail": str(log_tail or "")[-2000:]}, holder, now)
        conn.execute("UPDATE job SET message = 'Waiting for your decision.' WHERE id = ?", (job_id,))
    else:
        conn.execute("UPDATE job SET status = 'failed', lease_until = NULL, finished_at = ?, updated_at = ?, message = ? WHERE id = ?",
                     (now, now, str(message)[:300], job_id))
    return {"retry": "queued", "review": "needs_review", "failed": "failed"}[outcome]


def needs_review(conn, job_id, worker_id, reason, detail=None, keep_state=True, now=None):
    """A worker found something it cannot decide. The slot is freed; the job waits for a person."""
    _owned(conn, job_id, worker_id)
    now = now or db.now()
    _close_attempt(conn, job_id, "needs_review", error=reason, now=now)
    _to_review(conn, {"id": job_id}, reason, detail if isinstance(detail, dict) else {}, worker_id if keep_state else None, now)
    conn.execute("UPDATE job SET message = 'Waiting for your decision.' WHERE id = ?", (job_id,))


# ---------------------------------------------------------------- what people do
def _stamp(action, by, note=None, changes=None, now=None):
    return {"action": action, "by": by or "someone", "note": (note or "").strip()[:500] or None, "changes": changes or None, "at": now or db.now()}


def decide(conn, job_id, action, by=None, note=None, changes=None, now=None):
    """A person's answer for a job that is waiting for review: approve, approve_changes, retry, skip or reject."""
    job = get(conn, job_id)
    if job is None:
        raise JobError("That job no longer exists.")
    if job["status"] != "needs_review":
        raise JobError("This job is not waiting for a decision.")
    if action not in ("approve", "approve_changes", "retry", "skip", "reject"):
        raise JobError("Unknown decision.")
    if action == "approve_changes" and not (isinstance(changes, dict) and changes):
        raise JobError("Say what to change.")
    now = now or db.now()
    decision = _stamp(action, by, note, changes if action == "approve_changes" else None, now)
    holder = job["preferred_worker_id"]
    if action in ("approve", "approve_changes", "retry"):
        conn.execute("UPDATE job SET status = 'queued', decision = ?, review_reason = ?, attempts = CASE WHEN ? = 'retry' THEN 0 ELSE attempts END, "
                     "error_class = NULL, error = NULL, not_before = NULL, preferred_until = ?, message = ?, updated_at = ? WHERE id = ?",
                     (_dump(decision), job["review_reason"], action, _later(now, AFFINITY_SECONDS) if holder else None,
                      "Approved: waiting for a worker." if action != "retry" else "Retrying.", now, job_id))
        return "queued"
    status = "skipped" if action == "skip" else "cancelled"
    conn.execute("UPDATE job SET status = ?, decision = ?, cleanup = ?, finished_at = ?, updated_at = ?, message = ? WHERE id = ?",
                 (status, _dump(decision), 1 if holder else 0, now, now, "Skipped: the file is left as it is." if action == "skip" else "Rejected.", job_id))
    return status


def cancel(conn, job_id, by=None, now=None):
    """Cancels a job. A queued or waiting job stops at once; a running one is told to stop at its next report."""
    job = get(conn, job_id)
    if job is None:
        raise JobError("That job no longer exists.")
    if job["status"] in TERMINAL:
        raise JobError(f"This job is already {job['status']}.")
    now = now or db.now()
    if job["status"] == "running":
        conn.execute("UPDATE job SET cancel_requested = 1, message = 'Cancelling...', decision = ?, updated_at = ? WHERE id = ?", (_dump(_stamp("cancel", by, now=now)), now, job_id))
        return "cancelling"
    holder = job["preferred_worker_id"]
    conn.execute("UPDATE job SET status = 'cancelled', decision = ?, cleanup = ?, finished_at = ?, updated_at = ?, message = 'Cancelled.' WHERE id = ?",
                 (_dump(_stamp("cancel", by, now=now)), 1 if holder else 0, now, now, job_id))
    return "cancelled"


def retry(conn, job_id, by=None, now=None):
    """Puts a failed, cancelled or skipped job back in the queue with a fresh set of attempts."""
    job = get(conn, job_id)
    if job is None:
        raise JobError("That job no longer exists.")
    if job["status"] not in ("failed", "cancelled", "skipped"):
        raise JobError(f"Only a failed, cancelled or skipped job can be retried (this one is {job['status']}).")
    now = now or db.now()
    holder = job["preferred_worker_id"]
    conn.execute("UPDATE job SET status = 'queued', attempts = 0, cancel_requested = 0, cleanup = 0, error_class = NULL, error = NULL, not_before = NULL, "
                 "finished_at = NULL, worker_id = NULL, lease_until = NULL, decision = ?, preferred_until = ?, message = 'Retrying.', updated_at = ? WHERE id = ?",
                 (_dump(_stamp("retry", by, now=now)), _later(now, AFFINITY_SECONDS) if holder else None, now, job_id))


# ---------------------------------------------------------------- keeping the queue honest
def _lost(conn, job, now, why):
    _close_attempt(conn, job["id"], "worker_lost", "worker_lost", why, now=now)
    if job["cancel_requested"]:
        conn.execute("UPDATE job SET status = 'cancelled', lease_until = NULL, finished_at = ?, updated_at = ?, message = 'Cancelled.' WHERE id = ?", (now, now, job["id"]))
    elif job["attempts"] < job["max_attempts"]:
        conn.execute("UPDATE job SET error_class = 'worker_lost', error = ? WHERE id = ?", (why, job["id"]))
        _requeue(conn, job, now, holder=None, message=f"Re-queued: {why}")
    else:
        conn.execute("UPDATE job SET status = 'failed', lease_until = NULL, error_class = 'worker_lost', error = ?, finished_at = ?, updated_at = ?, message = ? WHERE id = ?",
                     (why, now, now, why, job["id"]))


def reap(conn, now=None):
    """Takes back jobs whose lease ran out (the worker is silent). A job that may be resumed keeps its worker's first call on it.
    Returns the ids of the jobs touched."""
    now = now or db.now()
    touched = []
    for r in conn.execute("SELECT * FROM job WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until < ?", (now,)).fetchall():
        job = row(r)
        _lost(conn, job, now, f"no word from the worker for over {LEASE_SECONDS} seconds")
        if job["attempts"] < job["max_attempts"] and not job["cancel_requested"]:
            conn.execute("UPDATE job SET preferred_worker_id = ?, preferred_until = ? WHERE id = ?", (job["worker_id"], _later(now, AFFINITY_SECONDS), job["id"]))
        touched.append(job["id"])
    if touched:
        logger.warning(f"Re-queued or closed {len(touched)} job(s) whose worker went silent: {touched}")
    return touched


def reconcile(conn, worker_id, holding, now=None):
    """A worker lists the jobs it still has files for. A job SyncHub thinks it is running, that the worker does not list, is
    lost (a restart that did not keep it). Jobs handed out in the last few seconds are left alone. Returns the ids taken back."""
    now = now or db.now()
    holding = {int(i) for i in holding or [] if str(i).lstrip("-").isdigit()}
    cutoff = _later(now, -RECONCILE_GRACE_SECONDS)
    taken = []
    for r in conn.execute("SELECT * FROM job WHERE worker_id = ? AND status = 'running' AND claimed_at <= ?", (worker_id, cutoff)).fetchall():
        if r["id"] not in holding:
            _lost(conn, row(r), now, "the worker no longer has this job")
            taken.append(r["id"])
    return taken


# ---------------------------------------------------------------- for pages
def recent(conn, limit=50, kind=None, status=None):
    where, params = ["1 = 1"], []
    if kind:
        where.append("j.kind = ?")
        params.append(kind)
    if status:
        where.append("j.status = ?")
        params.append(status)
    sql = f"""SELECT j.*, w.name AS worker_name, h.name AS holder_name FROM job j LEFT JOIN worker w ON w.id = j.worker_id
              LEFT JOIN worker h ON h.id = j.preferred_worker_id WHERE {' AND '.join(where)}
              ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'needs_review' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END, j.updated_at DESC, j.id DESC LIMIT ?"""
    return [row(r) for r in conn.execute(sql, (*params, int(limit)))]


def detail(conn, job_id):
    job = get(conn, job_id)
    if job is None:
        return None
    job["attempt_history"] = [dict(r) for r in conn.execute(
        "SELECT a.*, w.name AS worker_name FROM job_attempt a LEFT JOIN worker w ON w.id = a.worker_id WHERE a.job_id = ? ORDER BY a.attempt_no", (job_id,))]
    return job


def counts(conn):
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM job GROUP BY status")}
