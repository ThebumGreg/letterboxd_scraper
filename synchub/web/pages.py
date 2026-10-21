"""HTML pages."""
from flask import abort, flash, redirect, render_template, send_file, send_from_directory, url_for

from config import BASE_DIR
from synchub import catalog, db, ddns, posters, profiles, settings, states, tasks

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
        return render_template("settings.html", settings={**settings.DEFAULTS, **settings.get_all()}, profiles=profile_list)

    @app.route("/payloads")
    def payloads_page():
        return render_template("payloads.html", settings={**settings.DEFAULTS, **settings.get_all()})

    @app.route("/logs")
    def logs_page():
        return render_template("logs.html")

    @app.route("/ddns")
    def ddns_page():
        creds = ddns.credentials()
        return render_template("ddns.html", creds=creds, ddns_status=ddns.status(), ddns_history=ddns.history(10))
