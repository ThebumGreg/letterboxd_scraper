"""The Library page: the original analysis and approval tool, inside SyncHub.

Scan the shares, see every file's plain-language recommendation sorted into queues, approve what is right (one by one, or in bulk with a
confirmation), and export the lot as a CSV. Nothing here touches a file: approvals are decisions, recorded with who made them; what is done
with approved files is a later step.
"""
import json
import dataclasses
import os
from datetime import datetime, timezone

from flask import Response, jsonify, redirect, render_template, request, send_file, url_for

from synchub import compression_test, db, downloads, jobs, previews, quality, runs as file_runs, settings, storage, versions
from synchub.library import aside, cleanup, file_labels, film_link, findings, hide, links, loose, migrate, profiles, recommend as rc, rules as profile_rules, report, rule_steps, scan, size_policy, store
from synchub.library import vocab as v
from settings_validation import validate_setting

PAGE_SIZE = 100
POLL_MAX_AGE = 10
CONFIDENCE_FLOORS = {"high": rc.HIGH, "medium": rc.MEDIUM}
FILTER_KEYS = ("q", "root", "conf", "what", "type", "quality", "label", "size", "res", "sort")


def _who(conn):
    from synchub.web import current_profile
    profile = current_profile(conn)
    return profile["name"] if profile else "guest"


def _with_previews(conn, rows):
    """Each import with the newest preview made for it, so the row can link to it."""
    cfg = scan.config(conn)
    for row in rows:
        found = previews.latest_for(conn, "download_import", row["id"])
        row["preview"] = previews.describe(found) if found else None
        row["can_tidy"], row["tidy_text"] = _tidy(conn, row, cfg)
    return rows


def _tidy(conn, row, cfg):
    """Whether an import that needs no conversion can still be approved as the Library would approve it (clean name, right share), and in words what that does."""
    if row["status"] != "nothing_to_do" or not row.get("record_id"):
        return False, ""
    rec = store.get_record(conn, row["record_id"])
    if rec is None or rec["approval_status"] != v.APPROVAL_PENDING or rec.get("missing_since"):
        return False, ""
    try:
        r = rc.recommend(rec, rc.cfg_for(conn, dict(rec), cfg, {}), held=quality.is_held(conn, rec["id"]))
    except Exception:                                             # a recommendation that cannot be worked out just hides the button
        return False, ""
    return bool(r.can_approve and not r.needs_name and (rec.get("recommended_action") in v.MIGRATABLE_ACTIONS)), r.headline


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


def overview(conn, max_age=0):
    """`max_age`: the background refresh may show recommendations worked out up to that many seconds ago (see library/cache.py)."""
    cfg = scan.config(conn)
    rows = rc.classify(conn, cfg, max_age=max_age)
    table = rc.counts(conn, cfg, rows)
    groups = [{"key": g, "title": rc.GROUP_TITLES[g], "hint": rc.GROUP_HINTS[g], **table[g]} for g in rc.GROUP_ORDER]
    loose_ids = loose.loose_ids(conn)                      # loose films by the list they are in (done and rejected ones need nothing)
    loose_count = sum(1 for rec, r in rows if rec["id"] in loose_ids)      # the same films the "All files + loose" list shows, held (below minimum quality) ones included
    runs = scan.list_runs(conn, 12)
    for run in runs:
        run["percent"] = None if run["progress"] is None else round(run["progress"] * 100)
        run["when"] = (run["created_at"] or "")[:16]
    options = scan_options_for_page(conn)
    migration = migrate.queue_summary(conn)
    return {"quality_box": quality.counts(conn, max_age), "loose_count": loose_count, "cfg": cfg, "groups": groups, "total": len(rows), "shares": shares_summary(conn, cfg), "runs": runs, "active": any(r["active"] for r in runs) or bool(migration["queued"] or migration["running"]) or bool(downloads.summary(conn)["working"]), "options": options,
            "roots": [r["name"] for r in storage.list_all(conn)], "migration": migration,
            "downloads": _with_previews(conn, downloads.recent(conn, 15)), "preview_summary": previews.summary(conn), "review_first": previews.review_first(conn), "trust_after": previews.trust_after(conn),
            "preview_seconds": previews.clip_seconds(conn), "preview_keep_days": previews.keep_days(conn), "preview_auto": previews.auto_make(conn), "download_summary": downloads.summary(conn), "originals_days": downloads.retention_days(conn), "originals_where": downloads.originals_where(conn), "size": size_policy.load(conn), "lookups": {"confidence": CONFIDENCE_FLOORS},
            "profiles": profiles.all_profiles(conn), "default_profile_id": profiles.default(conn)["id"], "cleanup": cleanup.summary(conn), "attention": migrate.attention(conn), "missing_count": store.count_missing(conn),
            "rule_defaults": profile_rules.defaults(conn), "versions_config": dataclasses.asdict(versions.load(conn)), "heights": (480, 720, 1080, 1440),
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


QUALITY_FILTERS = ("waiting", "unchecked", "good", "poor", "small")


def _quality_matches(state, wanted):
    check, result = state["check"], state["result"]
    live = bool(check and check["status"] in quality.LIVE)
    grade = (result or {}).get("grade") if state["current"] else None
    if wanted == "waiting":
        return live
    if wanted == "unchecked":
        return not live and result is None and not state["small"]
    if wanted == "small":
        return state["small"]
    if wanted == "good":
        return grade in ("good", "ok") and not state["low"]
    return (state["low"] and not state["small"]) or grade in ("poor", "bad")


# The one list page (/library/queue): every queue of the Library card, the files below minimum quality and everything at once, chosen with the Status filter.
STATUS_BELOW, STATUS_ALL = "below_minimum", "all"
STATUS_TITLES = {STATUS_BELOW: "Below minimum quality", STATUS_ALL: "All files"}
STATUS_HINTS = {STATUS_ALL: "Every file the Library knows, whatever its state. Choose a status to work through one list."}


def status_of(value):
    """The list a `status` value names; anything unknown shows everything."""
    value = (value or "").strip()
    if value in rc.GROUP_ORDER or value == STATUS_BELOW:
        return value
    return STATUS_BELOW if value in (rc.G_HELD, "quality") else STATUS_ALL


def rows_for_status(conn, classified, status, f):
    """The (record, recommendation) rows of one status after the filters and the chosen sort. Below minimum quality is the flagged list (worst first), which is not one of the
    classifier's groups: a flagged file somebody approved or rejected sits in another group and is still listed here."""
    if status == STATUS_BELOW:
        order = {d["id"]: n for n, d in enumerate(quality._flagged(conn))}
        rows = sorted(((rec, r) for rec, r in classified if rec["id"] in order), key=lambda row: order[row[0]["id"]])
        return sort_rows(conn, rows_in_queue(conn, rows, None, f), f["sort"])
    return sort_rows(conn, rows_in_queue(conn, classified, None if status == STATUS_ALL else status, f), f["sort"])


# The Sort box of the list page: (value, label). The empty value keeps the list's own order (by name, or worst first on Below minimum quality). Ties keep that order.
SORTS = (("name", "Name"), ("size", "Size, largest first"), ("size_asc", "Size, smallest first"), ("resolution", "Resolution, highest first"), ("newest", "Newest file"),
         ("confidence", "Confidence, lowest first"), ("quality", "Quality score, worst first"), ("saving", "Estimated saving, largest first"))
_CONFIDENCE_RANK = {rc.LOW: 0, rc.MEDIUM: 1, rc.HIGH: 2}
_RESOLUTION_RANK = {"2160": 0, "1080": 1, "720": 2, "sd": 3, "unknown": 4}


def _quality_scores(conn):
    """{file id: score of its latest finished quality check}; a file with none (or no score) is left out."""
    scores = {}
    for r in conn.execute("SELECT library_file_id, score FROM quality_check WHERE mode = 'quality' AND status = 'done' ORDER BY id"):
        if r["score"] is None:
            scores.pop(r["library_file_id"], None)
        else:
            scores[r["library_file_id"]] = r["score"]
    return scores


def _estimated_saving(rec, policy):
    """Bytes the size rules would take off this file (0 when it would not be compressed)."""
    try:
        decision = size_policy.decide(rec, previews.plans_of(rec), policy)
    except Exception:                                      # a record the rules cannot read sorts as no saving
        return 0
    if decision.action != size_policy.COMPRESS or not decision.profile:
        return 0
    return max((rec.get("size_bytes") or 0) - (decision.profile.get("target_bytes") or 0), 0)


def sort_rows(conn, rows, key):
    """The (record, recommendation) rows in the order `key` names (see SORTS); an unknown or empty key leaves them as they are."""
    if key == "name":
        return sorted(rows, key=lambda row: (row[0].get("approved_name") or row[0].get("recommended_name") or row[0]["file_name"] or "").lower())
    if key == "size":
        return sorted(rows, key=lambda row: -(row[0].get("size_bytes") or 0))
    if key == "size_asc":
        return sorted(rows, key=lambda row: row[0].get("size_bytes") or 0)
    if key == "resolution":
        return sorted(rows, key=lambda row: (_RESOLUTION_RANK[resolution_of(row[0])], -(row[0].get("size_bytes") or 0)))
    if key == "newest":
        return sorted(rows, key=lambda row: -(row[0].get("mtime") or 0))
    if key == "confidence":
        return sorted(rows, key=lambda row: _CONFIDENCE_RANK.get(row[1].confidence, 3))
    if key == "quality":
        scores = _quality_scores(conn)
        return sorted(rows, key=lambda row: (row[0]["id"] not in scores, scores.get(row[0]["id"], 0)))     # worst first, files without a score last
    if key == "saving":
        policy = size_policy.load(conn)
        return sorted(rows, key=lambda row: -_estimated_saving(row[0], policy))
    return rows


def queue_view(status):
    """The list page for one status, with the filters and the page number from the address."""
    from synchub.web import quality_web
    f = _filters()
    page = max(int(request.args.get("page") or 1), 1) if (request.args.get("page") or "1").isdigit() else 1
    group = status if status in rc.GROUP_ORDER else None
    below = status == STATUS_BELOW
    with db.connection() as conn:
        cfg = scan.config(conn)
        everything = rc.classify(conn, cfg, f["q"] or None, f["root"] or None)
        all_rows = rows_for_status(conn, everything, status, f)
        table = rc.counts(conn, cfg, everything)
        flagged = len(quality._flagged(conn, f["q"] or None, f["root"] or None))
        candidates = [(rec, r) for rec, r in all_rows if r.can_approve and not r.needs_name and r.confidence in (rc.HIGH, rc.MEDIUM) and r.kind not in ("keep_duplicate", "skip_duplicate")] if group else []
        roots = [r["name"] for r in storage.list_all(conn)]
    shown = all_rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    extra = {}
    with db.connection() as conn:
        loose_set = loose.loose_ids(conn)
        quality_state = {rec["id"]: quality.state_for(conn, rec) for rec, _ in shown if below or str(rec.get("media_type") or "").startswith("Movie")}
        if below:
            from synchub import ai_test
            data = quality_web.overview(conn, full=True)
            extra = {"qual": data, "ai": ai_test.overview(conn), "values": data["values"], "flag_below": data["flag_below"], "version": data["version"],
                     "waiting": data["waiting"], "recent": data["recent"], "quality_counts": data["counts"]}
    rows = [{"rec": rec, "r": r, "size": _size(rec["size_bytes"]) if not rec.get("unreadable_reason") else None, "loose": rec["id"] in loose_set,
             "quality": quality_state.get(rec["id"])} for rec, r in shown]
    view = "film" if request.args.get("view") == "film" else "file"
    films, film_total = [], 0
    with db.connection() as conn:
        file_counts = file_labels.counts(conn)
        hidden_count = len(file_labels.ids_for(conn, "hidden"))
        if view == "film":
            films, film_total = films_with_many_files(conn, f, page)
    statuses = [(STATUS_ALL, "All files", len(everything))] + [(g, rc.GROUP_TITLES[g], table[g]["total"]) for g in rc.GROUP_ORDER] + [(STATUS_BELOW, STATUS_TITLES[STATUS_BELOW], flagged)]
    return render_template("library_queue.html", status=status, group=group, title=STATUS_TITLES.get(status) or rc.GROUP_TITLES[status],
                           hint=STATUS_HINTS.get(status) or rc.GROUP_HINTS.get(rc.G_HELD if below else status, ""), rows=rows, total=film_total if view == "film" else len(all_rows), page=page,
                           pages=max((len(all_rows) + PAGE_SIZE - 1) // PAGE_SIZE, 1), view=view, films=films, size_buckets=SIZE_BUCKETS, resolutions=RESOLUTIONS, sorts=SORTS, resolution_of=resolution_of, file_counts=file_counts, hidden_count=hidden_count, f=f, statuses=statuses, titles=rc.GROUP_TITLES, table=table,
                           high=sum(1 for _, r in candidates if r.confidence == rc.HIGH), high_medium=len(candidates), roots=roots, **extra)


def films_with_many_files(conn, f, page):
    """The By film list: films that have more than one visible file, narrowed by the search and the kind of file. Returns (the page's films, how many films)."""
    ids = [r["media_id"] for r in conn.execute("SELECT media_id FROM library_file WHERE media_id IS NOT NULL AND episode_id IS NULL AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside' "
                                                "GROUP BY media_id HAVING COUNT(*) > 1")]
    wanted = file_labels.ids_for(conn, f["label"]) if f["label"] in file_labels.FILTERS else None
    out = []
    for media_id in ids:
        m = conn.execute("SELECT title, year FROM media WHERE id = ?", (media_id,)).fetchone()
        if m is None or (f["q"] and f["q"].lower() not in (m["title"] or "").lower()):
            continue
        files = [dict(r) for r in conn.execute("SELECT id, file_name, size_bytes, file_label, edition_name, dup_review FROM library_file WHERE media_id = ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside' ORDER BY is_main DESC, size_bytes DESC", (media_id,))]
        if wanted is not None and not any(x["id"] in wanted for x in files):
            continue
        for x in files:
            x["size"] = _size(x["size_bytes"])
        out.append({"media_id": media_id, "title": m["title"], "year": m["year"], "files": files, "unexplained": any(x["file_label"] == file_labels.UNEXPLAINED for x in files)})
    out.sort(key=lambda x: (not x["unexplained"], (x["title"] or "").lower()))
    return out[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], len(out)


def queue_rows(conn, cfg, group, f):
    return rows_in_queue(conn, rc.classify(conn, cfg, f["q"] or None, f["root"] or None), group, f)


# The Size and Resolution filters of the list page: (value, label, lowest GB, highest GB) and (value, label) of the picture class the size rules use.
SIZE_BUCKETS = (("under5", "Under 5 GB", 0, 5), ("5to15", "5 to 15 GB", 5, 15), ("15to30", "15 to 30 GB", 15, 30), ("30to60", "30 to 60 GB", 30, 60), ("over60", "Over 60 GB", 60, None))
RESOLUTIONS = (("2160", "4K (2160p)"), ("1080", "1080p"), ("720", "720p"), ("sd", "Smaller than 720p"), ("unknown", "Not read yet"))


def size_matches(rec, wanted):
    bucket = next((b for b in SIZE_BUCKETS if b[0] == wanted), None)
    if bucket is None:
        return True
    gb = (rec.get("size_bytes") or 0) / 1024 ** 3
    return gb >= bucket[2] and (bucket[3] is None or gb < bucket[3])


def resolution_of(rec):
    """'2160', '1080', '720', 'sd' or 'unknown': the picture class of a record (the one the size rules use, so a wide film cropped to 800 lines is 1080)."""
    try:
        width, height = [int(x) for x in str(rec.get("resolution") or "").lower().split("x")]
    except ValueError:
        return "unknown"
    return size_policy.picture_class(width, height)


def rows_in_queue(conn, classified, group, f):
    """The rows of one group (every row when `group` is None), narrowed by the filters."""
    rows = [(rec, r) for rec, r in classified if group is None or r.group == group]
    floor = CONFIDENCE_FLOORS.get(f["conf"])
    if floor:
        rows = [(rec, r) for rec, r in rows if r.confidence == floor]
    rows = loose.narrow(conn, rows, f["what"])
    if f["type"] == "movies":
        rows = [(rec, r) for rec, r in rows if str(rec.get("media_type") or "").startswith("Movie")]
    elif f["type"] == "tv":
        rows = [(rec, r) for rec, r in rows if rec.get("media_type") == "TV Episode"]
    if f["label"] in file_labels.FILTERS:
        wanted = file_labels.ids_for(conn, f["label"])
        rows = [(rec, r) for rec, r in rows if rec["id"] in wanted]
    if f["size"] in {b[0] for b in SIZE_BUCKETS}:
        rows = [(rec, r) for rec, r in rows if size_matches(rec, f["size"])]
    if f["res"] in {x[0] for x in RESOLUTIONS}:
        rows = [(rec, r) for rec, r in rows if resolution_of(rec) == f["res"]]
    if f["quality"] in QUALITY_FILTERS:
        rows = [(rec, r) for rec, r in rows if _quality_matches(quality.state_for(conn, rec), f["quality"])]
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
            data = overview(conn, max_age=POLL_MAX_AGE if request.args.get("poll") else 0)       # the timer may use a result a few seconds old; a button press never does
        return render_template("_library_live.html", **data)

    # ---- the queues
    @app.route("/library/queue")
    def library_list():
        """Every Library list on one page: `?status=` chooses the queue, Below minimum quality or everything."""
        return queue_view(status_of(request.args.get("status")))

    @app.route("/library/sort")
    def library_sort():
        """Sort out a film: one unexplained file at a time next to the film's main file, with the choices that explain it. `media` names a film; `skip` lists films already passed."""
        skip = {int(x) for x in (request.args.get("skip") or "").split(",") if x.isdigit()}
        wanted = request.args.get("media")
        with db.connection() as conn:
            todo = [r["media_id"] for r in conn.execute("SELECT DISTINCT media_id FROM library_file WHERE file_label = ? AND media_id IS NOT NULL AND episode_id IS NULL AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside' ORDER BY media_id", (file_labels.UNEXPLAINED,))]
            candidates = [int(wanted)] if wanted and wanted.isdigit() else [m for m in todo if m not in skip]
            film = current = None
            files, others, left = [], [], 0
            for n, media_id in enumerate(candidates):
                row = conn.execute("SELECT id, title, year FROM media WHERE id = ?", (media_id,)).fetchone()
                if row is None:
                    continue
                files = [dict(r) for r in conn.execute("SELECT id, file_name, root_name, relative_path, size_bytes, resolution, video_codec, file_label, is_main FROM library_file WHERE media_id = ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside' ORDER BY is_main DESC, size_bytes DESC", (media_id,))]
                main = quality.copies(conn, media_id).get("main")
                for x in files:
                    x["is_main"] = x["is_main"] or bool(main and x["id"] == main["id"])
                    x["size"] = _size(x["size_bytes"])
                current = next((x for x in files if x["file_label"] == file_labels.UNEXPLAINED and not x["is_main"]), None)
                if current is not None:
                    film, left = row, len(candidates) - n - 1 if not wanted else len([m for m in todo if m not in skip and m != media_id])
                    break
        others = [x for x in files if current is not None and x["id"] != current["id"]]
        shown = film["id"] if film else None
        return render_template("library_sort.html", film=film, current=current, others=others, left=left, skip=",".join(str(x) for x in sorted(skip | ({shown} if shown else set()))), keep_skip=",".join(str(x) for x in sorted(skip)))

    @app.route("/library/review/<group>")
    def library_review(group):
        if group not in rc.GROUP_ORDER:
            return redirect(url_for("library_page"))
        f = _filters()
        after = request.args.get("after")
        with db.connection() as conn:
            ids = [rec["id"] for rec, _ in rows_for_status(conn, rc.classify(conn, scan.config(conn), f["q"] or None, f["root"] or None), group, f)]     # the same list, filters and sort as the page
        after = int(after) if after and after.isdigit() else None
        nxt = (ids[ids.index(after) + 1] if ids.index(after) + 1 < len(ids) else ids[0]) if ids and after in ids else (ids[0] if ids else None)
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
            rcm = rc.recommend(rec, cfg, lambda plans: rule_steps.for_record(conn, rec, plans), held=quality.is_held(conn, rec['id']))
            if quality.waiting_to_convert(conn, rec["id"]):
                rcm.notes = rcm.notes + ["Waiting for its quality check (and the compression test, when it compresses) before the conversion job is made. A file that passes is converted; a low quality, damaged or failed one is held."]
            if migrate.waiting_for_slot(conn, rec):
                rcm.notes = rcm.notes + [f"Approved, waiting for a free place: the limit is {migrate.max_in_flight(conn)} conversions at once (Options > Quality). It is queued as soon as one finishes."]
            events = store.events_for(conn, record_id)
            siblings = []
            if rec.get("duplicate_group"):
                siblings = [dict(r) for r in conn.execute("SELECT id, root_name, relative_path, duplicate_status, approval_status FROM library_file WHERE duplicate_group = ? AND id != ? ORDER BY id",
                                                           (rec["duplicate_group"], record_id))]
            chosen = migrate.profile_for(conn, rec)
            all_profiles = profiles.all_profiles(conn)
        group = request.args.get("group") or rcm.group
        back_status = status_of(request.args.get("status") or group)             # the list the person came from: its filters come along in `f`
        back_title = STATUS_TITLES.get(back_status) or rc.GROUP_TITLES.get(back_status, back_status)
        data = describe_record(rec, rcm, cfg, chosen)
        with db.connection() as conn:
            size = size_policy.decide(rec, previews.plans_of(rec, approved=rec["approval_status"] == v.APPROVAL_APPROVED), size_policy.load(conn, chosen))
            latest = previews.latest_for(conn, "library_file", record_id)
            data["quality_state"] = quality.state_for(conn, rec)
            data["compare"] = quality.compare(conn, rec)
            data["related"] = quality.related(conn, rec)
            data["film"], data["film_files"] = None, []
            if rec.get("media_id"):
                data["film"] = conn.execute("SELECT id, title, year FROM media WHERE id = ? AND kind = 'movie'", (rec["media_id"],)).fetchone()
                data["film_files"] = [dict(x) for x in conn.execute("SELECT id, root_name, file_name, file_label, dup_review, edition_name, duplicate_status FROM library_file WHERE media_id = ? AND id != ? "
                                                                    "AND missing_since IS NULL ORDER BY size_bytes DESC", (rec["media_id"], record_id))]
            data["ctest"] = compression_test.describe(conn, rec)
            data["quality"] = rule_steps.quality_facts(conn, rec, previews.plans_of(rec, approved=rec["approval_status"] == v.APPROVAL_APPROVED))
            file_jobs = links.jobs_for_record(conn, record_id)
            past_runs = file_runs.for_file(conn, rec)
        preview_info = {"summary": size.summary or "The size rules would compress this file; watch a short window first.", "latest": previews.describe(latest) if latest else None} if size.profile is not None else None
        data.update(preview_info=preview_info, cfg=cfg, profiles=all_profiles, chosen_profile=chosen, rename_to=rc.rename_path(rec, cfg), source_cleanup=cleanup.source_days())
        return render_template("library_record.html", group=group, group_titles=rc.GROUP_TITLES, group_title=rc.GROUP_TITLES.get(group, group), events=events, siblings=siblings, runs=past_runs, runs_json=json.dumps(past_runs, default=str).replace("</", "<\\/"), f=_filters(), file_jobs=file_jobs, back_status=back_status, back_title=back_title, **data)

    @app.route("/library/record/<int:record_id>/step/<direction>")
    def library_record_step(record_id, direction):
        """Previous or Next on a record page: the neighbour in the list the record was opened from, with the same filters. Worked out on the click, so opening
        a record does not have to sort the whole library. Past either end, or when the record has left that list, it goes back to the list."""
        f = _filters()
        keep = {k: x for k, x in f.items() if x}
        status = status_of(request.args.get("status") or request.args.get("group"))
        with db.connection() as conn:
            cfg = scan.config(conn)
            ids = [rec["id"] for rec, _ in rows_for_status(conn, rc.classify(conn, cfg, f["q"] or None, f["root"] or None), status, f)]
        at = ids.index(record_id) + (-1 if direction == "previous" else 1) if record_id in ids else -1
        if not 0 <= at < len(ids):
            return redirect(url_for("library_list", status=status, **keep))
        return redirect(url_for("library_record", record_id=ids[at], status=status, **keep))

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
                    if outcome == "approved" and quality.waiting_to_convert(conn, record_id):
                        message = "Approved. A quality check (and a compression test, when it compresses) runs first; a file that passes is converted, a low quality, damaged or failed one is held."
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
                elif action in ("hide-source", "show-source"):
                    hide.request(conn, record_id, undo=action == "show-source")
                    message = "A worker will hide the original from Jellyfin shortly." if action == "hide-source" else "A worker will make the original visible to Jellyfin again shortly."
                elif action == "delete-source-now":
                    cleanup.delete_now(conn, record_id, by)
                    message = "Queued: a worker checks the converted copy and then deletes the original."
                elif action == "release-source":
                    cleanup.release(conn, record_id, by)
                    message = "The source will be deleted when the waiting time is over."
                else:
                    rc.undo(conn, record_id, by)
                    message = "Decision undone."
        except (ValueError, v.LibraryError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message=message)

    @app.route("/api/library/hide-originals", methods=["POST"])
    def api_library_hide_originals():
        with db.connection() as conn:
            count = hide.backfill(conn)
        return jsonify(ok=True, message=f"Queued {count} original(s) to be hidden from Jellyfin." if count else "There are no visible originals to hide.")

    # ---- what a file is to its film (labels, edition, main file, "not this film", duplicates a person has looked at)
    def _film_file(conn, record_id):
        rec = conn.execute("SELECT * FROM library_file WHERE id = ?", (record_id,)).fetchone()
        if rec is None:
            raise v.LibraryError("That file is not in the Library.")
        return dict(rec)

    @app.route("/api/library/file/<int:record_id>/edition", methods=["POST"])
    def api_library_file_edition(record_id):
        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip()[:60]
        on = bool(data.get("on", True))
        try:
            with db.connection() as conn:
                rec = _film_file(conn, record_id)
                conn.execute("UPDATE library_file SET is_edition = ?, edition_name = ? WHERE id = ?", (1 if on else 0, (name or None) if on else None, record_id))
                file_labels.refresh(conn, [rec["media_id"]] if rec["media_id"] else None)
        except v.LibraryError as exc:
            return _fail(str(exc), 404)
        return jsonify(ok=True, message="Marked as a different edition." if on else "No longer marked as an edition.")

    @app.route("/api/library/file/<int:record_id>/mobile", methods=["POST"])
    def api_library_file_mobile(record_id):
        """Marks a file as a mobile (smaller) copy of the film by hand, without renaming anything on the share."""
        on = bool((request.get_json(silent=True) or {}).get("on", True))
        try:
            with db.connection() as conn:
                rec = _film_file(conn, record_id)
                conn.execute("UPDATE library_file SET is_mobile = ? WHERE id = ?", (1 if on else 0, record_id))
                file_labels.refresh(conn, [rec["media_id"]] if rec["media_id"] else None)
        except v.LibraryError as exc:
            return _fail(str(exc), 404)
        return jsonify(ok=True, message="Marked as a mobile copy." if on else "No longer marked as a mobile copy.")

    @app.route("/api/library/file/<int:record_id>/main", methods=["POST"])
    def api_library_file_main(record_id):
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                rec = _film_file(conn, record_id)
                if not rec["media_id"]:
                    return _fail("This file is not linked to a film yet.")
                if rec.get("episode_id") is not None:
                    conn.execute("UPDATE library_file SET is_main = 0 WHERE episode_id = ?", (rec["episode_id"],))
                else:
                    conn.execute("UPDATE library_file SET is_main = 0 WHERE media_id = ? AND episode_id IS NULL", (rec["media_id"],))
                if data.get("on", True):
                    conn.execute("UPDATE library_file SET is_main = 1 WHERE id = ?", (record_id,))
        except v.LibraryError as exc:
            return _fail(str(exc), 404)
        return jsonify(ok=True, message="SyncHub now treats this as the main file of the film." if data.get("on", True) else "SyncHub picks the main file by itself again.")

    @app.route("/api/library/file/<int:record_id>/not-this-film", methods=["POST"])
    def api_library_file_not_this_film(record_id):
        try:
            with db.connection() as conn:
                rec = _film_file(conn, record_id)
                film_link.not_this_film(conn, record_id)
                file_labels.refresh(conn, [rec["media_id"]] if rec["media_id"] else None)
        except v.LibraryError as exc:
            return _fail(str(exc), 404)
        return jsonify(ok=True, message="Saved: this file is no longer counted as part of that film.")

    @app.route("/api/library/file/<int:record_id>/duplicate-note", methods=["POST"])
    def api_library_file_duplicate_note(record_id):
        value = str((request.get_json(silent=True) or {}).get("value") or "")
        if value not in ("", "keep_both", "review_later"):
            return _fail("Unknown choice.")
        try:
            with db.connection() as conn:
                rec = _film_file(conn, record_id)
                if rec["dup_review"] == "aside":
                    return _fail("This file is set aside: put it back first.")
                conn.execute("UPDATE library_file SET dup_review = ? WHERE id = ?", (value or None, record_id))
        except v.LibraryError as exc:
            return _fail(str(exc), 404)
        return jsonify(ok=True, message={"keep_both": "Both copies are kept.", "review_later": "Marked to review later.", "": "Back to undecided."}[value])

    @app.route("/api/library/file/<int:record_id>/<action>", methods=["POST"])
    def api_library_file_aside(record_id, action):
        if action not in ("set-aside", "put-back"):
            return _fail("Unknown action.", 404)
        try:
            with db.connection() as conn:
                aside.request(conn, record_id, undo=action == "put-back", reason=str((request.get_json(silent=True) or {}).get("reason") or "") or None)
        except aside.AsideError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="A worker will rename the file shortly. Nothing is deleted." if action == "set-aside" else "A worker will put the file back shortly.")

    @app.route("/api/library/file/<int:record_id>/replace-main", methods=["POST"])
    def api_library_file_replace_main(record_id):
        """This file is the new main file: the film's current main file is hidden as an old version."""
        try:
            with db.connection() as conn:
                rec = _film_file(conn, record_id)
                if not rec["media_id"]:
                    raise aside.AsideError("This file is not linked to a film yet. Press Refresh links first.")
                episode = rec.get("episode_id")
                if episode is not None:
                    siblings = [dict(r) for r in conn.execute("SELECT id, is_main, resolution, size_bytes, file_label FROM library_file WHERE episode_id = ? AND missing_since IS NULL AND COALESCE(dup_review, '') != 'aside'", (episode,))]
                    old = {"id": file_labels.main_id(siblings, {f["id"]: f["file_label"] for f in siblings})} if siblings else None
                else:
                    old = quality.copies(conn, rec["media_id"]).get("main")
                if old is None or old["id"] == record_id:
                    raise aside.AsideError("This file is already the main file.")
                if episode is not None:
                    conn.execute("UPDATE library_file SET is_main = 0 WHERE episode_id = ?", (episode,))
                else:
                    conn.execute("UPDATE library_file SET is_main = 0 WHERE media_id = ? AND episode_id IS NULL", (rec["media_id"],))
                conn.execute("UPDATE library_file SET is_main = 1 WHERE id = ?", (record_id,))
                aside.request(conn, old["id"], reason="old_version")
        except (aside.AsideError, v.LibraryError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="This is now the main file. A worker will hide the old main file shortly.")

    @app.route("/api/library/links/refresh", methods=["POST"])
    def api_library_links_refresh():
        with db.connection() as conn:
            result = film_link.relink_all(conn)
        return jsonify(ok=True, message=f"Linked {result['files']} file(s) to {result['films']} film(s) and {result.get('episodes', 0)} episode file(s); {result['with_more_than_one']} film(s) or episode(s) have more than one file.")

    @app.route("/api/library/record/<int:record_id>/<action>", methods=["POST"])
    def api_library_decide(record_id, action):
        if action not in ("approve", "reject", "undo", "tested", "requeue", "redo", "forget", "exclude-folder", "keep-source", "release-source", "delete-source-now", "hide-source", "show-source"):
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
            candidates = [(rec, r) for rec, r in rc.bulk_candidates(conn, group, floor, f["q"] or None, f["root"] or None, cfg, f["what"] or None) if r.kind not in ("keep_duplicate", "skip_duplicate")]
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
                done, skipped = rc.bulk_approve(conn, group, _who(conn), floor, (data.get("q") or None), (data.get("root") or None), scan.config(conn), data.get("profile_id") or None, data.get("what") or None)
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

    @app.route("/api/library/forget_folder", methods=["POST"])
    def api_library_forget_folder():
        """Takes the undecided and rejected records of a share (or of one folder in it) off the list, so a scan of the wrong place can be cleared and redone."""
        data = request.get_json(silent=True) or {}
        root, folder = str(data.get("root") or "").strip(), str(data.get("folder") or "").strip().strip("/")
        with db.connection() as conn:
            if not root:
                return jsonify(ok=False, message="Choose a share first."), 400
            removed = store.forget_folder(conn, root, folder)
        return jsonify(ok=True, removed=removed, message=f"Took {removed} file(s) off the list. Nothing on the share was touched." if removed else "Nothing to clear there.")

    @app.route("/api/library/settings", methods=["POST"])
    def api_library_settings():
        data = request.get_json(silent=True) or {}
        keys = ("library_destination_root", "library_tv_folder", "library_movie_folder", "library_integrity_mode", "library_tail_seconds", "library_exclude_folders", "download_pipeline", "originals_retention_days", "originals_where",
                "size_policy", "cap_mbps_2160", "cap_mbps_1080", "cap_mbps_720", "cap_mbps_sd", "size_trigger_factor", "trigger_mbps_2160", "trigger_mbps_1080", "trigger_mbps_720", "trigger_mbps_sd", "rate_mode", "safety_gb_2160", "safety_gb_1080", "tv_cap_share",
                "naming_style", "audio_mode", "clean_names", "naming_tmdb_ids", "versions_on", "variant_min_gb", "variant_height", "variant_label", "variant_crf", "variant_maxrate_kbps", "variant_audio_kbps", "variant_stereo", "size_crf", "tv_size_policy", "gpu_encoding", "vmaf_mode", "dolby_vision_profile_5", "dolby_vision_profile_7_8",
                "review_first", "trust_after", "preview_seconds", "preview_keep_days", "preview_auto",
                "library_default_profile", "source_delete_after_days", "hide_source", "arr_after_migration", "arr_apply_when")
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
        for key in ("cap_mbps_2160", "cap_mbps_1080", "cap_mbps_720", "cap_mbps_sd", "trigger_mbps_2160", "trigger_mbps_1080", "trigger_mbps_720", "trigger_mbps_sd", "safety_gb_2160", "safety_gb_1080", "tv_cap_share"):
            ok, result = validate_setting(key, str(data.get(key, "")))
            if not ok:
                return jsonify(ok=False, error=result), 400
            values[key] = float(result)
        with db.connection() as conn:
            base = size_policy.load(conn)
            cfg = dataclasses.replace(base, on=True, tv_on=data.get("tv_size_policy") == "1", tv_cap_share=values["tv_cap_share"],
                                      trigger_mbps={"2160": values["trigger_mbps_2160"], "1080": values["trigger_mbps_1080"], "720": values["trigger_mbps_720"], "sd": values["trigger_mbps_sd"]},
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
        if action not in ("approve", "skip", "retry", "rescan", "tidy"):
            return _fail("Unknown action.", 404)
        try:
            with db.connection() as conn:
                who = _who(conn)
                if action == "approve":
                    downloads.approve(conn, intake_id, who)
                elif action == "tidy":
                    row = downloads.get(conn, intake_id)
                    if row is None or row["status"] != "nothing_to_do" or not row["record_id"]:
                        raise ValueError("This import is not waiting to be tidied.")
                    rc.apply_recommendation(conn, row["record_id"], who, cfg=scan.config(conn))
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
        return jsonify(ok=True, message={"approve": "Queued for a worker.", "skip": "Left as it is.", "retry": "Looking at it again.", "rescan": "Asked to rescan.", "tidy": "Approved: it is renamed and moved like any Library approval."}[action])

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
