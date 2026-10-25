"""HTML pages."""
import logging
import os

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for

from config import BASE_DIR
from synchub import backup, catalog, db, ddns, posters, profiles, search, settings, states, tasks, webhooks
from synchub.clients import ApiError, ConfigError
from synchub.clients.tmdb import TmdbClient

logger = logging.getLogger(__name__)

STATUS_ORDER = ("missing", "queued", "downloading", "downloaded", "transferring", "encoding", "in_library", "partial")


def register(app):
    @app.route("/")
    def index():
        with db.connection() as conn:
            rows = catalog.list_catalog(conn)
            defs = states.definitions(conn)
            defs["partial"] = {"label": "Partial"}
        users = sorted({u for r in rows for u in r["wanted_by"] + r["watched_by"]}, key=str.lower)
        status_options = [(code, defs[code]["label"]) for code in STATUS_ORDER if code in defs]
        return render_template("index.html", rows=rows, stats=catalog.dashboard_stats(rows), all_users=users, status_options=status_options)

    @app.route("/movie/<int:media_id>")
    def movie_details(media_id):
        with db.connection() as conn:
            movie = catalog.media_detail(conn, media_id)
            if not movie or movie["kind"] != "movie":
                flash("Movie not found.", "warning")
                return redirect(url_for("index"))
            state = states.movie_state(conn, media_id)
            defs = states.definitions(conn)
        return render_template("movie_details.html", movie=movie, state=state, state_info=defs.get(state["state"], defs["missing"]))

    @app.route("/show/<int:media_id>")
    def show_details(media_id):
        with db.connection() as conn:
            show = catalog.media_detail(conn, media_id)
            if not show or show["kind"] != "show":
                flash("Show not found.", "warning")
                return redirect(url_for("index"))
            summary = states.show_summary(conn, media_id)
            defs = states.definitions(conn)
            defs["partial"] = {"code": "partial", "label": "Partial", "color": "warning", "icon": "bi-pie-chart"}
        return render_template("show_details.html", show=show, summary=summary, defs=defs)

    @app.route("/healthz")
    def healthz():
        """For the container's health check: the app is up and the database answers. Reveals nothing else, so it needs no sign-in."""
        try:
            with db.connection() as conn:
                version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except Exception as exc:
            logger.warning(f"Health check failed: {exc}")
            return jsonify(ok=False), 503
        return jsonify(ok=True, schema=version)

    @app.route("/poster/<int:media_id>")
    def poster(media_id):
        with db.connection() as conn:
            path = posters.ensure_poster(conn, media_id)
        if path:
            return send_from_directory(path.parent, path.name, max_age=86400)
        placeholder = BASE_DIR / "static" / "placeholder.jpg"
        if placeholder.exists():
            return send_file(placeholder)
        abort(404)

    @app.route("/search")
    def search_page():
        """Search TMDB for a movie or show and add it to Radarr/Sonarr. Results are not saved until something is added."""
        query = (request.args.get("q") or "").strip()
        kind = request.args.get("type") if request.args.get("type") in search.KINDS else "all"
        results, error = [], None
        if query:
            try:
                results = search.find(TmdbClient(), query, kind)
                with db.connection() as conn:
                    search.annotate(conn, results)
            except ConfigError:
                error = "TMDB is not set up yet. Add your TMDB API key in Settings to search."
            except ApiError as exc:
                error = f"TMDB would not search right now: {exc}"
            except Exception as exc:
                logger.warning(f"TMDB search for {query!r} failed: {exc}")
                error = "Could not reach TMDB."
        return render_template("search.html", query=query, kind=kind, results=results, error=error)

    @app.route("/who", methods=["GET", "POST"])
    def who():
        """'Who is watching?': pick a profile, or carry on as a guest. A profile with a PIN asks for it; either choice is remembered on this device."""
        from flask import current_app
        from synchub import access
        from synchub.web import PIN_COOKIE, PROFILE_COOKIE, PROFILE_COOKIE_DAYS, safe_next
        with db.connection() as conn:
            profile_list = profiles.list_all(conn)
        by_id = {str(p["id"]): p for p in profile_list}
        destination = safe_next(request.values.get("next"))

        def chooser(ask_pin=None, error=None, status=200):
            return render_template("who.html", profiles=profile_list, destination=destination, ask_pin=ask_pin, error=error), status

        if request.method == "GET":
            if not profile_list:
                return redirect(destination)
            wanted = by_id.get(request.args.get("profile", ""))
            return chooser(ask_pin=wanted if wanted and wanted["has_pin"] else None)
        choice = (request.form.get("profile_id") or "").strip()
        if choice != "0" and choice not in by_id:
            flash("That profile no longer exists. Pick again.", "warning")
            return redirect(url_for("who", next=destination))
        response = redirect(destination)
        proof = None
        if choice != "0" and by_id[choice]["has_pin"]:
            if "pin" not in request.form:
                return chooser(ask_pin=by_id[choice])                       # picked a profile with a PIN: ask for it
            with db.connection() as conn:
                outcome, detail = access.check_pin(conn, int(choice), request.form.get("pin"))
            if outcome == "locked":
                minutes = f"{detail // 60 + 1} minutes" if detail >= 120 else f"{detail} seconds"
                return chooser(ask_pin=by_id[choice], error=f"Too many wrong tries. Try again in {minutes}.", status=429)
            if outcome == "wrong":
                return chooser(ask_pin=by_id[choice], error=f"That PIN is not right. {detail} more {'try' if detail == 1 else 'tries'} before it locks for a while.", status=401)
            if outcome != "ok":
                return redirect(url_for("who", next=destination))          # the PIN was removed while the page was open: choose again
            with db.connection() as conn:
                days = access.session_days(conn)
            proof = access.issue_proof(current_app.secret_key, detail["id"], detail["pin_version"])
        response.set_cookie(PROFILE_COOKIE, choice, max_age=PROFILE_COOKIE_DAYS * 86400, samesite="Lax", httponly=True)
        if proof:
            response.set_cookie(PIN_COOKIE, proof, max_age=days * 86400, samesite="Lax", httponly=True)
        else:
            response.delete_cookie(PIN_COOKIE)
        return response

    @app.route("/resolution")
    def resolution_center():
        with db.connection() as conn:
            reviews = catalog.pending_reviews(conn)
            reviewed = {(r["source"], r["key"]) for r in reviews}
            unmatched = [u for u in catalog.unmatched_sources(conn) if (u["source"], u["key"]) not in reviewed]
        return render_template("resolution.html", reviews=reviews, unmatched=unmatched)

    @app.route("/tools")
    def tools():
        return render_template("tools.html", task_statuses=tasks.all_statuses())

    @app.route("/settings")
    def settings_page():
        with db.connection() as conn:
            profile_list = profiles.list_all(conn)
        values = {**settings.DEFAULTS, **settings.get_all()}
        in_docker = os.getenv("SYNCHUB_IN_DOCKER") == "1"
        return render_template("settings.html", settings=values, profiles=profile_list, webhook_status=webhooks.status_for_all(), suggested_synchub_url=request.host_url.rstrip("/"),
                               local_addresses=backup.addresses_on_this_machine(values) if in_docker else [], last_backup=backup.latest())

    @app.route("/payloads")
    def payloads_page():
        return render_template("payloads.html", settings={**settings.DEFAULTS, **settings.get_all()})

    @app.route("/logs")
    def logs_page():
        return render_template("logs.html")

    @app.route("/activity")
    def activity_page():
        return render_template("activity.html")

    @app.route("/diagnostics")
    def diagnostics_page():
        return redirect(url_for("activity_page"), code=301)         # the page's old address

    @app.route("/ddns")
    def ddns_page():
        creds = ddns.credentials()
        return render_template("ddns.html", creds=creds, ddns_status=ddns.status(), ddns_history=ddns.history(10), ddns_interval=ddns.interval_minutes(), timeline=ddns.last_timeline())
