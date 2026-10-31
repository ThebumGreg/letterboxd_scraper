"""The Versions tab of the Media page: recommended extra versions of films and Dolby Vision 7 to 8.1 conversions, approved in bulk or one by one.

Nothing here is automatic: a recommendation is a row on this tab until a person approves it (with the choices they changed) or declines it."""
from flask import jsonify

from flask import request

from synchub import db, jobs, settings, versions
from settings_validation import validate_setting

SETTING_KEYS = ("versions_on", "variant_min_gb", "variant_height", "variant_label", "variant_crf", "variant_maxrate_kbps", "variant_audio_kbps", "variant_stereo", "dv7_remux")
HEIGHTS = (480, 720, 1080, 1440)


def overview(conn):
    data = versions.listing(conn)
    return {**data, "heights": HEIGHTS}


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def register(app):
    @app.route("/versions")
    def library_versions_page():
        from synchub.web import media
        return media.render_media("versions")

    @app.route("/api/library/versions/approve", methods=["POST"])
    def api_library_versions_approve():
        from synchub.web.library import _who
        data = request.get_json(silent=True) or {}
        kind = str(data.get("kind") or "")
        if kind not in versions.KINDS:
            return _fail("Unknown kind of action.")
        with db.connection() as conn:
            by = _who(conn)
            if data.get("all"):
                done, skipped = versions.approve_recommended(conn, kind, by)
            else:
                items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
                if not items:
                    return _fail("Choose at least one film.")
                done, skipped = versions.approve_many(conn, kind, items, by)
        message = f"Queued {done} film{'' if done == 1 else 's'}." + (f" {len(skipped)} could not be queued: {skipped[0][1]}" if skipped else "")
        return jsonify(ok=bool(done) or not skipped, message=message, queued=done, skipped=[{"id": i, "why": w} for i, w in skipped])

    @app.route("/api/library/versions/skip", methods=["POST"])
    def api_library_versions_skip():
        from synchub.web.library import _who
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") if isinstance(data.get("ids"), list) else [data.get("id")]
        try:
            with db.connection() as conn:
                by = _who(conn)
                for record_id in ids:
                    versions.skip(conn, str(data.get("kind") or ""), int(record_id), by)
        except (versions.VersionError, TypeError, ValueError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Declined. It stays off the list until you bring it back.")

    @app.route("/api/library/versions/restore", methods=["POST"])
    def api_library_versions_restore():
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                versions.restore(conn, str(data.get("kind") or ""), int(data.get("id")))
        except (versions.VersionError, TypeError, ValueError) as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Back on the list.")

    @app.route("/api/library/versions/cancel/<int:action_id>", methods=["POST"])
    def api_library_versions_cancel(action_id):
        from synchub.web.library import _who
        with db.connection() as conn:
            row = versions.get(conn, action_id)
            if row is None or not row["job_id"] or row["status"] not in versions.OPEN + ("blocked",):
                return _fail("Nothing to cancel there.", 404)
            try:
                result = jobs.cancel(conn, row["job_id"], _who(conn))
            except jobs.JobError as exc:
                return _fail(str(exc))
            versions.sync(conn, row["job_id"])
        return jsonify(ok=True, message="Cancelling: the worker stops at its next report." if result == "cancelling" else "Cancelled.")

    @app.route("/api/library/versions/settings", methods=["POST"])
    def api_library_versions_settings():
        data = request.get_json(silent=True) or {}
        cleaned, errors = {}, []
        for key in SETTING_KEYS:
            if key in data:
                ok, result = validate_setting(key, str(data[key]))
                (cleaned.__setitem__(key, result) if ok else errors.append(result))
        if errors:
            return _fail(" ".join(errors))
        with db.connection() as conn:
            for key, value in cleaned.items():
                settings.set_value(key, value, conn=conn)
        return jsonify(ok=True, message="Saved.")
