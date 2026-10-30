"""The Library page: the original analysis and approval tool, inside SyncHub.

Scan the shares, see every file's plain-language recommendation sorted into queues, approve what is right (one by one, or in bulk with a
confirmation), and export the lot as a CSV. Nothing here touches a file: approvals are decisions, recorded with who made them; what is done
with approved files is a later step.
"""
import dataclasses
import os
from datetime import datetime, timezone

from flask import Response, jsonify, redirect, render_template, request, send_file, url_for

from synchub import db, downloads, jobs, previews, settings, storage, versions
from synchub.library import cleanup, findings, migrate, profiles, recommend as rc, report, rule_steps, scan, size_policy, store
from synchub.library import vocab as v
from settings_validation import validate_setting

PAGE_SIZE = 100
CONFIDENCE_FLOORS = {"high": rc.HIGH, "medium": rc.MEDIUM}
FILTER_KEYS = ("q", "root", "conf")


def _who(conn):
    from synchub.web import current_profile
    profile = current_profile(conn)
    return profile["name"] if profile else "guest"


def _with_previews(conn, rows):
    """Each import with the newest preview made for it, so the row can link to it."""
    for row in rows:
        found = previews.latest_for(conn, "download_import", row["id"])
        row["preview"] = previews.describe(found) if found else None
    return rows


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
    return {"cfg": cfg, "groups": groups, "total": len(rows), "shares": shares_summary(conn, cfg), "runs": runs, "active": any(r["active"] for r in runs) or bool(migration["queued"] or migration["running"]) or bool(downloads.summary(conn)["working"]), "options": options,
            "roots": [r["name"] for r in storage.list_all(conn)], "migration": migration,
            "downloads": _with_previews(conn, downloads.recent(conn, 15)), "preview_summary": previews.summary(conn), "review_first": previews.review_first(conn), "trust_after": previews.trust_after(conn),
            "preview_seconds": previews.clip_seconds(conn), "preview_keep_days": previews.keep_days(conn), "preview_auto": previews.auto_make(conn), "download_summary": downloads.summary(conn), "originals_days": downloads.retention_days(conn), "size": size_policy.load(conn), "lookups": {"confidence": CONFIDENCE_FLOORS},
            "profiles": profiles.all_profiles(conn), "default_profile_id": profiles.default(conn)["id"], "cleanup": cleanup.summary(conn), "attention": migrate.attention(conn), "missing_count": store.count_missing(conn),
            "versions_config": dataclasses.asdict(versions.load(conn)), "heights": (480, 720, 1080, 1440),
            "profile_options": {"codecs": profiles.VIDEO_CODECS, "presets": profiles.PRESETS, "subtitles": profiles.SUBTITLE_LABELS}}


def scan_options_for_page(conn):
    options, cfg = scan.scan_options(conn), scan.config(conn)
    return {**options, "exclude_text": "\n".join(options["exclude"]), "destination": cfg.destination_root, "tv_folder": cfg.tv_folder, "movie_folder": cfg.movie_folder,
            "naming_style": cfg.naming_style, "audio_mode": settings.get("audio_mode", conn=conn), "clean_names": cfg.clean_names, "tmdb_ids": settings.get("naming_tmdb_ids", conn=conn) == "1"}


def describe_record(rec, rcm, cfg, profile=None, quality=None):
    """A record and its recommendation as the record page shows them. Once a decision is made the tracks are read-only and show what the job does."""
    streams = [s for s in v.json_list(rec.get("streams_json")) if isinstance(s, dict)]
    removals = {r["index"]: r for r in rcm.removals}
    decided = rec["approval_status"] == v.APPROVAL_APPROVED or rec["migration_status"] != v.MIGRATION_PENDING
    gone = migrate.approved_removals(rec, profile) if decided else {}
    for s in streams:
        s["language_name"] = rc.language_name(s.get("lang"))
        if decided:
            s["remove"], s["locked"] = False, False
            s["state"] = ("dropped" if gone[s["index"]] else "removed") if s["index"] in gone else "kept"
            continue
        s["remove"] = s["index"] in removals
        s["locked"] = bool(removals.get(s["index"], {}).get("locked"))
    plans = [p for p in v.json_list(rec.get("proposed_track_changes_json")) if isinstance(p, dict)]
    return {"rec": rec, "r": rcm, "streams": streams, "plans": plans, "size": _size(rec.get("size_bytes")), "duration": _duration(rec.get("duration_seconds")),
            "warnings": v.json_list(rec.get("warnings_json")), "errors": v.json_list(rec.get("errors_json")), "reasons": v.json_list(rec.get("review_reasons_json")),
            "quality": quality, "final_name": v.final_name(rec), "in_place": findings.in_place(rec, cfg), "missing": bool(rec.get("missing_since"))}


def queue_rows(conn, cfg, group, f):
    rows = [(rec, r) for rec, r in rc.classify(conn, cfg, f["q"] or None, f["root"] or None) if r.group == group]
    floor = CONFIDENCE_FLOORS.get(f["conf"])
    if floor:
        rows = [(rec, r) for rec, r in rows if r.confidence == floor]
    return rows


def register(app):
    @app.route("/library")
    def library_page():
        from synchub.web import media
        return media.render_media("library")

    @app.route("/library/options")
    def library_options_page():
        from synchub.web import media
        return media.render_media("options")

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
            cfg = rc.cfg_for(conn, rec, cfg, {})
            rcm = rc.recommend(rec, cfg, lambda plans: rule_steps.for_record(conn, rec, plans))
            events = store.events_for(conn, record_id)
            siblings = []
            if rec.get("duplicate_group"):
                siblings = [dict(r) for r in conn.execute("SELECT id, root_name, relative_path, duplicate_status, approval_status FROM library_file WHERE duplicate_group = ? AND id != ? ORDER BY id",
                                                           (rec["duplicate_group"], record_id))]
            chosen = migrate.profile_for(conn, rec)
            all_profiles = profiles.all_profiles(conn)
        group = request.args.get("group") or rcm.group
        data = describe_record(rec, rcm, cfg, chosen)
        with db.connection() as conn:
            size = size_policy.decide(rec, previews.plans_of(rec, approved=rec["approval_status"] == v.APPROVAL_APPROVED), size_policy.load(conn, chosen))
            latest = previews.latest_for(conn, "library_file", record_id)
            data["quality"] = rule_steps.quality_facts(conn, rec, previews.plans_of(rec, approved=rec["approval_status"] == v.APPROVAL_APPROVED))
        preview_info = {"summary": size.summary or "The size rules would compress this file; watch a short window first.", "latest": previews.describe(latest) if latest else None} if size.profile is not None else None
        data.update(preview_info=preview_info, cfg=cfg, profiles=all_profiles, chosen_profile=chosen, rename_to=rc.rename_path(rec, cfg), source_cleanup=cleanup.source_days())
        return render_template("library_record.html", group=group, group_title=rc.GROUP_TITLES.get(group, group), events=events, siblings=siblings, f=_filters(), **data)

    # ---- decisions
    def _decide(record_id, action):
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                by, cfg = _who(conn), scan.config(conn)
                if action == "approve":
                    keep = [int(i) for i in data.get("keep_streams") or []]
                    profile_id = int(data["profile_id"]) if str(data.get("profile_id") or "").isdigit() else None
                    outcome = rc.apply_recommendation(conn, record_id, by, name=data.get("name"), keep_streams=keep or None, note=data.get("note"), cfg=cfg,
                                                      mode=data.get("mode") or None, profile_id=profile_id)
                    message = {"approved": "Approved.", "rejected": "Skipped: that copy will not be migrated."}.get(outcome, outcome[:1].upper() + outcome[1:] + ".")
                elif action == "tested":
                    rc.approve_after_testing(conn, record_id, by, data.get("note"), cfg=cfg)
                    message = "Approved, with your note."
                elif action == "reject":
                    rc.reject(conn, record_id, by, data.get("note"))
                    message = "Rejected: it will not be migrated."
                elif action == "requeue":
                    migrate.requeue(conn, record_id, cfg, by)
                    message = "Queued again."
                elif action == "redo":
                    migrate.redo(conn, record_id, by, dest_removed=bool(data.get("dest_removed")))
                    message = "Back to pending: choose the settings and approve it again."
                    if data.get("rescan"):
                        rec = store.get_record(conn, record_id)
                        try:
                            scan.start_scan(conn, rec["root_name"], os.path.dirname(rec["relative_path"]), by, force=True)
                            message += " The folder is being scanned again."
                        except v.LibraryError as exc:
                            message += f" It was not scanned again: {exc}"
                elif action == "forget":
                    if not store.forget_missing(conn, [record_id]):
                        raise v.LibraryError("Only a file that is gone from its share, and that nobody is working on, can be taken off the list.")
                    message = "Taken off the list."
                elif action == "exclude-folder":
                    rec = store.get_record(conn, record_id)
                    folder = os.path.dirname(rec["relative_path"])
                    if not folder:
                        raise v.LibraryError("This file is at the top of its share; scan with a folder and use Folders to leave out under Settings instead.")
                    rule = f"{rec['root_name']}:{folder}"
                    current = scan.exclude_list(settings.get("library_exclude_folders", conn=conn))
                    if rule not in current:
                        settings.set_value("library_exclude_folders", "\n".join([*current, rule]), conn=conn)
                    removed = store.forget_folder(conn, rec["root_name"], folder)
                    message = f"Later scans leave out {folder}. {removed} undecided file(s) from it were taken off the list."
                elif action == "keep-source":
                    cleanup.hold(conn, record_id, by)
                    message = "The source will be kept."
                elif action == "release-source":
                    cleanup.release(conn, record_id, by)
                    message = "The source will be deleted when the waiting time is over."
                else:
                    rc.undo(conn, record_id, by)
                    message = "Decision undone."
        except (ValueError, v.LibraryError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message=message)

    @app.route("/api/library/record/<int:record_id>/<action>", methods=["POST"])
    def api_library_decide(record_id, action):
        if action not in ("approve", "reject", "undo", "tested", "requeue", "redo", "forget", "exclude-folder", "keep-source", "release-source"):
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
        with db.connection() as conn:
            all_profiles = profiles.all_profiles(conn)
            chosen = request.args.get("profile", type=int)
            chosen = chosen if chosen and any(p["id"] == chosen for p in all_profiles) else None
            summary = rule_steps.batch_summary(conn, [rec for rec, _ in candidates], chosen)
        return render_template("library_bulk.html", group=group, title=rc.GROUP_TITLES[group], f=f, floor="high" if floor == rc.HIGH else "high and medium", candidates=len(candidates),
                               sample=sample, size=_size(size), profiles=all_profiles, chosen_profile=chosen, summary=summary)

    @app.route("/api/library/bulk/<group>", methods=["POST"])
    def api_library_bulk(group):
        if group not in rc.GROUP_ORDER:
            return _fail("Unknown queue.", 404)
        data = request.get_json(silent=True) or {}
        floor = CONFIDENCE_FLOORS.get(str(data.get("conf") or ""), rc.MEDIUM)
        try:
            with db.connection() as conn:
                done, skipped = rc.bulk_approve(conn, group, _who(conn), floor, (data.get("q") or None), (data.get("root") or None), scan.config(conn), data.get("profile_id") or None)
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

    # ---- conversion profiles
    @app.route("/api/library/profiles", methods=["POST"])
    def api_library_profile_save():
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                profile_id = int(data["id"]) if str(data.get("id") or "").isdigit() else None
                profiles.save(conn, data, _who(conn), profile_id)
        except profiles.ProfileError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Profile saved.")

    @app.route("/api/library/profiles/<int:profile_id>/delete", methods=["POST"])
    def api_library_profile_delete(profile_id):
        try:
            with db.connection() as conn:
                profiles.delete(conn, profile_id)
        except profiles.ProfileError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Profile deleted.")

    # ---- scans, duplicates, settings
    @app.route("/api/library/scan", methods=["POST"])
    def api_library_scan():
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                run_id = scan.start_scan(conn, data.get("root"), data.get("prefix") or "", _who(conn), data.get("integrity") or None, bool(data.get("force")),
                                         data.get("exclude") if isinstance(data.get("exclude"), str) else None, data.get("profile_id") or None)
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

    @app.route("/api/library/forget_missing", methods=["POST"])
    def api_library_forget_missing():
        """Takes every record whose file is gone off the list (the Library page shows the count first)."""
        with db.connection() as conn:
            removed = store.forget_missing(conn)
        return jsonify(ok=True, removed=removed, message=f"Took {removed} missing file(s) off the list." if removed else "Nothing to clear.")

    @app.route("/api/library/settings", methods=["POST"])
    def api_library_settings():
        data = request.get_json(silent=True) or {}
        keys = ("library_destination_root", "library_tv_folder", "library_movie_folder", "library_integrity_mode", "library_tail_seconds", "library_exclude_folders", "download_pipeline", "originals_retention_days",
                "size_policy", "size_mode", "cap_mbps_2160", "cap_mbps_1080", "cap_mbps_720", "cap_mbps_sd", "size_trigger_factor", "safety_gb_2160", "safety_gb_1080", "tv_cap_share",
                "naming_style", "audio_mode", "clean_names", "naming_tmdb_ids", "versions_on", "variant_min_gb", "variant_height", "variant_label", "variant_crf", "variant_maxrate_kbps", "variant_audio_kbps", "variant_stereo", "dv7_remux", "size_crf", "tv_size_policy", "gpu_encoding", "vmaf_mode", "dolby_vision_profile_5", "dolby_vision_profile_7_8",
                "review_first", "trust_after", "preview_seconds", "preview_keep_days", "preview_auto",
                "library_default_profile", "source_delete_after_days", "arr_after_migration", "arr_apply_when")
        cleaned, errors = {}, []
        for key in keys:
            if key in data:
                ok, result = validate_setting(key, str(data[key]))
                (cleaned.__setitem__(key, result) if ok else errors.append(result))
        with db.connection() as conn:
            if "library_destination_root" in cleaned and storage.by_name(conn, cleaned["library_destination_root"]) is None:
                errors.append(f"There is no share called '{cleaned['library_destination_root']}'. Add it on the Storage page first.")
            if "library_default_profile" in cleaned and profiles.get(conn, int(cleaned["library_default_profile"])) is None:
                errors.append("That conversion profile no longer exists.")
            if errors:
                return _fail(" ".join(errors))
            for key, value in cleaned.items():
                settings.set_value(key, value, conn=conn)
            if cleaned:
                findings.refresh(conn, scan.config(conn))                   # a different destination or layout changes what is "in place"
        return jsonify(ok=True, message="Saved.")

    @app.route("/api/library/size_preview", methods=["POST"])
    def api_library_size_preview():
        """How many scanned files the size rules would compress with the values on the form (nothing is saved or queued)."""
        data = request.get_json(silent=True) or {}
        values = {}
        for key in ("cap_mbps_2160", "cap_mbps_1080", "cap_mbps_720", "cap_mbps_sd", "size_trigger_factor", "safety_gb_2160", "safety_gb_1080", "tv_cap_share"):
            ok, result = validate_setting(key, str(data.get(key, "")))
            if not ok:
                return jsonify(ok=False, error=result), 400
            values[key] = float(result)
        with db.connection() as conn:
            base = size_policy.load(conn)
            cfg = dataclasses.replace(base, on=True, tv_on=data.get("tv_size_policy") == "1", mode="bitrate", tv_cap_share=values["tv_cap_share"], trigger_factor=values["size_trigger_factor"],
                                      caps_mbps={"2160": values["cap_mbps_2160"], "1080": values["cap_mbps_1080"], "720": values["cap_mbps_720"], "sd": values["cap_mbps_sd"]},
                                      safety_gb={"2160": values["safety_gb_2160"], "1080": values["safety_gb_1080"]})
            count = {"movies": [0, 0], "episodes": [0, 0]}
            for row in conn.execute("SELECT * FROM library_file WHERE size_bytes > 0 AND missing_since IS NULL"):
                rec = dict(row)
                decision = size_policy.decide(rec, [], cfg)
                if decision.action in (size_policy.COMPRESS, size_policy.REVIEW):
                    kind = "movies" if (rec.get("media_type") or "").lower().startswith("movie") else "episodes"
                    count[kind][0] += 1
                    count[kind][1] += max(0, rec["size_bytes"] - (decision.numbers.get("target_gb") or 0) * size_policy.GIB)
        parts = [f"{n} {kind} (up to about {saved / size_policy.GIB:.0f} GB saved)" for kind, (n, saved) in count.items() if n]
        return jsonify(ok=True, message=("Would compress " + " and ".join(parts) + ".") if parts else "Nothing in the scanned library is over these limits.")

    # ---- files Radarr/Sonarr imported (the download pipeline)
    @app.route("/api/downloads/<int:intake_id>/<action>", methods=["POST"])
    def api_library_download_action(intake_id, action):
        if action not in ("approve", "skip", "retry", "rescan"):
            return _fail("Unknown action.", 404)
        try:
            with db.connection() as conn:
                who = _who(conn)
                if action == "approve":
                    downloads.approve(conn, intake_id, who)
                elif action == "skip":
                    downloads.skip(conn, intake_id, who)
                elif action == "retry":
                    downloads.retry(conn, intake_id)
                else:
                    downloads.ask_rescan_again(conn, intake_id)
        except ValueError as exc:
            return _fail(str(exc))
        if action == "rescan":
            downloads.rescan_due()
        return jsonify(ok=True, message={"approve": "Queued for a worker.", "skip": "Left as it is.", "retry": "Looking at it again.", "rescan": "Asked to rescan."}[action])

    # ---- preview clips (docs/DESIGN_ENCODING.md section 12)
    @app.route("/library/previews/<int:preview_id>")
    def library_preview(preview_id):
        with db.connection() as conn:
            row = previews.get(conn, preview_id)
            if row is None:
                return redirect(url_for("library_page"))
            rec = store.get_record(conn, row["record_id"]) if row["record_id"] else None
            data = previews.describe(row)
            data["seen_before"] = previews.approved_count(conn, row["signature"]) if row["signature"] else 0
            data["trust_after"] = previews.trust_after(conn)
            data["rule"] = previews.review_first(conn)
            data["size_gb"] = round((rec["size_bytes"] or 0) / previews.GIB, 1) if rec else None
            data["live"] = previews.live_for(conn, row["subject_type"], row["subject_id"])
            data["other"] = [previews.describe(r) for r in conn.execute("SELECT * FROM preview WHERE subject_type = ? AND subject_id = ? AND id != ? ORDER BY id DESC LIMIT 8",
                                                                         (row["subject_type"], row["subject_id"], preview_id))]
        return render_template("library_preview.html", p=data, rec=rec)

    @app.route("/library/previews/<int:preview_id>/clip/<name>")
    def library_preview_clip(preview_id, name):
        if name not in previews.CLIP_NAMES:
            return _fail("Unknown clip.", 404)
        path = previews.clip_path(preview_id, name)
        if not os.path.isfile(path):
            return _fail("That clip is not there (it may have expired).", 404)
        download = request.args.get("download") == "1"
        return send_file(path, mimetype="video/mp4", conditional=True, as_attachment=download, download_name=f"preview-{preview_id}-{name}.mp4", max_age=0)

    @app.route("/api/library/previews", methods=["POST"])
    def api_library_preview_create():
        data = request.get_json(silent=True) or {}
        try:
            subject_id = int(data.get("subject_id"))
        except (TypeError, ValueError):
            return _fail("Say which file to preview.")
        try:
            with db.connection() as conn:
                preview_id = previews.create(conn, data.get("subject_type") or "library_file", subject_id, _who(conn), mode=data.get("mode") or "random")
        except previews.PreviewError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, id=preview_id, url=url_for("library_preview", preview_id=preview_id), message="A worker will make the clips; this takes a few minutes.")

    @app.route("/api/library/previews/<int:preview_id>/<action>", methods=["POST"])
    def api_library_preview_action(preview_id, action):
        if action not in ("approve", "retarget", "another", "skip", "reject"):
            return _fail("Unknown action.", 404)
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                message = previews.decide(conn, preview_id, action, _who(conn), target_gb=data.get("target_gb"))
                row = previews.get(conn, preview_id)
                newest = previews.latest_for(conn, row["subject_type"], row["subject_id"]) if row else None
        except previews.PreviewError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message=message, next=url_for("library_preview", preview_id=newest["id"]) if action in ("retarget", "another") and newest else None)

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
