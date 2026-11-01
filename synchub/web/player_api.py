"""The player API: what the program on a TV player PC calls (register, poll, answer, log).

Every call carries the player's token (Authorization: Bearer ...). Like the worker API it is not behind the profile chooser, has no CSRF token and answers
in JSON. Endpoint names start with `worker_api_` so the access rules and the CSRF exemption that agents already have apply to it as well.
"""
import logging
import time
from functools import wraps

from flask import g, jsonify, request

from synchub import db, players

logger = logging.getLogger(__name__)

PREFIX = "/api/player/v1"
POLL_SECONDS = 2            # how soon a player asks again after an answer
WAIT_SECONDS = 20           # how long a poll is held open waiting for a command (a command then reaches the player within a second)
MAX_WAIT_SECONDS = 30
STEP_SECONDS = 0.5


def _error(message, status, **extra):
    return jsonify(ok=False, message=message, **extra), status


def _bearer():
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else ""


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _wait(value):
    try:
        return max(0.0, min(float(value), MAX_WAIT_SECONDS))
    except (TypeError, ValueError):
        return float(WAIT_SECONDS)


def register(app):
    def route(rule, name):
        """A player endpoint: checks the token first, then runs the view with the player in g.player."""
        def decorate(view):
            @wraps(view)
            def guarded(*args, **kwargs):
                with db.connection() as conn:
                    player, problem = players.authenticate(conn, _bearer())
                if problem == "disabled":
                    return _error("This player is disabled in SyncHub.", 403)
                if problem:
                    return _error("The player token was not accepted.", 401)
                g.player = player
                return view(*args, **kwargs)
            app.add_url_rule(f"{PREFIX}{rule}", endpoint=f"worker_api_player_{name}", view_func=guarded, methods=["POST"])
            return guarded
        return decorate

    @route("/register", "register")
    def register_player():
        with db.connection() as conn:
            try:
                players.register(conn, g.player["id"], _body())
            except players.ProtocolError as exc:
                return _error(str(exc), 426, protocol=players.PROTOCOL)
        logger.info(f"[players] {g.player['name']} registered")
        return jsonify(ok=True, player_id=g.player["id"], name=g.player["name"], protocol=players.PROTOCOL, poll_seconds=POLL_SECONDS, wait_seconds=WAIT_SECONDS)

    @route("/poll", "poll")
    def poll():
        """Holds the request open until a command is queued (or `wait` seconds pass), so a command arrives within about a second.
        The connection is closed while waiting: a held-open poll must never hold the database."""
        data = _body()
        wait_until = time.monotonic() + _wait(data.get("wait", WAIT_SECONDS))
        with db.connection() as conn:
            players.touch(conn, g.player["id"], data.get("status"), data.get("now_playing"))
            wanted_log = bool(conn.execute("SELECT log_requested FROM player WHERE id = ?", (g.player["id"],)).fetchone()[0])
            command = players.take(conn, g.player["id"])
        while command is None and not wanted_log and time.monotonic() < wait_until:
            time.sleep(min(STEP_SECONDS, max(0.0, wait_until - time.monotonic())))
            with db.connection() as conn:
                if players.has_queued(conn, g.player["id"]):
                    command = players.take(conn, g.player["id"])
                wanted_log = bool(conn.execute("SELECT log_requested FROM player WHERE id = ?", (g.player["id"],)).fetchone()[0])
        return jsonify(ok=True, command=command, send_log=wanted_log, poll_seconds=POLL_SECONDS, wait_seconds=WAIT_SECONDS)

    @route("/commands/<int:command_id>/answer", "answer")
    def answer(command_id):
        data = _body()
        with db.connection() as conn:
            try:
                players.finish(conn, g.player["id"], command_id, bool(data.get("ok")), data.get("result"))
            except players.PlayerError as exc:
                return _error(str(exc), 409)
        return jsonify(ok=True)

    @route("/log", "log")
    def player_log():
        data = _body()
        with db.connection() as conn:
            players.store_log(conn, g.player["id"], data.get("log"), bool(data.get("truncated")))
        return jsonify(ok=True)
