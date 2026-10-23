"""The Access page: who has a PIN and who may open what."""
from flask import current_app, g, jsonify, render_template, request

from synchub import access, db, settings

PRESETS = {"everything": list(access.KEYS), "viewer": ["dashboard", "search"], "nothing": []}


def _fail(message, status=400):
    response = jsonify(ok=False, message=message)
    response.status_code = status
    return response


def _keys(data):
    keys = data.get("permissions")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise access.AccessError("Send the permissions as a list.")
    return keys


def register(app):
    from synchub.web import PIN_COOKIE

    @app.route("/access")
    def access_page():
        with db.connection() as conn:
            overview = access.overview(conn)
        return render_template("access.html", overview=overview, permissions=access.PERMISSIONS, presets=PRESETS, me=(g.profile or {}).get("id"))

    def act(profile_id, change, message):
        """Runs `change(conn)`; the answer is the message or the reason it was refused."""
        try:
            with db.connection() as conn:
                change(conn)
        except access.AccessError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message=message)

    @app.route("/api/access/profile/<int:profile_id>/permissions", methods=["POST"])
    def api_access_permissions(profile_id):
        try:
            keys = _keys(request.get_json(silent=True) or {})
        except access.AccessError as exc:
            return _fail(str(exc))
        return act(profile_id, lambda conn: access.set_permissions(conn, profile_id, keys), "Saved.")

    @app.route("/api/access/guest", methods=["POST"])
    def api_access_guest():
        try:
            keys = _keys(request.get_json(silent=True) or {})
        except access.AccessError as exc:
            return _fail(str(exc))
        return act(None, lambda conn: access.set_guest_permissions(conn, keys), "Saved.")

    @app.route("/api/access/profile/<int:profile_id>/pin", methods=["POST"])
    def api_access_set_pin(profile_id):
        pin = (request.get_json(silent=True) or {}).get("pin")
        response = act(profile_id, lambda conn: access.set_pin(conn, profile_id, pin), "PIN saved. Everyone who had entered the old one has to enter this one.")
        if response.status_code == 200 and g.profile and g.profile["id"] == profile_id:
            with db.connection() as conn:                       # you changed your own: keep this device signed in
                version = conn.execute("SELECT pin_version FROM profile WHERE id = ?", (profile_id,)).fetchone()["pin_version"]
            response.set_cookie(PIN_COOKIE, access.issue_proof(current_app.secret_key, profile_id, version), max_age=access.session_days() * 86400, samesite="Lax", httponly=True)
        return response

    @app.route("/api/access/profile/<int:profile_id>/pin/clear", methods=["POST"])
    def api_access_clear_pin(profile_id):
        response = act(profile_id, lambda conn: access.clear_pin(conn, profile_id), "PIN removed. Anyone on the network can now choose this profile.")
        if response.status_code == 200 and g.profile and g.profile["id"] == profile_id:
            response.delete_cookie(PIN_COOKIE)
        return response

    @app.route("/api/access/profile/<int:profile_id>/signout", methods=["POST"])
    def api_access_signout(profile_id):
        return act(profile_id, lambda conn: access.sign_out_everywhere(conn, profile_id), "Signed out everywhere: the PIN is asked for again on every device.")

    @app.route("/api/access/profile/<int:profile_id>/unlock", methods=["POST"])
    def api_access_unlock(profile_id):
        return act(profile_id, lambda conn: access.unlock(conn, profile_id), "Unlocked.")

    @app.route("/api/access/session-days", methods=["POST"])
    def api_access_session_days():
        from settings_validation import validate_setting
        ok, value = validate_setting("access_session_days", str((request.get_json(silent=True) or {}).get("days", "")))
        if not ok:
            return _fail(value)
        settings.set_value("access_session_days", value)
        return jsonify(ok=True, message=f"A PIN is asked for again after {value} day{'s' if value != '1' else ''} on a device.")
