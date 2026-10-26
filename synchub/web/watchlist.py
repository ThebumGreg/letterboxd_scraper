"""A person's own watchlist: the page, adding and removing titles, and the switch that takes watched titles off it."""
import logging

from flask import jsonify, render_template, request

from synchub import access, db, ratings, watchlist
from synchub.clients import ApiError, ConfigError
from synchub.clients.tmdb import TmdbClient
from synchub.web import current_limit, current_profile, title_allowed

logger = logging.getLogger(__name__)


def _fail(message, status=400):
    response = jsonify(ok=False, message=message)
    response.status_code = status
    return response


def _person(conn):
    """(the profile using this request, None), or (None, why they cannot change a watchlist). The same rule as notification settings: once any
    profile has a PIN, a profile without one (which anyone on the network can choose) may not change a person's list."""
    profile = current_profile(conn)
    if profile is None:
        return None, "Choose your profile first (the link at the top right). A watchlist belongs to a profile."
    has_pin = conn.execute("SELECT pin_hash IS NOT NULL FROM profile WHERE id = ?", (profile["id"],)).fetchone()[0]
    if access.locked_down(conn) and not has_pin:
        return None, "This profile has no PIN, and anyone on the network can choose a profile without one. Ask whoever manages People to give it a PIN first."
    return profile, None


def register(app):
    @app.route("/watchlist")
    def watchlist_page():
        with db.connection() as conn:
            profile = current_profile(conn)
            mine, removed, drop = [], [], False
            if profile:
                mine = watchlist.items(conn, profile["id"], limit=current_limit())
                removed = watchlist.items(conn, profile["id"], hidden=True, limit=current_limit())
                drop = watchlist.drop_watched_enabled(conn, profile["id"])
        return render_template("watchlist.html", person=profile, items=mine, removed=removed, drop_watched=drop)

    @app.route("/api/watchlist/add", methods=["POST"])
    def api_watchlist_add():
        """Adds a title by its TMDB ID (from Search or Discover), or one SyncHub already has by its own ID. Requests nothing."""
        data = request.get_json(silent=True) or {}
        with db.connection() as conn:
            profile, problem = _person(conn)
        if problem:
            return _fail(problem, 403)
        try:
            if data.get("media_id") is not None:
                media_id = data["media_id"]
                if not isinstance(media_id, int) or isinstance(media_id, bool):
                    return _fail("That is not a title.")
                with db.connection() as conn:
                    if not conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() or not title_allowed(conn, media_id):
                        return _fail("That title is not available for your profile.", 404)
                    result, title = watchlist.add(conn, profile["id"], media_id), conn.execute("SELECT title FROM media WHERE id = ?", (media_id,)).fetchone()["title"]
                got = {"media_id": media_id, "title": title, "result": result}
            else:
                kind, tmdb_id = data.get("kind"), str(data.get("tmdb_id") or "").strip()
                if kind not in ("movie", "show") or not tmdb_id.isdigit():
                    return _fail("Say which movie or show to add.")
                tmdb = TmdbClient()
                limit = current_limit()
                if limit is not None:
                    known = ratings.ensure(tmdb, kind, tmdb_id)
                    if known is None or not ratings.permitted(known["rank"], limit):
                        return _fail("That title is not available for your profile.", 403)
                got = watchlist.add_from_tmdb(tmdb, kind, tmdb_id, profile["id"])
        except ConfigError as exc:
            return _fail(str(exc))
        except ApiError as exc:
            return _fail(str(exc), 502)
        except watchlist.WatchlistError as exc:
            return _fail(str(exc))
        said = {"added": "Added to your watchlist.", "restored": "Put back on your watchlist.", "already": "Already on your watchlist."}[got["result"]]
        return jsonify(ok=True, message=f"'{got['title']}': {said}", media_id=got["media_id"], result=got["result"])

    def change(media_id, action):
        with db.connection() as conn:
            profile, problem = _person(conn)
            if problem:
                return _fail(problem, 403)
            if action == "remove":
                if not watchlist.remove(conn, profile["id"], media_id):
                    return _fail("That title is not on your watchlist.", 404)
                return jsonify(ok=True, message="Removed from your watchlist.")
            if not conn.execute("SELECT 1 FROM watchlist_item WHERE profile_id = ? AND media_id = ?", (profile["id"], media_id)).fetchone():
                return _fail("That title was never on your watchlist.", 404)
            watchlist.add(conn, profile["id"], media_id)
            return jsonify(ok=True, message="Put back on your watchlist.")

    @app.route("/api/watchlist/<int:media_id>/remove", methods=["POST"])
    def api_watchlist_remove(media_id):
        return change(media_id, "remove")

    @app.route("/api/watchlist/<int:media_id>/restore", methods=["POST"])
    def api_watchlist_restore(media_id):
        return change(media_id, "restore")

    @app.route("/api/watchlist/settings", methods=["POST"])
    def api_watchlist_settings():
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("drop_watched"), bool):
            return _fail("Say whether watched titles should come off the list.")
        with db.connection() as conn:
            profile, problem = _person(conn)
            if problem:
                return _fail(problem, 403)
            watchlist.set_drop_watched(conn, profile["id"], data["drop_watched"])
            hidden = watchlist.drop_watched(conn) if data["drop_watched"] else 0
        note = f" {hidden} watched title(s) came off your list." if hidden else ""
        return jsonify(ok=True, message=("Watched titles will come off your watchlist." if data["drop_watched"] else "Watched titles will stay on your watchlist.") + note)
