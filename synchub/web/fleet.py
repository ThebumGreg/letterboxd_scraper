"""Pages for the worker fleet: the Workers page, its live fragment and the actions behind its buttons."""
from flask import jsonify, render_template, request

from synchub import db, jobs, webhooks, workers

# What each worker button does: setting, new value.
WORKER_ACTIONS = {"pause": ("paused", True), "resume": ("paused", False), "drain": ("draining", True),
                  "undrain": ("draining", False), "enable": ("enabled", True), "disable": ("enabled", False)}


def _gb(value):
    return f"{value / 1024 ** 3:,.1f} GB" if isinstance(value, (int, float)) else None


def describe_worker(w):
    """The worker plus what the page shows: a state, encoders, space and when it was last heard from."""
    if w["revoked"]:
        state, color = "Revoked", "danger"
    elif not w["enabled"]:
        state, color = "Disabled", "secondary"
    elif not w["online"]:
        state, color = "Offline", "secondary"
    elif w["paused"]:
        state, color = "Paused", "warning"
    elif w["draining"]:
        state, color = "Draining", "info"
    else:
        state, color = "Online", "success"
    status = w["status"]
    held = status.get("originals_count")
    w.update(state=state, state_color=color, encoders=", ".join(str(e) for e in w["capabilities"].get("encoders") or []),
             free=_gb(status.get("work_free_bytes")), originals=(f"{held} held, {_gb(status.get('originals_bytes')) or '?'}" if held else None),
             seen=webhooks.ago(w["last_seen"]) if w["last_seen"] else "never")
    return w


def describe_job(j):
    j["percent"] = None if j["progress"] is None else round(j["progress"] * 100)
    j["eta"] = None
    if j["eta_seconds"]:
        minutes = j["eta_seconds"] // 60
        j["eta"] = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{max(minutes, 1)} min"
    j["updated"] = webhooks.ago(j["updated_at"]) if j["updated_at"] else ""
    return j


def snapshot(conn):
    counts = jobs.counts(conn)
    summary = " \u00b7 ".join(f"{counts[s]} {s.replace('_', ' ')}" for s in ("running", "needs_review", "queued", "done", "failed") if counts.get(s))
    return {"workers": [describe_worker(w) for w in workers.list_all(conn)], "jobs": [describe_job(j) for j in jobs.recent(conn, 50)], "counts": counts, "summary": summary}


def _who(conn):
    from synchub.web import current_profile
    profile = current_profile(conn)
    return profile["name"] if profile else "guest"


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def register(app):
    @app.route("/workers")
    def workers_page():
        with db.connection() as conn:
            data = snapshot(conn)
        return render_template("workers.html", **data)

    @app.route("/workers/fragment")
    def workers_fragment():
        with db.connection() as conn:
            data = snapshot(conn)
        return render_template("_fleet.html", **data)

    @app.route("/api/workers")
    def api_workers():
        with db.connection() as conn:
            data = snapshot(conn)
        return jsonify(ok=True, **data)

    @app.route("/api/workers", methods=["POST"])
    def api_worker_create():
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                worker_id, token = workers.create(conn, data.get("name"), data.get("tags") or [], data.get("slots") or 1)
        except workers.WorkerError as exc:
            return _fail(str(exc))
        name = " ".join(str(data.get("name")).split())
        config = f"[synchub]\nurl = {request.host_url.rstrip('/')}\ntoken = {token}\nname = {name}\n"
        return jsonify(ok=True, id=worker_id, token=token, config=config, message=f"Added {name}. Copy its token now: it is not shown again.")

    @app.route("/api/workers/<int:worker_id>/update", methods=["POST"])
    def api_worker_update(worker_id):
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                workers.update(conn, worker_id, name=data.get("name"), tags=data.get("tags"), slots=data.get("slots"))
        except workers.WorkerError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Saved.")

    @app.route("/api/workers/<int:worker_id>/<action>", methods=["POST"])
    def api_worker_action(worker_id, action):
        try:
            with db.connection() as conn:
                if action in WORKER_ACTIONS:
                    workers.set_flag(conn, worker_id, *WORKER_ACTIONS[action])
                    return jsonify(ok=True, message="Done.")
                if action == "rotate":
                    token = workers.rotate_token(conn, worker_id)
                    name = workers.get(conn, worker_id)["name"]
                    config = f"[synchub]\nurl = {request.host_url.rstrip('/')}\ntoken = {token}\nname = {name}\n"
                    return jsonify(ok=True, token=token, config=config, message="New token. The old one no longer works; copy this one now.")
                if action == "revoke":
                    workers.revoke(conn, worker_id)
                    return jsonify(ok=True, message="Token revoked. The worker's history stays; rotate the token to let it back in.")
                if action == "delete":
                    workers.delete(conn, worker_id)
                    return jsonify(ok=True, message="Worker removed.")
        except workers.WorkerError as exc:
            return _fail(str(exc))
        return _fail("Unknown action.", 404)

    @app.route("/api/jobs/<int:job_id>")
    def api_job(job_id):
        with db.connection() as conn:
            job = jobs.detail(conn, job_id)
        return jsonify(ok=True, job=describe_job(job)) if job else _fail("That job no longer exists.", 404)

    @app.route("/api/jobs/<int:job_id>/<action>", methods=["POST"])
    def api_job_action(job_id, action):
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                by = _who(conn)
                if action == "cancel":
                    result = jobs.cancel(conn, job_id, by)
                    return jsonify(ok=True, message="Cancelling: the worker stops at its next report." if result == "cancelling" else "Cancelled.")
                if action == "retry":
                    job = jobs.get(conn, job_id)
                    if job and job["status"] == "needs_review":
                        jobs.decide(conn, job_id, "retry", by, data.get("note"))
                    else:
                        jobs.retry(conn, job_id, by)
                    return jsonify(ok=True, message="Queued again.")
                if action in ("approve", "skip", "reject"):
                    jobs.decide(conn, job_id, action, by, data.get("note"))
                    return jsonify(ok=True, message={"approve": "Approved: it carries on.", "skip": "Skipped: the file is left as it is.", "reject": "Rejected."}[action])
        except jobs.JobError as exc:
            return _fail(str(exc))
        return _fail("Unknown action.", 404)
