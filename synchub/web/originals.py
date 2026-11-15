"""The Originals tab of the Media page: the original files workers keep after replacing a file, with Restore, Keep longer and Delete now."""
from flask import jsonify, request

from synchub import db, originals, settings
from synchub.library import aside, hidden
from settings_validation import validate_setting


def overview(conn):
    data = originals.listing(conn)
    return {**data, "actions": originals.ACTIONS, "conversions": originals.conversions(conn), "hidden": hidden.listing(conn)}


def register(app):
    @app.route("/originals")
    def workers_originals_page():
        from synchub.web import media
        return media.render_media("originals")

    @app.route("/api/worker_originals/<int:worker_id>", methods=["POST"])
    def api_worker_originals(worker_id):
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                job_id = originals.queue(conn, worker_id, str(data.get("id") or ""), str(data.get("action") or ""), data.get("days"))
        except originals.OriginalsError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        return jsonify(ok=True, job=job_id, message="Queued. The worker does it next time it asks for work.")

    @app.route("/api/library/hidden/act", methods=["POST"])
    def api_worker_hidden_act():
        """Put back or delete hidden files: {action: 'put_back' | 'delete', ids: [...]}. Each file is its own job; the answer says how many were queued and why the rest were not."""
        data = request.get_json(silent=True) or {}
        action, ids = data.get("action"), [i for i in (data.get("ids") or []) if isinstance(i, int)][:500]
        if action not in ("put_back", "delete") or not ids:
            return jsonify(ok=False, message="Choose what to do and at least one file."), 400
        queued, problems = 0, []
        with db.connection() as conn:
            for rid in ids:
                try:
                    if action == "delete":
                        hidden.request_delete(conn, rid)
                    else:
                        aside.request(conn, rid, undo=True)
                    queued += 1
                except (hidden.HiddenError, aside.AsideError) as exc:
                    problems.append(str(exc))
        if not queued:
            return jsonify(ok=False, message=problems[0] if problems else "Nothing was queued."), 400
        what = "deleted" if action == "delete" else "put back"
        return jsonify(ok=True, message=f"Queued {queued} file(s) to be {what}. The worker does it next time it asks for work." + (f" {len(problems)} could not be queued: {problems[0]}" if problems else ""))

    @app.route("/api/library/hidden/delete-after", methods=["POST"])
    def api_worker_hidden_delete_after():
        value = str((request.get_json(silent=True) or {}).get("days", ""))
        ok, result = validate_setting("hidden_delete_after_days", value)
        if not ok:
            return jsonify(ok=False, message=str(result)), 400
        with db.connection() as conn:
            settings.set_value("hidden_delete_after_days", result, conn=conn)
        return jsonify(ok=True, message="Saved." if result != "0" else "Automatic deletion is off.")
