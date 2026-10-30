"""The Sessions page: who is watching what and from which device, and what the DVR is recording or about to record."""
import logging

from flask import jsonify, render_template

from synchub import db, dvr, sessions, settings

logger = logging.getLogger(__name__)


def snapshot(conn):
    """Everything the page shows. A Jellyfin that cannot be reached is reported, not raised, so the DVR half still shows."""
    out = {"rows": [], "error": None, "dvr_on": settings.get("dvr_enabled", conn=conn) == "1", "dvr": None}
    try:
        from synchub.clients.jellyfin import JellyfinClient
        out["rows"] = sessions.live(JellyfinClient())
    except Exception as e:
        logger.warning(f"Sessions: could not read Jellyfin: {e}")
        out["error"] = str(e) or "Jellyfin did not answer."
    if out["dvr_on"]:
        out["dvr"] = dvr.upcoming(conn)
    return out


def register(app):
    @app.route("/system/sessions")
    def sessions_page():
        with db.connection() as conn:
            data = snapshot(conn)
        return render_template("sessions.html", **data)

    @app.route("/system/sessions/fragment")
    def sessions_fragment():
        with db.connection() as conn:
            data = snapshot(conn)
        return render_template("_sessions_live.html", **data)

    @app.route("/api/sessions")
    def api_sessions():
        with db.connection() as conn:
            return jsonify(snapshot(conn))
