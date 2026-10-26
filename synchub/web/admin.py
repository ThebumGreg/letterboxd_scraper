"""Settings, profiles, tasks, logs, payload log, DDNS and export."""
import csv
import io
import re
import time
from collections import deque

from datetime import datetime

from flask import Response, flash, jsonify, redirect, render_template, request, send_file, url_for

from config import LOG_FILE_PATH
from settings_validation import validate_setting
from synchub import catalog, db, ddns, ddns_check, jellyfin_events, profiles, settings, tasks, webhooks
from synchub.profiles import ProfileError
from synchub.web import current_limit

# name -> (job, message shown after starting it)
TRIGGERS = {
    "jellyfin_movies": (tasks.with_reconcile(tasks.sync_jellyfin_movies), "Jellyfin movie sync started."),
    "jellyfin_tv": (tasks.with_reconcile(tasks.sync_jellyfin_tv), "Jellyfin TV sync started."),
    "watch": (tasks.sync_watch_state, "Watch history sync started."),
    "radarr": (tasks.with_reconcile(tasks.sync_radarr), "Radarr sync started."),
    "sonarr": (tasks.with_reconcile(tasks.sync_sonarr), "Sonarr sync started."),
    "letterboxd": (tasks.with_reconcile(tasks.scrape_letterboxd), "Letterboxd scrape started."),
    "serializd": (tasks.with_reconcile(tasks.scrape_serializd), "Serializd scrape started."),
    "id_resolver": (tasks.with_reconcile(tasks.resolve_letterboxd_ids), "ID resolver started."),
    "reconcile": (tasks.reconcile, "Reconcile started."),
    "posters": (tasks.refresh_posters, "Poster refresh started."),
    "backup": (tasks.backup_database, "Database backup started."),
    "all": (tasks.run_all, "Full run started."),
}
_TOKEN_RE = re.compile(r"(?i)\b(api_?key|token|apikey|password)=([^&\s]+)")


def _known_secrets():
    from synchub.notifications import store
    found = set()
    for key in settings.SECRET_FIELDS:
        value = settings.get(key) or ""
        for piece in [value] + re.split(r"[,\s]+", value):
            if len(piece) >= 6:
                found.add(piece)
    with db.connection() as conn:
        found.update(store.secret_values(conn))             # people's notification keys
    return sorted(found, key=len, reverse=True)


def _ticks_to_time(ticks):
    """Jellyfin counts time in 100-nanosecond ticks; shown as h:mm:ss."""
    if ticks is None:
        return ""
    seconds = int(ticks // 10_000_000)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _redact(line, secrets_):
    for secret in secrets_:
        line = line.replace(secret, "***")
    return _TOKEN_RE.sub(r"\1=***", line)


def register(app):
    @app.route("/tools/trigger/<name>", methods=["POST"])
    def trigger(name):
        if name not in TRIGGERS:
            flash("Unknown task.", "danger")
            return redirect(url_for("tools"))
        job, message = TRIGGERS[name]
        tasks.start_async(job)
        flash(message, "info")
        return redirect(url_for("tools"))

    @app.route("/tools/update-settings", methods=["POST"])
    def update_settings():
        errors = settings.save_form(request.form)
        if (request.form.get("pushover_app_token") or "").strip():
            from synchub.notifications import dispatch
            dispatch.clear_pause("pushover")                # a new token ends the hold put on by the old one
        from synchub import scheduler
        scheduler.configure()
        flash("Some values were not saved: " + " ".join(errors), "danger") if errors else flash("Settings updated successfully.", "success")
        tab = request.form.get("tab")
        return redirect(url_for("settings_page", tab=tab) if tab in ("connections", "automation", "logging") else url_for("settings_page"))

    @app.route("/tools/export-csv")
    def export_csv():
        with db.connection() as conn:
            rows = catalog.list_catalog(conn, max_rating=current_limit())
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["title", "year", "type", "state", "in_jellyfin", "in_radarr", "in_sonarr", "wanted_by", "watched_by", "tmdb_id", "tvdb_id"])
        for r in rows:
            cells = [r["title"], r["year"], r["kind"], r["state"], int(r["in_jellyfin"]), int(r["in_radarr"]), int(r["in_sonarr"]),
                     ", ".join(r["wanted_by"]), ", ".join(r["watched_by"]), r["tmdb_id"], r["tvdb_id"]]
            # Titles come from other sites: stop spreadsheet programs from running them as formulas.
            writer.writerow([("'" + c) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in cells])
        return send_file(io.BytesIO(out.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="synchub_export.csv")

    # ---------------------------------------------------------------- profiles
    @app.route("/settings/profiles/save", methods=["POST"])
    def save_profile():
        form = request.form
        profile_id = form.get("profile_id", type=int)
        from_import = "letterboxd_username" not in form and "serializd_username" not in form
        try:
            with db.connection() as conn:
                profiles.save(conn, profile_id, form.get("profile_name"), form.get("letterboxd_username"), form.get("serializd_username"),
                              form.get("jellyfin_user_id"), only_given=from_import)
            flash(f"Profile '{(form.get('profile_name') or '').strip()}' saved.", "success")
        except ProfileError as exc:
            flash(str(exc), "danger")
        back = form.get("return_to") or ""
        return redirect(back if back.startswith("/people/") and not back.startswith("//") else url_for("people_page"))

    @app.route("/settings/profiles/delete/<int:profile_id>", methods=["POST"])
    def delete_profile(profile_id):
        try:
            with db.connection() as conn:
                profiles.delete(conn, profile_id)
            flash("Profile removed. Its Letterboxd/Serializd entries are kept.", "info")
        except ProfileError as exc:
            flash(str(exc), "danger")
        return redirect(url_for("people_page"))

    @app.route("/api/profiles/toggle", methods=["POST"])
    def api_toggle_profile():
        data = request.get_json(silent=True) or {}
        try:
            profile_id, active = int(data["id"]), bool(int(data["is_active"]))
        except (KeyError, TypeError, ValueError):
            return jsonify(ok=False, message="Bad request."), 400
        with db.connection() as conn:
            done = profiles.set_active(conn, profile_id, active)
        return jsonify(ok=done) if done else (jsonify(ok=False, message="Unknown profile."), 404)

    # ---------------------------------------------------------------- DDNS
    @app.route("/ddns/update-settings", methods=["POST"])
    def ddns_update_settings():
        problem = ddns.save_settings(request.form.get("dynu_hostname"), request.form.get("dynu_api_key"), request.form.get("dynu_enabled") == "1", request.form.get("ddns_interval_minutes"))
        if problem:
            flash(problem, "danger")
        else:
            from synchub import scheduler
            scheduler.configure()                      # a new interval takes effect now
            flash("DDNS configuration saved successfully.", "success")
        return redirect(url_for("ddns_page"))

    @app.route("/api/ddns/check", methods=["POST"])
    def api_ddns_check():
        """Times the path to the services and compares what Dynu's name servers and public resolvers say about the name. Changes nothing."""
        creds = ddns.credentials()
        started = time.monotonic()
        ip = ddns.current_ip()
        urls = [(source, None) for source in ddns.IP_SOURCES[:1]] + [(f"{ddns.API_BASE}/dns", {"API-Key": creds["api_key"], "Accept": "application/json"} if creds["api_key"] else None)]
        timings = [ddns_check.timed_request(url, headers) for url, headers in urls]
        record = ddns.read_record(creds["dns_id"], creds["api_key"]) if creds["dns_id"] and creds["api_key"] else None
        rows = ddns_check.snapshot(creds["domain"], ip) if creds["domain"] else []
        for t in timings:
            t["name"] = "Public address service" if t["url"] in ddns.IP_SOURCES else "Dynu API"
        return jsonify(ok=True, ip=ip, domain=creds["domain"], timings=timings, rows=rows, dynu=record and {k: record.get(k) for k in ("ipv4Address", "ttl", "state", "updatedOn")},
                       findings=[{"level": level, "text": text} for level, text in ddns_check.findings(timings, rows, record, ip)], took=round(time.monotonic() - started, 1))

    @app.route("/api/ddns/timeline")
    def api_ddns_timeline():
        return jsonify(ok=True, timeline=ddns.last_timeline())

    @app.route("/ddns/trigger-update", methods=["POST"])
    def ddns_trigger_update():
        ok, message = ddns.run_update(force=True)
        flash(message, "success" if ok else "danger")
        return redirect(url_for("ddns_page"))

    # ---------------------------------------------------------------- logs and payloads
    @app.route("/api/logs")
    def api_logs():
        try:
            tail = max(1, min(int(request.args.get("tail", 300)), 5000))
        except ValueError:
            tail = 300
        lines = []
        if LOG_FILE_PATH.exists():
            try:
                secrets_ = _known_secrets()
                with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as fh:
                    lines = [_redact(line.rstrip("\n"), secrets_) for line in deque(fh, maxlen=tail)]
            except OSError:
                lines = ["Unable to read the log file."]
        return jsonify(logs=lines)

    @app.route("/api/payload-logs")
    def api_payload_logs():
        try:
            limit = max(1, min(int(request.args.get("limit", 100)), 5000))
        except ValueError:
            limit = 100
        sql, params = "SELECT * FROM api_payload_log WHERE 1=1", []
        if request.args.get("service"):
            sql += " AND service = ?"
            params.append(request.args["service"])
        if request.args.get("errors_only") == "1":
            sql += " AND (status_code >= 400 OR status_code = 0)"
        with db.connection() as conn:
            return jsonify([dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))])

    # ---------------------------------------------------------------- Jellyfin events that were kept
    def _event_filters():
        return {"type": (request.args.get("type") or "").strip(), "user": (request.args.get("user") or "").strip(), "q": (request.args.get("q") or "").strip()}

    @app.route("/jellyfin-events")
    def jellyfin_events_page():
        f = _event_filters()
        with db.connection() as conn:
            summary = jellyfin_events.summary(conn)
            rows = jellyfin_events.recent(conn, f["type"] or None, f["user"] or None, f["q"] or None, limit=200)
            keep = jellyfin_events.keep_days(conn)
        return render_template("jellyfin_events.html", summary=summary, rows=rows, f=f, keep_days=keep, ticks_to_time=_ticks_to_time,
                               status=webhooks.last_received("jellyfin"))

    @app.route("/jellyfin-events/<int:event_id>/payload")
    def jellyfin_event_payload(event_id):
        with db.connection() as conn:
            raw = jellyfin_events.payload_of(conn, event_id)
        if raw is None:
            return jsonify(ok=False, message="That event is gone."), 404
        parsed = jellyfin_events.parse_body(raw)
        return jsonify(ok=True, payload=parsed or None, raw=None if parsed else raw)

    @app.route("/jellyfin-events/export.jsonl")
    def jellyfin_events_export():
        f = _event_filters()

        def generate():
            with db.connection() as conn:
                yield from jellyfin_events.export_lines(conn, f["type"] or None, f["user"] or None, f["q"] or None)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Response(generate(), mimetype="application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="jellyfin_events_{stamp}.jsonl"'})

    @app.route("/jellyfin-events/clear", methods=["POST"])
    def jellyfin_events_clear():
        with db.connection() as conn:
            removed = jellyfin_events.clear(conn)
        flash(f"Deleted {removed} kept Jellyfin event{'s' if removed != 1 else ''}.", "info")
        return redirect(url_for("jellyfin_events_page"))

    @app.route("/api/payload-logs/clear", methods=["POST"])
    def api_clear_payload_logs():
        with db.connection() as conn:
            conn.execute("DELETE FROM api_payload_log")
        return jsonify(ok=True)
