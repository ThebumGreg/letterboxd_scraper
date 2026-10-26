"""The Originals tab of the Media page: the original files workers keep after replacing a file, with Restore, Keep longer and Delete now."""
from flask import jsonify, request

from synchub import db, originals


def overview(conn):
    data = originals.listing(conn)
    return {**data, "actions": originals.ACTIONS}


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
