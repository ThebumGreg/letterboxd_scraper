"""The Storage page: the shares workers mount, defined once, with how each worker sees them."""
from flask import jsonify, render_template, request

from synchub import db, storage, workers
from synchub.web import fleet


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def describe(conn):
    names = [r["name"] for r in storage.list_all(conn)]
    described = [fleet.describe_worker(w, names) for w in workers.list_all(conn) if w["enabled"] and not w["revoked"]]
    out = []
    for root in storage.list_all(conn):
        root["protocol_label"] = storage.PROTOCOLS[root["protocol"]]
        root["address"] = f"{root['host']}:{root['share']}" if root["protocol"] == "nfs" else (f"//{root['host']}/{root['share']}" if root["protocol"] == "smb" else root["share"])
        root["seen_by"] = [{"worker": w["name"], "online": w["online"], **next(r for r in w["roots"] if r["name"] == root["name"])} for w in described]
        out.append(root)
    return out


def register(app):
    @app.route("/storage")
    def storage_page():
        from synchub.web import media
        return media.render_media("storage")

    @app.route("/api/storage", methods=["POST"])
    def api_storage_create():
        try:
            with db.connection() as conn:
                storage.create(conn, request.get_json(silent=True) or {})
        except storage.StorageError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Share added.")

    @app.route("/api/storage/<int:root_id>/update", methods=["POST"])
    def api_storage_update(root_id):
        try:
            with db.connection() as conn:
                storage.update(conn, root_id, request.get_json(silent=True) or {})
        except storage.StorageError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Saved. Generate the worker's setup files again if the address or mount point changed.")

    @app.route("/api/storage/<int:root_id>/delete", methods=["POST"])
    def api_storage_delete(root_id):
        try:
            with db.connection() as conn:
                storage.delete(conn, root_id)
        except storage.StorageError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Share removed.")
