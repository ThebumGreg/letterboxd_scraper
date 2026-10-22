"""Settings, profiles, tasks, logs, payload log, DDNS and export."""
import csv
import io
import re
from collections import deque

from flask import flash, jsonify, redirect, render_template, request, send_file, url_for

from config import LOG_FILE_PATH
from synchub import catalog, db, ddns, notify, profiles, settings, tasks
from synchub.profiles import ProfileError

# name -> (job, message shown after starting it)
TRIGGERS = {
    "jellyfin_movies": (tasks.with_reconcile(tasks.sync_jellyfin_movies), "Jellyfin movie sync started."),
    "jellyfin_tv": (tasks.with_reconcile(tasks.sync_jellyfin_tv), "Jellyfin TV sync started."),
    "radarr": (tasks.with_reconcile(tasks.sync_radarr), "Radarr sync started."),
    "sonarr": (tasks.with_reconcile(tasks.sync_sonarr), "Sonarr sync started."),
    "letterboxd": (tasks.with_reconcile(tasks.scrape_letterboxd), "Letterboxd scrape started."),
    "serializd": (tasks.with_reconcile(tasks.scrape_serializd), "Serializd scrape started."),
    "id_resolver": (tasks.with_reconcile(tasks.resolve_letterboxd_ids), "ID resolver started."),
    "reconcile": (tasks.reconcile, "Reconcile started."),
    "posters": (tasks.refresh_posters, "Poster refresh started."),
    "all": (tasks.run_all, "Full run started."),
}
_TOKEN_RE = re.compile(r"(?i)\b(api_?key|token|apikey|password)=([^&\s]+)")


def _known_secrets():
    found = set()
    for key in settings.SECRET_FIELDS:
        value = settings.get(key) or ""
        for piece in [value] + re.split(r"[,\s]+", value):
            if len(piece) >= 6:
                found.add(piece)
    return sorted(found, key=len, reverse=True)


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
        from synchub import scheduler
        scheduler.configure()
        flash("Some values were not saved: " + " ".join(errors), "danger") if errors else flash("Settings updated successfully.", "success")
        return redirect(url_for("payloads_page") if request.referrer and "payloads" in request.referrer else url_for("settings_page"))

    @app.route("/tools/export-csv")
    def export_csv():
        with db.connection() as conn:
            rows = catalog.list_catalog(conn)
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
        return redirect(url_for("settings_page"))

    @app.route("/settings/profiles/delete/<int:profile_id>", methods=["POST"])
    def delete_profile(profile_id):
        with db.connection() as conn:
            profiles.delete(conn, profile_id)
        flash("Profile removed. Its Letterboxd/Serializd entries are kept.", "info")
        return redirect(url_for("settings_page"))

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
        ddns.save_settings(request.form.get("dynu_hostname"), request.form.get("dynu_api_key"), request.form.get("dynu_enabled") == "1")
        flash("DDNS configuration saved successfully.", "success")
        return redirect(url_for("ddns_page"))

    @app.route("/ddns/trigger-update", methods=["POST"])
    def ddns_trigger_update():
        ok, message = ddns.run_update(force=True)
        flash(message, "success" if ok else "danger")
        return redirect(url_for("ddns_page"))

    @app.route("/tools/test-notification", methods=["POST"])
    def test_notification():
        sent = notify.send("SyncHub Test", "Test notification from SyncHub.", force=True)
        flash("Notification sent!" if sent else "Failed to send. Check the Apprise URLs in Settings.", "success" if sent else "danger")
        return redirect(url_for("tools"))

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

    @app.route("/api/payload-logs/clear", methods=["POST"])
    def api_clear_payload_logs():
        with db.connection() as conn:
            conn.execute("DELETE FROM api_payload_log")
        return jsonify(ok=True)
