"""Pages for the worker fleet: the Workers page, its live fragment and the actions behind its buttons."""
from flask import jsonify, render_template, request

from synchub import db, jobs, storage, versions, webhooks, workers
from synchub.library import cleanup, migrate

# What each worker button does: setting, new value.
WORKER_ACTIONS = {"pause": ("paused", True), "resume": ("paused", False), "drain": ("draining", True),
                  "undrain": ("draining", False), "enable": ("enabled", True), "disable": ("enabled", False)}


def _gb(value):
    return f"{value / 1024 ** 3:,.1f} GB" if isinstance(value, (int, float)) else None


def _duration(seconds):
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    minutes = int(seconds // 60)
    if minutes >= 1440:
        return f"{minutes // 1440} d {minutes % 1440 // 60} h"
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{max(minutes, 1)} min"


def _text(value, limit=200):
    return str(value).strip()[:limit] if value not in (None, "") else None


def _items(value, limit=40):
    """A list the agent sent, or an empty one when it sent something else: a report is trusted to be a report, never to be well formed."""
    return list(value)[:limit] if isinstance(value, (list, tuple)) else []


def _whole(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# The video encoders the Worker Status tab names: ffmpeg name -> (codec, how it encodes). The agent tests the ones it can with a real frame;
# whatever it reports that is not here is still shown, under its own name.
ENCODERS = {
    "libx264": ("H.264", "Software (CPU)"), "libx265": ("HEVC (H.265)", "Software (CPU)"), "libsvtav1": ("AV1", "Software (CPU)"),
    "libaom-av1": ("AV1", "Software (CPU)"), "librav1e": ("AV1", "Software (CPU)"), "libvpx-vp9": ("VP9", "Software (CPU)"),
    "h264_nvenc": ("H.264", "NVIDIA NVENC"), "hevc_nvenc": ("HEVC (H.265)", "NVIDIA NVENC"), "av1_nvenc": ("AV1", "NVIDIA NVENC"),
    "h264_qsv": ("H.264", "Intel Quick Sync"), "hevc_qsv": ("HEVC (H.265)", "Intel Quick Sync"), "av1_qsv": ("AV1", "Intel Quick Sync"),
    "h264_vaapi": ("H.264", "VAAPI (Intel / AMD GPU)"), "hevc_vaapi": ("HEVC (H.265)", "VAAPI (Intel / AMD GPU)"), "av1_vaapi": ("AV1", "VAAPI (Intel / AMD GPU)"),
    "h264_amf": ("H.264", "AMD AMF"), "hevc_amf": ("HEVC (H.265)", "AMD AMF"), "av1_amf": ("AV1", "AMD AMF"),
    "h264_videotoolbox": ("H.264", "Apple VideoToolbox"), "hevc_videotoolbox": ("HEVC (H.265)", "Apple VideoToolbox"),
}


# What the agent tests with a real frame (agent/synchub_agent.py, detect_capabilities).
TESTED = {"libx265", "libx264", "libsvtav1", "hevc_nvenc", "h264_nvenc", "av1_nvenc", "hevc_qsv", "h264_qsv", "av1_qsv", "hevc_vaapi", "h264_vaapi", "av1_vaapi"}


def describe_encoders(working, built):
    """One row per encoder the worker has: it works (a test frame was encoded), is built into ffmpeg but did not work, or was not tested."""
    working = [str(e) for e in _items(working)]
    built = [str(e) for e in _items(built)]
    rows = []
    for name in list(dict.fromkeys(working + built)):
        codec, kind = ENCODERS.get(name, ("-", "-"))
        if name in working:
            state, color, note = "works", "success", "Encoded a test frame."
        elif name in TESTED:
            state, color, note = "built in, but failed", "warning", "ffmpeg has it but the test frame failed: usually the GPU, its driver or /dev/dri is not available here."
        else:
            state, color, note = "built in, not tested", "secondary", "ffmpeg has it; the agent does not test this one."
        rows.append({"name": name, "codec": codec, "kind": kind, "state": state, "color": color, "note": note})
    return sorted(rows, key=lambda r: (r["color"] != "success", r["kind"] != "Software (CPU)", r["kind"], r["codec"], r["name"]))


def describe_host(w):
    """What the Worker Status tab shows for one worker: the hardware it reported at registration and the live numbers from its last poll.
    `known` is False for an agent too old to report the details."""
    caps, live = w["capabilities"], w["status"]
    host = caps.get("host") if isinstance(caps.get("host"), dict) else None
    dynamic = live.get("host") if isinstance(live.get("host"), dict) else {}
    ffmpeg = host.get("ffmpeg") if host and isinstance(host.get("ffmpeg"), dict) else {}
    cpu = host.get("cpu") if host and isinstance(host.get("cpu"), dict) else {}
    threads, cores = _whole(cpu.get("threads")) or _whole(caps.get("cores")), _whole(cpu.get("cores"))
    load = [x for x in _items(dynamic.get("load"), 3) if isinstance(x, (int, float)) and not isinstance(x, bool)]
    gpus = []
    for gpu in _items((host or {}).get("gpus"), 8):
        if isinstance(gpu, dict) and gpu.get("name"):
            memory = _whole(gpu.get("memory_mb"))
            gpus.append({"name": _text(gpu["name"]), "driver": _text(gpu.get("driver"), 40), "memory": f"{memory / 1024:,.1f} GB" if memory else None, "source": _text(gpu.get("source"), 20)})
    cpu_lines = [f"{cores} cores, {threads} threads" if cores and threads else f"{threads} threads" if threads else None,
                 f"{cpu['mhz'] / 1000:.2f} GHz" if isinstance(cpu.get("mhz"), (int, float)) and cpu["mhz"] > 0 else None]
    container = _text((host or {}).get("container"), 20)
    return {
        "known": host is not None, "arch": _text((host or {}).get("arch"), 20), "kernel": _text((host or {}).get("kernel"), 60),
        "cpu_model": _text(cpu.get("model")) or ("unknown" if host else None), "cpu_size": ", ".join(x for x in cpu_lines if x) or None,
        "cpu_flags": [str(f)[:16] for f in _items(cpu.get("flags"))], "cpu_percent": round(dynamic["cpu_percent"]) if isinstance(dynamic.get("cpu_percent"), (int, float)) else None,
        "load": " ".join(f"{x:.2f}" for x in load) or None, "memory_total": _gb((host or {}).get("memory_bytes")), "memory_free": _gb(dynamic.get("mem_available_bytes")),
        "gpus": gpus, "render_devices": [str(d)[:40] for d in _items((host or {}).get("render_devices"), 8)],
        "container": container, "host_uptime": _duration(dynamic.get("host_uptime_seconds")), "agent_uptime": _duration(live.get("uptime_seconds")),
        "python": _text(caps.get("python"), 20), "ffmpeg": _text(caps.get("ffmpeg"), 120), "ffprobe": _text(caps.get("ffprobe"), 120),
        "hwaccels": [str(h)[:24] for h in _items(ffmpeg.get("hwaccels"), 20)], "vmaf": bool(caps.get("vmaf")), "ops": [str(o)[:24] for o in _items(caps.get("ops"), 20)],
        "encoders": describe_encoders(caps.get("encoders"), ffmpeg.get("encoders_built")), "work_free": _gb(live.get("work_free_bytes")),
        "slots": caps.get("agent_slots"),
    }


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
    w.update(state=state, state_color=color, encoders=", ".join(str(e) for e in _items(w["capabilities"].get("encoders"))),
             free=_gb(status.get("work_free_bytes")), originals=(f"{held} held, {_gb(status.get('originals_bytes')) or '?'}" if held else None),
             seen=webhooks.ago(w["last_seen"]) if w["last_seen"] else "never", roots=describe_roots(w, root_names), host=describe_host(w))
    w["benchmark"] = describe_benchmark(w)
    return w


def describe_benchmark(w):
    """The 'Last benchmark' block of a worker row: what the newest report says, and whether a run is waiting."""
    last = (w.get("benchmark_last") or None)
    out = {"pending": bool(w.get("benchmark_requested")), "can_run": "benchmark" in (w["capabilities"].get("features") or []), "last": None}
    if last:
        out["last"] = {"ok": bool(last["ok"]), "when": webhooks.ago(last["created_at"]), "summary": last["summary"], "report": last["report"]}
    return out


def describe_job(j):
    j["lifecycle"] = dict(zip(("code", "label"), jobs.lifecycle(j)))
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
    for w in raw:
        w["benchmark_last"] = workers.last_benchmark(conn, w["id"])
    able = [w for w in raw if w["enabled"] and not w["revoked"] and w["online"]]
    listed = [describe_job(j) for j in jobs.recent(conn, 50)]
    for j in listed:
        j["waiting_for"] = _waiting_for(j, able)
    return {"workers": [describe_worker(w, root_names) for w in raw], "jobs": listed, "counts": counts, "summary": summary, "root_names": root_names}


def _setup(conn, name, token=None):
    """The setup files for a worker. Right after a token is made they hold it; afterwards a placeholder, since the token cannot be read again."""
    url = request.host_url.rstrip("/")
    shown = token or "<the token shown when this worker was added>"
    files = {storage.QUICK_SETUP: storage.quick_setup(url, shown), **storage.setup_files(name, url, shown, storage.list_all(conn))}
    return {"url": url, "files": files, "order": [storage.QUICK_SETUP, *storage.SETUP_ORDER], "has_token": bool(token), "shares": len(storage.list_all(conn))}


def _who(conn):
    from synchub.web import current_profile
    profile = current_profile(conn)
    return profile["name"] if profile else "guest"


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def register(app):
    @app.route("/workers")
    def workers_page():
        from synchub.web import media
        return media.render_media("workers")

    @app.route("/workers/fragment")
    def workers_fragment():
        with db.connection() as conn:
            data = snapshot(conn)
        return render_template("_fleet.html", **data)

    @app.route("/jobs")
    def jobs_page():
        from synchub.web import media
        return media.render_media("jobs")

    @app.route("/jobs/fragment")
    def jobs_fragment():
        with db.connection() as conn:
            data = snapshot(conn)
        return render_template("_jobs_live.html", **data)

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
                if action == "log":
                    workers.request_log(conn, worker_id)
                    online = workers.get(conn, worker_id)["online"]
                    return jsonify(ok=True, online=online, message="Asked the worker for its log." if online
                                   else "Asked. The worker is offline, so the log arrives when it is back.")
                if action == "benchmark":
                    workers.request_benchmark(conn, worker_id)
                    return jsonify(ok=True, message="Asked the worker to run a benchmark. It starts when the worker has no job running, and the result appears here when it is done (a few minutes).")
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

    @app.route("/api/workers/<int:worker_id>/log")
    def api_worker_log(worker_id):
        with db.connection() as conn:
            worker = workers.get(conn, worker_id)
            if worker is None:
                return _fail("That worker no longer exists.", 404)
            log = workers.stored_log(conn, worker_id)
        if log:
            log["received"] = webhooks.ago(log["received_at"])
        return jsonify(ok=True, log=log, pending=bool(worker["log_requested"]), name=worker["name"], online=worker["online"], version=worker["version"])

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
                    migrate.sync(conn, job_id)
                    versions.sync(conn, job_id)
                    cleanup.sync(conn, job_id)
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
                    migrate.sync(conn, job_id)
                    versions.sync(conn, job_id)
                    cleanup.sync(conn, job_id)
                    return jsonify(ok=True, message="Queued again.")
                if action in ("approve", "skip", "reject"):
                    jobs.decide(conn, job_id, action, by, data.get("note"))
                    migrate.sync(conn, job_id)
                    versions.sync(conn, job_id)
                    cleanup.sync(conn, job_id)
                    return jsonify(ok=True, message={"approve": "Approved: it carries on.", "skip": "Skipped: the file is left as it is.", "reject": "Rejected."}[action])
        except jobs.JobError as exc:
            return _fail(str(exc))
        return _fail("Unknown action.", 404)
