"""Pages for the worker fleet: the Workers page, its live fragment and the actions behind its buttons."""
from flask import jsonify, render_template, request

from synchub import db, jobs, storage, webhooks, workers

# What each worker button does: setting, new value.
WORKER_ACTIONS = {"pause": ("paused", True), "resume": ("paused", False), "drain": ("draining", True),
                  "undrain": ("draining", False), "enable": ("enabled", True), "disable": ("enabled", False)}


def _gb(value):
    return f"{value / 1024 ** 3:,.1f} GB" if isinstance(value, (int, float)) else None


# What a worker reports for a share, and how the page shows it.
ROOT_STATES = {"ok": ("ready", "success"), "not_mounted": ("not mounted", "danger"), "no_marker": ("no marker file", "danger"),
               "wrong_marker": ("wrong share", "danger"), "read_only": ("read-only", "warning"), "error": ("error", "danger")}


def describe_roots(w, names):
    """One entry per share the site defines: what this worker says about it (or that it has not said)."""
    reported = w["status"].get("roots") if isinstance(w["status"].get("roots"), dict) else {}
    out = []
    for name in names:
        info = reported.get(name) if isinstance(reported.get(name), dict) else None
        if info is None:
            out.append({"name": name, "label": "not reported", "color": "secondary", "message": None, "free": None})
            continue
        label, color = ROOT_STATES["ok"] if info.get("ok") is True else ROOT_STATES.get(info.get("state"), (str(info.get("state") or "unknown")[:24], "secondary"))
        out.append({"name": name, "label": label, "color": color, "message": str(info.get("message") or "")[:300] or None, "free": _gb(info.get("free_bytes"))})
    return out


def describe_worker(w, root_names=()):
    """The worker plus what the page shows: a state, encoders, space, its shares and when it was last heard from."""
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
             seen=webhooks.ago(w["last_seen"]) if w["last_seen"] else "never", roots=describe_roots(w, root_names))
    return w


def describe_job(j):
    j["percent"] = None if j["progress"] is None else round(j["progress"] * 100)
    j["eta"] = None
    if j["eta_seconds"]:
        minutes = j["eta_seconds"] // 60
        j["eta"] = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{max(minutes, 1)} min"
    j["updated"] = webhooks.ago(j["updated_at"]) if j["updated_at"] else ""
    return j


def _waiting_for(job, able):
    """Why a queued job is not being picked up: a share no online worker has ready."""
    if job["status"] != "queued" or not able:
        return None
    wanted = [r for r in dict.fromkeys((job["source_root"], job["dest_root"])) if r]
    missing = [r for r in wanted if not any(jobs.roots_ready(w, {"source_root": r, "dest_root": None}) for w in able)]
    if missing:
        return f"No online worker has {', '.join(missing)} mounted and ready."
    if wanted and not any(jobs.roots_ready(w, job) for w in able):
        return "No single online worker has all of its shares ready."
    return None


def snapshot(conn):
    counts = jobs.counts(conn)
    summary = " \u00b7 ".join(f"{counts[s]} {s.replace('_', ' ')}" for s in ("running", "needs_review", "queued", "done", "failed") if counts.get(s))
    root_names = [r["name"] for r in storage.list_all(conn)]
    raw = workers.list_all(conn)
    able = [w for w in raw if w["enabled"] and not w["revoked"] and w["online"]]
    listed = [describe_job(j) for j in jobs.recent(conn, 50)]
    for j in listed:
        j["waiting_for"] = _waiting_for(j, able)
    return {"workers": [describe_worker(w, root_names) for w in raw], "jobs": listed, "counts": counts, "summary": summary, "root_names": root_names}


def _setup(conn, name, token=None):
    """The setup files for a worker. Right after a token is made they hold it; afterwards a placeholder, since the token cannot be read again."""
    url = request.host_url.rstrip("/")
    files = storage.setup_files(name, url, token or "<the token shown when this worker was added>", storage.list_all(conn))
    return {"url": url, "files": files, "order": list(storage.SETUP_ORDER), "has_token": bool(token), "shares": len(storage.list_all(conn))}


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
        with db.connection() as conn:
            setup = _setup(conn, name, token)
        return jsonify(ok=True, id=worker_id, token=token, config=config, setup=setup, message=f"Added {name}. Copy its token now: it is not shown again.")

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
                    return jsonify(ok=True, token=token, config=config, setup=_setup(conn, name, token), message="New token. The old one no longer works; copy this one now.")
                if action == "revoke":
                    workers.revoke(conn, worker_id)
                    return jsonify(ok=True, message="Token revoked. The worker's history stays; rotate the token to let it back in.")
                if action == "delete":
                    workers.delete(conn, worker_id)
                    return jsonify(ok=True, message="Worker removed.")
        except workers.WorkerError as exc:
            return _fail(str(exc))
        return _fail("Unknown action.", 404)

    @app.route("/api/workers/<int:worker_id>/setup")
    def api_worker_setup(worker_id):
        with db.connection() as conn:
            worker = workers.get(conn, worker_id)
            if worker is None:
                return _fail("That worker no longer exists.", 404)
            return jsonify(ok=True, name=worker["name"], setup=_setup(conn, worker["name"]))

    @app.route("/api/jobs/test-copy", methods=["POST"])
    def api_test_copy():
        data = request.get_json(silent=True) or {}
        try:
            worker_id = int(data["worker_id"]) if str(data.get("worker_id") or "").isdigit() else None
            with db.connection() as conn:
                job_id = storage.queue_test_copy(conn, str(data.get("source_root") or "").strip(), data.get("source_rel"), str(data.get("dest_root") or "").strip(),
                                                 data.get("dest_rel"), worker_id)
        except storage.StorageError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, id=job_id, message=f"Queued test copy #{job_id}. A worker picks it up within seconds.")

    @app.route("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id):
        with db.connection() as conn:
            if jobs.get(conn, job_id) is None:
                return _fail("That job no longer exists.", 404)
            log = jobs.stored_log(conn, job_id)
            holder_id = jobs.log_holder(conn, job_id)
            holder = workers.get(conn, holder_id) if holder_id else None
            pending = bool(conn.execute("SELECT log_requested FROM job WHERE id = ?", (job_id,)).fetchone()[0])
        if log:
            log["received"] = webhooks.ago(log["received_at"])
        return jsonify(ok=True, log=log, pending=pending, holder=holder["name"] if holder else None, holder_online=bool(holder and holder["online"]))

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
                if action == "log":
                    holder = workers.get(conn, jobs.request_log(conn, job_id))
                    return jsonify(ok=True, holder=holder["name"] if holder else None, holder_online=bool(holder and holder["online"]),
                                   message="Asked the worker for the log." if holder and holder["online"] else "Asked. The worker is offline, so the log arrives when it is back.")
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
