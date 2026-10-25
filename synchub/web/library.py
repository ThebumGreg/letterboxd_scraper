"""The Library page: the original analysis and approval tool, inside SyncHub.

Scan the shares, see every file's plain-language recommendation sorted into queues, approve what is right (one by one, or in bulk with a
confirmation), and export the lot as a CSV. Nothing here touches a file: approvals are decisions, recorded with who made them; what is done
with approved files is a later step.
"""
from datetime import datetime, timezone

from flask import Response, jsonify, redirect, render_template, request, url_for

from synchub import db, jobs, settings, storage
from synchub.library import findings, migrate, recommend as rc, report, scan, store
from synchub.library import vocab as v
from settings_validation import validate_setting

PAGE_SIZE = 100
CONFIDENCE_FLOORS = {"high": rc.HIGH, "medium": rc.MEDIUM}
FILTER_KEYS = ("q", "root", "conf")


def _who(conn):
    from synchub.web import current_profile
    profile = current_profile(conn)
    return profile["name"] if profile else "guest"


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def _size(size):
    """GB for anything that large, smaller units below so a small file does not read 0.00 GB."""
    if size is None:
        return "?"
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:,.2f} {unit}"
    return f"{size} B"


def _duration(seconds):
    if not seconds:
        return None
    minutes = int(seconds // 60)
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def _filters():
    return {k: (request.args.get(k) or "").strip() for k in FILTER_KEYS}


def shares_summary(conn, cfg):
    """What each share holds, as the library knows it."""
    rows = {r["root_name"]: dict(r) for r in conn.execute(
        "SELECT root_name, COUNT(*) AS files, SUM(size_bytes) AS bytes, SUM(missing_since IS NOT NULL) AS missing, SUM(approval_status = 'approved') AS approved, "
        "SUM(unreadable_reason IS NOT NULL) AS unreadable FROM library_file GROUP BY root_name")}
    out = []
    for root in storage.list_all(conn):
        r = rows.get(root["name"], {"files": 0, "bytes": 0, "missing": 0, "approved": 0, "unreadable": 0})
        out.append({"name": root["name"], "label": root["label"], "destination": root["name"] == cfg.destination_root, "files": r["files"], "size": _size(r["bytes"] or 0),
                    "missing": r["missing"] or 0, "approved": r["approved"] or 0, "unreadable": r["unreadable"] or 0})
    return out


def overview(conn):
    cfg = scan.config(conn)
    rows = rc.classify(conn, cfg)
    table = rc.counts(conn, cfg, rows)
    groups = [{"key": g, "title": rc.GROUP_TITLES[g], "hint": rc.GROUP_HINTS[g], **table[g]} for g in rc.GROUP_ORDER]
    runs = scan.list_runs(conn, 12)
    for run in runs:
        run["percent"] = None if run["progress"] is None else round(run["progress"] * 100)
        run["when"] = (run["created_at"] or "")[:16]
    options = scan_options_for_page(conn)
    migration = migrate.queue_summary(conn)
    return {"cfg": cfg, "groups": groups, "total": len(rows), "shares": shares_summary(conn, cfg), "runs": runs, "active": any(r["active"] for r in runs) or bool(migration["queued"] or migration["running"]), "options": options,
            "roots": [r["name"] for r in storage.list_all(conn)], "migration": migration, "lookups": {"confidence": CONFIDENCE_FLOORS}}


def scan_options_for_page(conn):
    options, cfg = scan.scan_options(conn), scan.config(conn)
    return {**options, "exclude_text": "\n".join(options["exclude"]), "destination": cfg.destination_root, "tv_folder": cfg.tv_folder, "movie_folder": cfg.movie_folder}


def describe_record(rec, rcm, cfg):
    """A record and its recommendation as the record page shows them."""
    streams = [s for s in v.json_list(rec.get("streams_json")) if isinstance(s, dict)]
    removals = {r["index"]: r for r in rcm.removals}
    for s in streams:
        s["remove"] = s["index"] in removals
        s["locked"] = bool(removals.get(s["index"], {}).get("locked"))
        s["language_name"] = rc.language_name(s.get("lang"))
    plans = [p for p in v.json_list(rec.get("proposed_track_changes_json")) if isinstance(p, dict)]
    return {"rec": rec, "r": rcm, "streams": streams, "plans": plans, "size": _size(rec.get("size_bytes")), "duration": _duration(rec.get("duration_seconds")),
            "warnings": v.json_list(rec.get("warnings_json")), "errors": v.json_list(rec.get("errors_json")), "reasons": v.json_list(rec.get("review_reasons_json")),
            "final_name": v.final_name(rec), "in_place": findings.in_place(rec, cfg), "missing": bool(rec.get("missing_since"))}


def queue_rows(conn, cfg, group, f):
    rows = [(rec, r) for rec, r in rc.classify(conn, cfg, f["q"] or None, f["root"] or None) if r.group == group]
    floor = CONFIDENCE_FLOORS.get(f["conf"])
    if floor:
        rows = [(rec, r) for rec, r in rows if r.confidence == floor]
    return rows


def register(app):
    @app.route("/library")
    def library_page():
        with db.connection() as conn:
            data = overview(conn)
        return render_template("library.html", **data)

    @app.route("/library/fragment")
    def library_fragment():
        with db.connection() as conn:
            data = overview(conn)
        return render_template("_library_live.html", **data)

    # ---- the queues
    @app.route("/library/queue/<group>")
    def library_queue(group):
        if group not in rc.GROUP_ORDER:
            return redirect(url_for("library_page"))
        f = _filters()
        page = max(int(request.args.get("page") or 1), 1) if (request.args.get("page") or "1").isdigit() else 1
        with db.connection() as conn:
            cfg = scan.config(conn)
            all_rows = queue_rows(conn, cfg, group, f)
            everything = rc.classify(conn, cfg, f["q"] or None, f["root"] or None)
            table = rc.counts(conn, cfg, [(rec, r) for rec, r in everything])
            candidates = [(rec, r) for rec, r in all_rows if r.can_approve and not r.needs_name and r.confidence in (rc.HIGH, rc.MEDIUM) and r.kind not in ("keep_duplicate", "skip_duplicate")]
            roots = [r["name"] for r in storage.list_all(conn)]
        shown = all_rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        rows = [{"rec": rec, "r": r, "size": _size(rec["size_bytes"]) if not rec.get("unreadable_reason") else None} for rec, r in shown]
        return render_template("library_queue.html", group=group, title=rc.GROUP_TITLES[group], hint=rc.GROUP_HINTS[group], rows=rows, total=len(all_rows), page=page,
                               pages=max((len(all_rows) + PAGE_SIZE - 1) // PAGE_SIZE, 1), f=f, counts=table[group], groups=rc.GROUP_ORDER, titles=rc.GROUP_TITLES, table=table,
                               high=sum(1 for _, r in candidates if r.confidence == rc.HIGH), high_medium=len(candidates), roots=roots)

    @app.route("/library/review/<group>")
    def library_review(group):
        if group not in rc.GROUP_ORDER:
            return redirect(url_for("library_page"))
        f = _filters()
        after = request.args.get("after")
        with db.connection() as conn:
            nxt = rc.next_record_id(conn, group, int(after) if after and after.isdigit() else None, f["q"] or None, f["root"] or None, scan.config(conn))
        if nxt is None:
            return redirect(url_for("library_page", done=group))
        return redirect(url_for("library_record", record_id=nxt, group=group, **{k: x for k, x in f.items() if x}))

    @app.route("/library/record/<int:record_id>")
    def library_record(record_id):
        with db.connection() as conn:
            cfg = scan.config(conn)
            rec = store.get_record(conn, record_id)
            if rec is None:
                return redirect(url_for("library_page"))
            rcm = rc.recommend(rec, cfg)
            events = store.events_for(conn, record_id)
            siblings = []
            if rec.get("duplicate_group"):
                siblings = [dict(r) for r in conn.execute("SELECT id, root_name, relative_path, duplicate_status, approval_status FROM library_file WHERE duplicate_group = ? AND id != ? ORDER BY id",
                                                           (rec["duplicate_group"], record_id))]
        group = request.args.get("group") or rcm.group
        data = describe_record(rec, rcm, cfg)
        return render_template("library_record.html", group=group, group_title=rc.GROUP_TITLES.get(group, group), events=events, siblings=siblings, f=_filters(), **data)

    # ---- decisions
    def _decide(record_id, action):
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                by, cfg = _who(conn), scan.config(conn)
                if action == "approve":
                    keep = [int(i) for i in data.get("keep_streams") or []]
                    outcome = rc.apply_recommendation(conn, record_id, by, name=data.get("name"), keep_streams=keep or None, note=data.get("note"), cfg=cfg)
                    message = {"approved": "Approved.", "rejected": "Skipped: that copy will not be migrated."}.get(outcome, outcome[:1].upper() + outcome[1:] + ".")
                elif action == "tested":
                    rc.approve_after_testing(conn, record_id, by, data.get("note"), cfg=cfg)
                    message = "Approved, with your note."
                elif action == "reject":
                    rc.reject(conn, record_id, by, data.get("note"))
                    message = "Rejected: it will not be migrated."
                else:
                    rc.undo(conn, record_id, by)
                    message = "Decision undone."
        except (ValueError, v.LibraryError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message=message)

    @app.route("/api/library/record/<int:record_id>/<action>", methods=["POST"])
    def api_library_decide(record_id, action):
        if action not in ("approve", "reject", "undo", "tested"):
            return _fail("Unknown action.", 404)
        return _decide(record_id, action)

    # ---- bulk approval
    @app.route("/library/bulk/<group>")
    def library_bulk(group):
        if group not in rc.GROUP_ORDER:
            return redirect(url_for("library_page"))
        f = _filters()
        floor = CONFIDENCE_FLOORS.get(f["conf"], rc.MEDIUM)
        with db.connection() as conn:
            cfg = scan.config(conn)
            candidates = [(rec, r) for rec, r in rc.bulk_candidates(conn, group, floor, f["q"] or None, f["root"] or None, cfg) if r.kind not in ("keep_duplicate", "skip_duplicate")]
        sample = [{"rec": rec, "r": r} for rec, r in candidates[:25]]
        size = sum(rec["size_bytes"] or 0 for rec, _ in candidates)
        return render_template("library_bulk.html", group=group, title=rc.GROUP_TITLES[group], f=f, floor="high" if floor == rc.HIGH else "high and medium", candidates=len(candidates),
                               sample=sample, size=_size(size))

    @app.route("/api/library/bulk/<group>", methods=["POST"])
    def api_library_bulk(group):
        if group not in rc.GROUP_ORDER:
            return _fail("Unknown queue.", 404)
        data = request.get_json(silent=True) or {}
        floor = CONFIDENCE_FLOORS.get(str(data.get("conf") or ""), rc.MEDIUM)
        try:
            with db.connection() as conn:
                done, skipped = rc.bulk_approve(conn, group, _who(conn), floor, (data.get("q") or None), (data.get("root") or None), scan.config(conn))
        except (ValueError, v.LibraryError) as exc:
            return _fail(str(exc))
        left = f" {len(skipped)} left for you: {skipped[0][1]}." if skipped else ""
        return jsonify(ok=True, done=done, skipped=len(skipped), message=f"Approved {done} file{'s' if done != 1 else ''}.{left}")

    @app.route("/api/library/queue-approved", methods=["POST"])
    def api_library_queue_approved():
        """Queues the files that were approved but have no job (approved before approvals queued work, or a job that was cancelled)."""
        try:
            with db.connection() as conn:
                queued, skipped = migrate.queue_approved(conn, scan.config(conn), _who(conn))
        except (ValueError, v.LibraryError) as exc:
            return _fail(str(exc))
        left = f" {len(skipped)} could not be queued: {skipped[0][1]}." if skipped else ""
        return jsonify(ok=True, queued=queued, skipped=len(skipped), message=f"Queued {queued} file{'s' if queued != 1 else ''} for a worker.{left}")

    # ---- scans, duplicates, settings
    @app.route("/api/library/scan", methods=["POST"])
    def api_library_scan():
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                run_id = scan.start_scan(conn, data.get("root"), data.get("prefix") or "", _who(conn), data.get("integrity") or None, bool(data.get("force")),
                                         data.get("exclude") if isinstance(data.get("exclude"), str) else None)
        except v.LibraryError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, run=run_id, message="Scan queued. A worker starts it within seconds; progress shows below.")

    @app.route("/api/library/duplicates", methods=["POST"])
    def api_library_duplicates():
        try:
            with db.connection() as conn:
                run_id = scan.start_duplicate_search(conn, _who(conn))
                run = scan.list_runs(conn, 1)[0]
        except v.LibraryError as exc:
            return _fail(str(exc))
        message = "Nothing needed reading: the groups were formed from what is already known." if run["status"] == "completed" else "Duplicate search queued: the files that have not been compared yet are read by a worker."
        return jsonify(ok=True, run=run_id, message=message)

    @app.route("/api/library/runs/<run_id>/cancel", methods=["POST"])
    def api_library_cancel(run_id):
        with db.connection() as conn:
            row = conn.execute("SELECT * FROM library_run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return _fail("That run no longer exists.", 404)
            stopped = 0
            for job in scan.run_jobs(conn, run_id):
                if job["status"] in ("queued", "running", "needs_review"):
                    jobs.cancel(conn, job["id"], _who(conn))
                    stopped += 1
        return jsonify(ok=True, message="Cancelling: the worker stops at its next report." if stopped else "Nothing was running.")

    @app.route("/api/library/settings", methods=["POST"])
    def api_library_settings():
        data = request.get_json(silent=True) or {}
        keys = ("library_destination_root", "library_tv_folder", "library_movie_folder", "library_integrity_mode", "library_tail_seconds", "library_exclude_folders")
        cleaned, errors = {}, []
        for key in keys:
            if key in data:
                ok, result = validate_setting(key, str(data[key]))
                (cleaned.__setitem__(key, result) if ok else errors.append(result))
        with db.connection() as conn:
            if "library_destination_root" in cleaned and storage.by_name(conn, cleaned["library_destination_root"]) is None:
                errors.append(f"There is no share called '{cleaned['library_destination_root']}'. Add it on the Storage page first.")
            if errors:
                return _fail(" ".join(errors))
            for key, value in cleaned.items():
                settings.set_value(key, value, conn=conn)
            if cleaned:
                findings.refresh(conn, scan.config(conn))                   # a different destination or layout changes what is "in place"
        return jsonify(ok=True, message="Saved.")

    # ---- the report
    @app.route("/library/export.csv")
    def library_export():
        f = _filters()
        group = request.args.get("group") or ""
        with db.connection() as conn:
            cfg = scan.config(conn)
            rows = rc.classify(conn, cfg, f["q"] or None, f["root"] or None)
            records = [rec for rec, r in rows if not group or r.group == group]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return Response(report.csv_text(records), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="library_export_{group or "all"}_{stamp}.csv"'})
