"""The Players tab of the Media page: add a TV player, see whether it is up, and send it Ping, Update and Restart. The player's own calls are in player_api.py."""
from flask import jsonify, render_template, request

from synchub import db, players, webhooks
from synchub import __version__

CLONE = "git clone https://github.com/ThebumGreg/letterboxd_scraper.git && cd letterboxd_scraper"


def describe(p, commands):
    """A player for the page: its state, how long ago it was seen and what it reported about itself."""
    if p["revoked"]:
        state, color = "Token revoked", "danger"
    elif not p["enabled"]:
        state, color = "Disabled", "secondary"
    elif not p["last_seen"]:
        state, color = "Never connected", "secondary"
    elif p["online"]:
        state, color = "Online", "success"
    else:
        state, color = "Offline", "warning"
    caps = p["capabilities"]
    return {**p, "state": state, "state_color": color, "seen": webhooks.ago(p["last_seen"]) or "never", "mpv": caps.get("mpv") or "", "screens": caps.get("screens") or [],
            "graphics": caps.get("graphics") or [], "load": p["status"].get("load"), "uptime": p["status"].get("uptime_text") or "", "playing": p["now_playing"].get("title") or "",
            "commands": commands}


def snapshot(conn):
    items = players.list_all(conn)
    return {"players": [describe(p, players.recent_commands(conn, p["id"], 3)) for p in items], "app_version": __version__}


def _fail(message, status=400):
    return jsonify(ok=False, message=message), status


def _setup(name, token):
    return f"{CLONE}\nsudo ./player/setup-player.sh --url {request.host_url.rstrip('/')} --token {token} --name \"{name}\""


def register(app):
    @app.route("/players")
    def players_page():
        from synchub.web import media
        return media.render_media("players")

    @app.route("/players/fragment")
    def players_fragment():
        with db.connection() as conn:
            data = snapshot(conn)
        return render_template("_players_list.html", **data)

    @app.route("/api/players", methods=["POST"])
    def api_player_create():
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                player_id, token = players.create(conn, data.get("name"), data.get("tv_name") or "")
        except players.PlayerError as exc:
            return _fail(str(exc))
        name = " ".join(str(data.get("name")).split())
        return jsonify(ok=True, id=player_id, token=token, setup=_setup(name, token), message=f"Added {name}. Copy the setup command now: the token is not shown again.")

    @app.route("/api/players/<int:player_id>/edit", methods=["POST"])
    def api_player_edit(player_id):
        data = request.get_json(silent=True) or {}
        try:
            with db.connection() as conn:
                players.update(conn, player_id, name=data.get("name"), tv_name=data.get("tv_name"))
        except players.PlayerError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Saved.")

    @app.route("/api/players/<int:player_id>/<action>", methods=["POST"])
    def api_player_action(player_id, action):
        try:
            with db.connection() as conn:
                player = players.get(conn, player_id)
                if player is None:
                    return _fail("That player no longer exists.", 404)
                if action in ("ping", "update", "restart"):
                    players.send(conn, player_id, action)
                    note = "" if player["online"] else " The player is offline, so this is dropped after ten minutes if it does not come back."
                    text = {"ping": "Asked the player to answer.", "update": "Asked the player to update to the newest code. It restarts itself when it is done.",
                            "restart": "Asked the player to restart its program."}[action]
                    return jsonify(ok=True, message=text + note)
                if action == "log":
                    players.request_log(conn, player_id)
                    return jsonify(ok=True, online=player["online"], message="Asked the player for its log." if player["online"] else "Asked. The player is offline, so the log arrives when it is back.")
                if action in ("enable", "disable"):
                    players.set_enabled(conn, player_id, action == "enable")
                    return jsonify(ok=True, message="Done.")
                if action == "rotate":
                    token = players.rotate_token(conn, player_id)
                    return jsonify(ok=True, token=token, setup=_setup(player["name"], token), message="New token. The old one no longer works; copy the setup command now.")
                if action == "revoke":
                    players.revoke(conn, player_id)
                    return jsonify(ok=True, message="Token revoked. Make a new token to let the player back in.")
                if action == "delete":
                    players.delete(conn, player_id)
                    return jsonify(ok=True, message="Player removed.")
        except players.PlayerError as exc:
            return _fail(str(exc))
        return _fail("Unknown action.", 404)

    @app.route("/api/players/<int:player_id>/log")
    def api_player_log(player_id):
        with db.connection() as conn:
            player = players.get(conn, player_id)
            if player is None:
                return _fail("That player no longer exists.", 404)
            log = players.stored_log(conn, player_id)
        if log:
            log["received"] = webhooks.ago(log["received_at"])
        return jsonify(ok=True, log=log, pending=bool(player["log_requested"]), name=player["name"], online=player["online"], version=player["version"])
