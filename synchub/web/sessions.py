"""The Sessions page: who is watching what and from which device, and what the DVR is recording or about to record."""
import logging

from flask import jsonify, render_template

from synchub import db, dvr, sessions, settings

logger = logging.getLogger(__name__)


def _live_programs(conn, client, rows):
    """For people watching a live channel: what is on air and whether it is set to record. A guide that cannot be read leaves the row as it was."""
    seen = {}
    for r in rows:
        if r["kind"] != "TvChannel" or not r["item_id"]:
            continue
        if r["item_id"] not in seen:
            try:
                seen[r["item_id"]] = (client.channel(r["item_id"]).get("CurrentProgram") or {})
            except Exception as e:
                logger.info(f"Sessions: no guide for channel {r['item_id']}: {e}")
                seen[r["item_id"]] = {}
        program = seen[r["item_id"]]
        if not program.get("Id"):
            continue
        timer = conn.execute("SELECT status FROM dvr_timer WHERE kind = 'timer' AND removed_at IS NULL AND program_id = ?", (program["Id"],)).fetchone()
        r["program"] = program.get("Name")
        r["program_ends"] = (program.get("EndDate") or "")[:19].replace("T", " ") or None
        r["recording"] = ("recording now" if timer["status"] == "InProgress" else "set to record") if timer else (
            "set to record" if program.get("TimerId") else None)


def snapshot(conn):
    """Everything the page shows. A Jellyfin that cannot be reached is reported, not raised, so the DVR half still shows."""
    out = {"rows": [], "error": None, "dvr_on": settings.get("dvr_enabled", conn=conn) == "1", "dvr": None}
    try:
        from synchub.clients.jellyfin import JellyfinClient
        client = JellyfinClient()
        out["rows"] = sessions.live(client)
        _live_programs(conn, client, out["rows"])
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
