"""Quality checks on the web: the Low quality page (what is flagged, what is waiting, the settings), and the buttons that check or accept a file."""
from flask import Response, jsonify, redirect, render_template, request, url_for

from synchub import ai_test, better_copy, db, jobs, quality, settings, storage
from synchub.library import recommend as rc, report, scan
from settings_validation import validate_setting

PAGE_SIZE = 50
SETTING_KEYS = ("quality_samples", "quality_slice_seconds", "quality_flag_below", "quality_min_height", "quality_on_download", "quality_scan_library", "quality_scan_order", "quality_paused", "quality_damage_pass", "quality_check_first")


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def _who(conn):
    from synchub.web import current_profile
    profile = current_profile(conn)
    return profile["name"] if profile else "guest"


def _file_id(data):
    try:
        return int(data.get("file_id"))
    except (TypeError, ValueError):
        return None


def overview(conn, full=True):
    """What the Library tab (the flagged files) and the Options tab (`full=False`, only the settings) show about quality checks."""
    values = {k: (settings.get(k, conn=conn) or settings.DEFAULTS.get(k, "")) for k in SETTING_KEYS}   # a blank saved value must not hide the recommended one
    if not full:
        return {"values": values, "flag_below": quality.flag_below(conn), "version": quality.CHECK_VERSION}
    waiting = [dict(r) for r in conn.execute(
        """SELECT q.id, q.status, q.source, q.message, f.file_name, f.id AS file_id FROM quality_check q JOIN library_file f ON f.id = q.library_file_id
           WHERE q.status IN ('queued', 'running') ORDER BY q.id DESC LIMIT 30""")]
    recent = [dict(r) for r in conn.execute(
        """SELECT q.id, q.status, q.score, q.grade, q.message, q.finished_at, f.file_name, f.id AS file_id FROM quality_check q JOIN library_file f ON f.id = q.library_file_id
           WHERE q.status IN ('done', 'failed') ORDER BY q.id DESC LIMIT 15""")]
    return {"low": quality.low_quality(conn), "counts": quality.counts(conn), "waiting": waiting, "recent": recent, "flag_below": quality.flag_below(conn),
            "values": values, "version": quality.CHECK_VERSION}


def register(app):
    @app.route("/library/quality")
    def library_quality_page():
        """Below minimum quality: files that are too small, scored low or are damaged. Held from conversion; they may only be renamed and moved."""
        q, root = (request.args.get("q") or "").strip(), (request.args.get("root") or "").strip()
        page = int(request.args["page"]) if (request.args.get("page") or "").isdigit() and int(request.args["page"]) > 0 else 1
        with db.connection() as conn:
            cfg, cache = scan.config(conn), {}
            total = len(quality._flagged(conn, q or None, root or None))
            rows = quality.low_quality(conn, PAGE_SIZE, q or None, root or None, (page - 1) * PAGE_SIZE)
            for d in rows:
                d["quality_state"] = quality.state_for(conn, d)
                d["r"] = rc.recommend(d, rc.cfg_for(conn, d, cfg, cache), held=True)
                d["where"] = d["r"].group                  # held, unless a decision or a problem keeps it on another page (queued, rejected, needs you)
            data = overview(conn, full=True)
            roots = [r["name"] for r in storage.list_all(conn)]
        return render_template("library_quality.html", rows=rows, total=total, page=page, pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1), f={"q": q, "root": root}, roots=roots,
                               qual=data, counts=data["counts"], values=data["values"], flag_below=data["flag_below"], version=data["version"], waiting=data["waiting"], recent=data["recent"],
                               titles=rc.GROUP_TITLES)

    @app.route("/library/quality/export")
    def library_quality_export():
        q, root = (request.args.get("q") or "").strip(), (request.args.get("root") or "").strip()
        with db.connection() as conn:
            records = quality.low_quality(conn, 1000000, q or None, root or None)
        return Response(report.csv_text(records), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="below_minimum_quality.csv"'})

    @app.route("/library/quality/ai")
    def library_ai_fragment():
        with db.connection() as conn:
            return render_template("_quality_ai_results.html", ai=ai_test.overview(conn))

    @app.route("/api/library/quality/ai/files")
    def api_library_ai_files():
        with db.connection() as conn:
            rows = ai_test.files(conn, request.args.get("q", ""))
        return jsonify(files=[{"id": r["id"], "label": f"{r['recommended_name'] or r['file_name']} ({r['root_name']}: {r['relative_path']})"[:200]} for r in rows])

    @app.route("/api/library/quality/ai/run", methods=["POST"])
    def api_library_ai_run():
        data = request.get_json(silent=True) or {}
        file_id = _file_id(data)
        if file_id is None:
            return _fail("Choose a file.")
        try:
            with db.connection() as conn:
                ai_test.run(conn, file_id, str(data.get("mode") or ""), by=_who(conn))
        except (ai_test.AiTestError, jobs.JobError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message={"standard": "Standard check queued.", "ai": "AI test queued.", "both": "Standard check and AI test queued."}[data["mode"]] + " A worker will take it soon; the result appears below.")

    @app.route("/api/library/quality/check", methods=["POST"])
    def api_library_quality_check():
        data = request.get_json(silent=True) or {}
        file_id = _file_id(data)
        if file_id is None:
            return _fail("Choose a file to check.")
        mode = "damage" if data.get("mode") == "damage" else "quality"
        try:
            with db.connection() as conn:
                quality.request(conn, file_id, by=_who(conn), source="manual", mode=mode)
        except (quality.QualityError, jobs.JobError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Queued. A worker will check it soon; this page does not need to stay open." if mode == "quality" else "Queued. A worker will read the whole file looking for decode errors; this can take a while.")

    @app.route("/api/library/quality/accept", methods=["POST"])
    def api_library_quality_accept():
        data = request.get_json(silent=True) or {}
        file_id = _file_id(data)
        if file_id is None:
            return _fail("Choose a file.")
        try:
            with db.connection() as conn:
                if data.get("undo"):
                    quality.unaccept(conn, file_id)
                    return jsonify(ok=True, message="It will be flagged again if it is still low quality.")
                quality.accept(conn, file_id, by=_who(conn))
        except quality.QualityError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Accepted as it is. It is flagged again only if the file changes.")

    @app.route("/api/library/quality/releases", methods=["POST"])
    def api_library_quality_releases():
        """Read only: what Radarr's indexers have for this film. Changes nothing."""
        file_id = _file_id(request.get_json(silent=True) or {})
        if file_id is None:
            return _fail("Choose a file.")
        try:
            return jsonify(ok=True, **better_copy.preview(file_id))
        except better_copy.BetterCopyError as exc:
            return _fail(str(exc))

    @app.route("/api/library/quality/better", methods=["POST"])
    def api_library_quality_better():
        """Blocks the release that was grabbed for this file and asks Radarr to search again."""
        file_id = _file_id(request.get_json(silent=True) or {})
        if file_id is None:
            return _fail("Choose a file.")
        try:
            with db.connection() as conn:
                who = _who(conn)
            return jsonify(ok=True, **better_copy.request_auto(file_id, by=who))
        except better_copy.BetterCopyError as exc:
            return _fail(str(exc))

    @app.route("/api/library/quality/grab", methods=["POST"])
    def api_library_quality_grab():
        """Blocks the release that was grabbed for this file and sends one release from the list to the download client."""
        data = request.get_json(silent=True) or {}
        file_id = _file_id(data)
        if file_id is None or not data.get("guid") or data.get("indexer_id") in (None, ""):
            return _fail("Choose a release from the list.")
        try:
            with db.connection() as conn:
                who = _who(conn)
            return jsonify(ok=True, **better_copy.grab(file_id, str(data["guid"]), data["indexer_id"], by=who))
        except better_copy.BetterCopyError as exc:
            return _fail(str(exc))

    @app.route("/api/library/quality/stop-all", methods=["POST"])
    def api_library_quality_stop_all():
        with db.connection() as conn:
            stopped = quality.stop_all(conn, by=_who(conn))
        return jsonify(ok=True, stopped=stopped, message=f"Told {stopped} check(s) to stop." if stopped else "No check was waiting or running.")

    @app.route("/api/library/quality/clear", methods=["POST"])
    def api_library_quality_clear():
        data = request.get_json(silent=True) or {}
        with db.connection() as conn:
            removed = quality.clear_results(conn, str(data.get("root") or "") or None)
        return jsonify(ok=True, removed=removed, message=f"Removed {removed} stored result(s). The Library list was not touched." if removed else "There were no stored results to remove.")

    @app.route("/api/library/quality/settings", methods=["POST"])
    def api_library_quality_settings():
        data = request.get_json(silent=True) or {}
        cleaned, errors = {}, []
        for key in SETTING_KEYS:
            if key in data:
                raw = str(data[key]).strip() or settings.DEFAULTS.get(key, "")     # a blank box means the recommended value
                ok, result = validate_setting(key, raw)
                (cleaned.__setitem__(key, result) if ok else errors.append(result))
        if errors:
            return _fail(" ".join(errors))
        with db.connection() as conn:
            for key, value in cleaned.items():
                settings.set_value(key, value, conn=conn)
        return jsonify(ok=True, message="Saved.")
