"""The worker API: what an agent on a worker PC calls (register, poll, progress, complete, fail, needs_review, log, setup).

Every call carries the worker's token (Authorization: Bearer ...). This is separate from the web pages: it is not behind the
profile chooser, has no CSRF token and answers in JSON. A 409 means "that job is no longer yours: stop working on it".
"""
import logging
from functools import wraps

from flask import Response, g, jsonify, request

from synchub import downloads, db, jobs, pipeline_events, previews, protect, states, storage, tasks, versions, workers
from synchub.library import cleanup, migrate, scan

logger = logging.getLogger(__name__)

POLL_SECONDS = 5          # how often an agent should poll (the agent may poll less often while it is busy)
PREFIX = "/api/worker/v1"


def _error(message, status, **extra):
    return jsonify(ok=False, message=message, **extra), status


def _bearer():
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else ""


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _text(value, limit=2000):
    return None if value is None else str(value)[:limit]


def _dashboard(job_ids):
    """Moves the Dashboard stage of each job's title along. Own connection: call it after the request's transaction has ended."""
    ids = set()
    with db.connection() as conn:
        for job_id in job_ids:
            changed = pipeline_events.update_for_job(conn, job_id)
            ids |= states.media_ids_for(conn, changed) if changed else set()
    if ids:
        tasks.notify_states_changed(ids)


def register(app):
    def route(rule, name):
        """A worker endpoint: checks the token first, then runs the view with the worker in g.worker."""
        def decorate(view):
            @wraps(view)
            def guarded(*args, **kwargs):
                with db.connection() as conn:
                    worker, problem = workers.authenticate(conn, _bearer())
                if problem == "disabled":
                    return _error("This worker is disabled in SyncHub.", 403)
                if problem:
                    return _error("The worker token was not accepted.", 401)
                g.worker = worker
                try:
                    return view(*args, **kwargs)
                except jobs.JobConflict as exc:
                    return _error(str(exc), 409, stop=True)
                except jobs.JobError as exc:
                    return _error(str(exc), 400)
            app.add_url_rule(f"{PREFIX}{rule}", endpoint=f"worker_api_{name}", view_func=guarded, methods=["POST"])
            return guarded
        return decorate

    @route("/register", "register")
    def register_worker():
        with db.connection() as conn:
            try:
                workers.register(conn, g.worker["id"], _body())
            except workers.ProtocolError as exc:
                return _error(str(exc), 426, protocol=workers.PROTOCOL)
            roots, roots_hash = storage.for_agent(conn), storage.roots_hash(conn)
            settings = workers.agent_settings(workers.get(conn, g.worker["id"]))
        logger.info(f"[workers] {g.worker['name']} registered")
        return jsonify(ok=True, worker_id=g.worker["id"], name=g.worker["name"], protocol=workers.PROTOCOL,
                       poll_seconds=POLL_SECONDS, lease_seconds=jobs.LEASE_SECONDS, roots=roots, roots_hash=roots_hash, settings=settings)

    @route("/roots", "roots")
    def roots():
        """The shares and their marker files, without registering: for `--check` and `--init-roots` on the worker."""
        with db.connection() as conn:
            return jsonify(ok=True, name=g.worker["name"], protocol=workers.PROTOCOL, roots=storage.for_agent(conn), roots_hash=storage.roots_hash(conn))

    @route("/setup/<path:filename>", "setup")
    def setup_file(filename):
        """One of this worker's setup files as plain text, for agent/setup-worker.sh: the same files as the Workers page's Setup dialog, but with the
        shares mounted by the PC itself (/etc/fstab) instead of by Docker. The caller proved it holds the token, so the token is in the text."""
        if filename == "smb.cred":
            with db.connection() as conn:
                text = storage.smb_cred_file(conn)
            if text is None:
                return _error("No share login is stored in SyncHub (Storage page, Share login).", 404)
            logger.info(f"[workers] {g.worker['name']} fetched the share login")
            return Response(text, mimetype="text/plain")
        if filename not in storage.SETUP_ORDER:
            return _error(f"There is no setup file called '{filename}'.", 404)
        with db.connection() as conn:
            files = storage.setup_files(g.worker["name"], request.host_url.rstrip("/"), _bearer(), storage.list_all(conn), host_mounts=request.args.get("mounts") == "host", relay=protect.is_relay_name(conn, g.worker["name"]))
        return Response(files[filename], mimetype="text/plain")

    @route("/update/check", "update_check")
    def update_check():
        """For agent/auto-update.sh, every minute: is the worker idle, its update settings, and a request (update or restart) from the Workers page."""
        with db.connection() as conn:
            answer = workers.host_check(conn, g.worker["id"])
        return jsonify(ok=True, **answer)

    @route("/update/report", "update_report")
    def update_report():
        """For agent/auto-update.sh: what the automatic update did ('ok' or 'failed', the version, the error tail). The administrators are told."""
        with db.connection() as conn:
            try:
                report = workers.report_update(conn, g.worker["id"], _body())
            except ValueError as exc:
                return _error(str(exc), 400)
        logger.info(f"[workers] {g.worker['name']} automatic update: {report['status']} {report['commit']}")
        return jsonify(ok=True)

    @route("/update/log", "update_log")
    def update_log():
        """For agent/auto-update.sh: its own recent log lines, sent when the Workers page's Logs button asked for them."""
        with db.connection() as conn:
            workers.store_updater_log(conn, g.worker["id"], _text(_body().get("log"), 6000))
        return jsonify(ok=True)

    @route("/relay/report", "relay_report")
    def relay_report():
        """For agent/auto-update.sh: what installing (or taking away) the camera relay's go2rtc did on the PC."""
        with db.connection() as conn:
            try:
                report = protect.report_install(conn, g.worker["id"], _body())
            except ValueError as exc:
                return _error(str(exc), 400)
        logger.info(f"[workers] {g.worker['name']} camera relay: {report['status']}{' ' + report['error'] if report['error'] else ''}")
        return jsonify(ok=True)

    @route("/poll", "poll")
    def poll():
        data = _body()
        protect.note_worker_address(g.worker["id"], request.remote_addr)
        relay = protect.for_worker(g.worker["id"])            # asks UniFi Protect (at most every few minutes), so it happens before the database is held
        with db.connection() as conn:
            workers.touch(conn, g.worker["id"], data.get("status"))
            if "originals" in data:
                workers.store_originals(conn, g.worker["id"], data["originals"])
            worker = workers.get(conn, g.worker["id"])
            if isinstance(data.get("holding"), list):
                lost = jobs.reconcile(conn, worker["id"], data["holding"])
                if lost:
                    logger.warning(f"[jobs] {worker['name']} no longer holds job(s) {lost}; they were re-queued")
            resume = jobs.resume_for(conn, worker["id"], data["active"]) if isinstance(data.get("active"), list) else []
            release = jobs.release_for(conn, data["holding"]) if isinstance(data.get("holding"), list) else []
            free_bytes = data.get("free_bytes")
            workers.benchmark_polled(conn, worker)
            cleanup.sweep(conn)                                                  # sources whose waiting time is over get their deletion job before this claim
            claimed = jobs.claim(conn, worker, data.get("free_slots") or 0, free_bytes if isinstance(free_bytes, (int, float)) else None)
            downloads.process(conn)
            finished = migrate.sync_active(conn) + downloads.sync_active(conn) + versions.sync_active(conn)
            previews.sync_active(conn)
            cleanup.sync_active(conn)
            for job in claimed:
                logger.info(f"[jobs] job {job['id']} ({job['kind']}: {job['title']}) went to {worker['name']}, attempt {job['attempt']}")
            response = jsonify(ok=True, jobs=claimed, resume=resume, release=release, cancel=jobs.cancels_for(conn, worker["id"]), cleanup=jobs.cleanups_for(conn, worker["id"]),
                           log_requests=jobs.log_requests_for(conn, worker["id"]), worker_log=bool(worker["log_requested"]), benchmark=bool(worker["benchmark_requested"]), roots_hash=storage.roots_hash(conn), paused=bool(worker["paused"]), draining=bool(worker["draining"]),
                           poll_seconds=POLL_SECONDS, lease_seconds=jobs.LEASE_SECONDS, settings=workers.agent_settings(worker), **({"relay": relay} if relay else {}))
        downloads.rescan_due()
        with db.connection() as conn:
            stale = pipeline_events.close_stale(conn)
            stale_ids = states.media_ids_for(conn, stale) if stale else set()
        if stale_ids:
            tasks.notify_states_changed(stale_ids)
        _dashboard([j["id"] for j in claimed])
        cleanup.arr_due()
        if finished:
            migrate.tell_jellyfin(finished)
        return response

    @route("/jobs/<int:job_id>/progress", "progress")
    def job_progress(job_id):
        data = _body()
        with db.connection() as conn:
            got = jobs.progress(conn, job_id, g.worker["id"], stage=data.get("stage"), progress=data.get("progress"), fps=data.get("fps"),
                                speed=data.get("speed"), eta_seconds=data.get("eta_seconds"), message=data.get("message"))
        _dashboard([job_id])
        return jsonify(ok=True, **got)

    @route("/jobs/<int:job_id>/complete", "complete")
    def job_complete(job_id):
        data = _body()
        with db.connection() as conn:
            jobs.complete(conn, job_id, g.worker["id"], data.get("result"), command=_text(data.get("command"), 8000))
            arrived = migrate.sync(conn, job_id) or downloads.sync(conn, job_id) or versions.sync(conn, job_id)
            previews.sync(conn, job_id)
            cleanup.sync(conn, job_id)
            updates = migrate.jellyfin_updates(conn, job_id) if arrived else None
        logger.info(f"[jobs] job {job_id} completed by {g.worker['name']}")
        _dashboard([job_id])
        downloads.rescan_due()
        cleanup.arr_due()
        if arrived:
            migrate.tell_jellyfin(updates=updates)
        return jsonify(ok=True)

    @route("/jobs/<int:job_id>/fail", "fail")
    def job_fail(job_id):
        data = _body()
        retryable = data.get("retryable")
        with db.connection() as conn:
            outcome = jobs.fail(conn, job_id, g.worker["id"], data.get("error_class"), _text(data.get("message")) or "The worker gave no reason.",
                                retryable=retryable if isinstance(retryable, bool) else None, log_tail=_text(data.get("log_tail"), 8000),
                                command=_text(data.get("command"), 8000), keep_state=data.get("keep_state") is not False)
            migrate.sync(conn, job_id)
            downloads.sync(conn, job_id)
            versions.sync(conn, job_id)
            previews.sync(conn, job_id)
            cleanup.sync(conn, job_id)
        _dashboard([job_id])
        logger.warning(f"[jobs] job {job_id} failed on {g.worker['name']} ({data.get('error_class')}): {_text(data.get('message'), 300)} -> {outcome}")
        return jsonify(ok=True, outcome=outcome)

    @route("/jobs/<int:job_id>/needs_review", "needs_review")
    def job_needs_review(job_id):
        data = _body()
        with db.connection() as conn:
            jobs.needs_review(conn, job_id, g.worker["id"], _text(data.get("reason")) or "The worker is not sure what to do.",
                              data.get("detail"), keep_state=data.get("keep_state") is not False)
            migrate.sync(conn, job_id)
            downloads.sync(conn, job_id)
            versions.sync(conn, job_id)
            previews.sync(conn, job_id)
            cleanup.sync(conn, job_id)
        _dashboard([job_id])
        logger.info(f"[jobs] job {job_id} is waiting for review: {_text(data.get('reason'), 200)}")
        return jsonify(ok=True)

    @route("/jobs/<int:job_id>/preview/<name>", "preview_clip")
    def job_preview_clip(job_id, name):
        with db.connection() as conn:
            written = previews.store_clip(conn, job_id, g.worker["id"], name, request.stream, request.content_length)
        return jsonify(ok=True, bytes=written)

    @route("/jobs/<int:job_id>/cleaned", "cleaned")
    def job_cleaned(job_id):
        with db.connection() as conn:
            jobs.cleaned(conn, job_id, g.worker["id"])
        return jsonify(ok=True)

    @route("/jobs/<int:job_id>/log", "log")
    def job_log(job_id):
        data = _body()
        with db.connection() as conn:
            jobs.store_log(conn, job_id, g.worker["id"], data.get("text"), truncated=data.get("truncated") is True)
        return jsonify(ok=True)

    @route("/log", "worker_log")
    def worker_log():
        """The tail of the worker's own log, uploaded after the Workers page asked for it (the poll answer says worker_log: true)."""
        data = _body()
        with db.connection() as conn:
            workers.store_log(conn, g.worker["id"], data.get("text"), truncated=data.get("truncated") is True)
        logger.info(f"[workers] {g.worker['name']} uploaded its log")
        return jsonify(ok=True)

    @route("/benchmark", "benchmark")
    def worker_benchmark():
        """The report of a benchmark the Workers page asked for (the poll answer says benchmark: true)."""
        data = _body()
        with db.connection() as conn:
            workers.store_benchmark(conn, g.worker["id"], data.get("report"))
        logger.info(f"[workers] {g.worker['name']} uploaded a benchmark")
        return jsonify(ok=True)

    # ---- scans and duplicate searches
    @route("/jobs/<int:job_id>/scan/inventory", "scan_inventory")
    def scan_inventory(job_id):
        data = _body()
        with db.connection() as conn:
            job = jobs.owned(conn, job_id, g.worker["id"])
            need = scan.inventory(conn, job, data.get("files") or [], data.get("unreadable") or [])
            jobs.heartbeat(conn, job_id)
        return jsonify(ok=True, need=need)

    @route("/jobs/<int:job_id>/scan/records", "scan_records")
    def scan_records(job_id):
        data = _body()
        with db.connection() as conn:
            job = jobs.owned(conn, job_id, g.worker["id"])
            tally = scan.ingest_records(conn, job, data.get("records") or [])
            jobs.heartbeat(conn, job_id)
        for path, why in tally["failed"][:5]:
            logger.warning(f"[library] a record from {g.worker['name']} was not stored ({path}): {why}")
        return jsonify(ok=True, stored=tally["inserted"] + tally["unchanged"] + tally["changed"], failed=len(tally["failed"]), changed=tally["changed"])

    @route("/jobs/<int:job_id>/scan/hashes", "scan_hashes")
    def scan_hashes(job_id):
        data = _body()
        with db.connection() as conn:
            job = jobs.owned(conn, job_id, g.worker["id"])
            stored = scan.ingest_hashes(conn, job, data.get("results") or [])
            jobs.heartbeat(conn, job_id)
        return jsonify(ok=True, stored=stored)

    @route("/jobs/<int:job_id>/scan/finish", "scan_finish")
    def scan_finish(job_id):
        data = _body()
        with db.connection() as conn:
            job = jobs.owned(conn, job_id, g.worker["id"])
            summary = scan.finish(conn, job, data)
            jobs.heartbeat(conn, job_id)
        logger.info(f"[library] {g.worker['name']} finished {job['title']}: {summary}")
        return jsonify(ok=True, **summary)
