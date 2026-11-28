"""HTML pages."""
import logging
import os

from flask import abort, flash, g, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for

from config import BASE_DIR
from synchub import access, automation, backup, catalog, data_usage, db, ddns, discover, posters, poor_quality, previews, profiles, quality, ratings, search, settings, states, status, tasks, watch, watchlist, webhooks, workers
from synchub.clients import ApiError, ConfigError
from synchub.clients.tmdb import TmdbClient
from synchub.library import episode_link, file_labels
from synchub.notifications import dispatch
from synchub.notifications import store as notification_store
from synchub.web import current_limit, title_allowed

logger = logging.getLogger(__name__)

STATUS_ORDER = ("missing", "queued", "downloading", "downloaded", "transferring", "encoding", "in_library", "partial")


def _progress(conn, kind, media_id):
    """What the person using this page has watched of a title, for the page: None for a guest (no one to say it about), {'linked': False}
    for a profile whose Jellyfin user is not set (People), else {'linked': True, 'status': watch.movie_status / show_status}."""
    profile = getattr(g, "profile", None)
    if not profile:
        return None
    user = watch.user_of(profile)
    if not user:
        return {"linked": False, "name": profile["name"]}
    status = watch.movie_status(conn, user, media_id) if kind == "movie" else watch.show_status(conn, user, media_id)
    return {"linked": True, "name": profile["name"], "status": status}


def _first_run_steps(conn, health):
    """The setup steps a new install goes through, each with whether it is done and where to do it: [(label, done, endpoint)]."""
    states = {i["label"]: i["state"] for i in health["items"]}
    count = lambda table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return [("Add your TMDB and Jellyfin connections", states.get("TMDB", "off") != "off" and states.get("Jellyfin", "off") != "off", "settings_page"),
            ("Add the people who use SyncHub", count("profile") > 0, "people_page"),
            ("Run everything once to fill the library", count("media") > 0, "tools"),
            ("Add your shares (optional, for the Media pages)", count("storage_root") > 0, "storage_page"),
            ("Add a worker (optional, does the heavy file work)", count("worker") > 0, "workers_page")]


def _watchlist_row(items, by_id, limit=12):
    """The signed-in person's watchlist for the dashboard: {'ready': titles now in the library, 'missing': the rest, 'total'}; None when there is no list to show.
    Each entry is the dashboard row of the title (poster, link, state), so the page can show it as it shows the rest."""
    if items is None:
        return None
    ready, missing = [], []
    for item in items:
        row = by_id.get(item["media_id"])
        if row:
            (ready if row["state"] == "in_library" and row["jellyfin_link"] else missing).append(row)
    return {"ready": ready[:limit], "ready_total": len(ready), "missing": missing[:limit], "missing_total": len(missing), "total": len(ready) + len(missing)}


def register(app):
    app.jinja_env.filters["filesize"] = data_usage.human

    @app.route("/")
    def index():
        permissions = getattr(g, "permissions", None)
        with db.connection() as conn:
            rows = catalog.list_catalog(conn, max_rating=current_limit())
            defs = states.definitions(conn)
            defs["partial"] = {"label": "Partial"}
            user = watch.user_of(getattr(g, "profile", None))
            keep_going = watch.continue_watching(conn, user) if user else []
            seen = watch.library_states(conn, user) if user else {}
            played = watch.last_played(conn, user)
            librarian = access.allowed(permissions, "library")
            file_counts = file_labels.counts(conn) if librarian else None
            film_tokens = file_labels.film_tokens(conn) if librarian else {}
            first_run = _first_run_steps(conn, status.snapshot(conn)) if not rows and access.allowed(permissions, "logs") else []
            my_list = watchlist.items(conn, g.profile["id"], limit=current_limit()) if getattr(g, "profile", None) and access.allowed(permissions, "watchlist") else None
        by_id = {r["id"]: r for r in rows}
        resume = []
        for item in keep_going:
            row = by_id.get(item["media_id"])
            if row and row["jellyfin_link"]:
                resume.append({**item, "row": row})
                seen[row["id"]] = "in_progress"
        ranks = {code: d.get("rank", 0) for code, d in defs.items()}
        for r in rows:
            r["watch"] = seen.get(r["id"], "not_started") if user else ""
            r["file_tokens"] = film_tokens.get(r["id"], "")
            r["last_watched"] = played.get(r["id"], "")
            r["state_rank"] = ranks.get(r["state"], 0)
        users = sorted({u for r in rows for u in r["wanted_by"] + r["watched_by"]}, key=str.lower)
        status_options = [(code, defs[code]["label"]) for code in STATUS_ORDER if code in defs]
        ratings_seen = sorted({r["certification"] for r in rows if r["certification"]}, key=lambda c: (ratings.rank(c) is None, ratings.rank(c) or 0, c))
        return render_template("index.html", rows=rows, stats=catalog.dashboard_stats(rows), all_users=users, status_options=status_options, my_list=_watchlist_row(my_list, by_id),
                               resume=resume, can_filter_watch=bool(user), certifications=ratings_seen, first_run=first_run, file_counts=file_counts)

    @app.route("/discover")
    def discover_page():
        """New and trending: TMDB's lists, what was added to the library lately, and what Radarr and Sonarr expect soon."""
        sections, error = [], None
        try:
            tmdb = TmdbClient()
        except ConfigError:
            tmdb, error = None, "TMDB is not set up yet. Add your TMDB API key in Settings to see trending and upcoming titles."
        limit = current_limit()
        if tmdb:
            sections = discover.tmdb_sections(tmdb, limit)
        with db.connection() as conn:
            recent = discover.recently_added(conn, discover.PER_SECTION, limit)
            coming, notes = discover.coming_up(conn, limit)
        return render_template("discover.html", sections=sections, recent=recent, coming=coming, notes=notes, error=error)

    @app.route("/system/storage")
    def system_storage_page():
        """What SyncHub itself takes: database, preview clips, backups, logs, work folders, free space, and how that has grown over the last 90 days."""
        with db.connection() as conn:
            data_usage.ensure_today(conn)
            usage = data_usage.measure(conn)
            history = data_usage.history(conn)
        series = [(key, label, data_usage.chart_points(history, key)) for key, label in
                  (("data_bytes", "Everything in /data"), ("db_bytes", "Database"), ("previews_bytes", "Preview clips"), ("backups_bytes", "Backups"), ("logs_bytes", "Logs"))]
        return render_template("system_storage.html", usage=usage, history=history, series=series, growth=data_usage.growth(history), free_growth=data_usage.growth(history, "free_bytes"))

    @app.route("/system/requests")
    def system_requests_page():
        """Every title SyncHub asked Radarr or Sonarr for on its own, and when."""
        with db.connection() as conn:
            rows = automation.request_log(conn)
            status = automation.request_status(conn)
        return render_template("system_requests.html", rows=rows, status=status)

    @app.route("/system/previews")
    def system_previews_page():
        """All preview clips: which film, when, what was decided, and how much room each takes."""
        with db.connection() as conn:
            rows = previews.recent(conn, 1000)
        for r in rows:
            r["bytes"] = previews.folder_bytes(r["id"])
        return render_template("system_previews.html", rows=rows, total=sum(r["bytes"] for r in rows))

    @app.route("/api/system/storage/docker")
    def api_system_storage_docker():
        """Image and container sizes, read through the Docker socket when it is mounted (loaded after the page, because Docker takes a moment to add up a container)."""
        return jsonify(ok=True, **data_usage.docker_sizes())

    @app.route("/system/status")
    def status_page():
        """The health list, then how Jellyfin's movies add up to titles here and the breakdown of the files behind them (the dashboard keeps only the counts)."""
        permissions = getattr(g, "permissions", None)
        with db.connection() as conn:
            snapshot = status.snapshot(conn)
            reconcile = catalog.jellyfin_movie_reconcile(conn)
            file_counts = file_labels.counts(conn) if access.allowed(permissions, "library") else None
            stats = catalog.dashboard_stats(catalog.list_catalog(conn))
        return render_template("status.html", snapshot=snapshot, reconcile=reconcile, file_counts=file_counts, stats=stats)

    @app.route("/api/status/dismiss-failed", methods=["POST"])
    def api_status_dismiss_failed():
        with db.connection() as conn:
            status.dismiss_failed(conn)
        return jsonify(ok=True, message="Dismissed. A job that fails from now on shows again.")

    def _on_watchlist(conn, media_id):
        me = (g.profile or {}).get("id")
        return bool(me) and watchlist.on_list(conn, me, media_id)

    @app.route("/movie/<int:media_id>")
    def movie_details(media_id):
        with db.connection() as conn:
            movie = catalog.media_detail(conn, media_id)
            if not movie or movie["kind"] != "movie" or not title_allowed(conn, media_id):
                flash("Movie not found.", "warning")
                return redirect(url_for("index"))
            state = states.movie_state(conn, media_id)
            defs = states.definitions(conn)
            progress = _progress(conn, "movie", media_id)
            on_watchlist = _on_watchlist(conn, media_id)
            librarian = access.allowed(getattr(g, "permissions", None), "library")
            copies = quality.copies(conn, media_id) if librarian else None
            poor = poor_quality.page_state(conn, media_id) if librarian else None
        return render_template("movie_details.html", movie=movie, state=state, state_info=defs.get(state["state"], defs["missing"]), progress=progress, on_watchlist=on_watchlist, copies=copies, poor=poor)

    @app.route("/show/<int:media_id>")
    def show_details(media_id):
        with db.connection() as conn:
            show = catalog.media_detail(conn, media_id)
            if not show or show["kind"] != "show" or not title_allowed(conn, media_id):
                flash("Show not found.", "warning")
                return redirect(url_for("index"))
            summary = states.show_summary(conn, media_id)
            defs = states.definitions(conn)
            defs["partial"] = {"code": "partial", "label": "Partial", "color": "warning", "icon": "bi-pie-chart"}
            progress = _progress(conn, "show", media_id)
            me = (g.profile or {}).get("id")
            follow = {"muted": notification_store.is_muted(conn, me, media_id)} if me and access.allowed(g.permissions, "notifications") else None
            on_watchlist = _on_watchlist(conn, media_id)
            poor = poor_quality.page_state(conn, media_id) if access.allowed(g.permissions, "library") else None
            episode_files = episode_link.show_groups(conn, media_id) if access.allowed(g.permissions, "library") else []
        return render_template("show_details.html", show=show, summary=summary, defs=defs, progress=progress, follow=follow, on_watchlist=on_watchlist, poor=poor, episode_files=episode_files)

    @app.route("/healthz")
    def healthz():
        """For the container's health check and uptime monitors: the app is up and the database answers. Reveals nothing else, so it needs no sign-in."""
        try:
            with db.connection(timeout=2) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            logger.warning(f"Health check failed: {type(exc).__name__}")
            response, code = jsonify(status="down"), 503
        else:
            response, code = jsonify(status="ok"), 200
        response.status_code = code
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/status")
    def public_status():
        """Read-only status for Homepage and Uptime Kuma. Off (404) until STATUS_TOKEN is set; then it needs `Authorization: Bearer <STATUS_TOKEN>`."""
        from synchub import public_status as status
        if not status.token():
            response, code = jsonify(error="not found"), 404
        elif not status.token_matches(request.headers.get("Authorization", "")):
            response, code = jsonify(error="unauthorized"), 401
        else:
            logger.debug("Status report requested")
            body = status.report()
            response, code = jsonify(body), (503 if body["status"] == "down" else 200)
        response.status_code = code
        response.headers["Cache-Control"] = "no-store"
        return response

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
                tmdb = TmdbClient()
                results = ratings.for_results(tmdb, search.find(tmdb, query, kind), current_limit())
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

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """The site's access token (SYNCHUB_ACCESS_TOKEN): asked once per device, then remembered. With no token set there is nothing to ask."""
        from flask import current_app
        from synchub import access
        from synchub.web import TOKEN_COOKIE, cookie_options, safe_next
        destination = safe_next(request.values.get("next"))
        if not access.access_token():
            return redirect(destination)
        error, status = None, 200
        if request.method == "POST":
            wait = access.login_wait(request.remote_addr)
            if wait:
                error, status = f"Too many wrong tries. Wait {wait} seconds.", 429
            elif access.token_matches(request.form.get("token", "").strip()):
                response = redirect(destination)
                response.set_cookie(TOKEN_COOKIE, access.issue_token_proof(current_app.secret_key), max_age=365 * 86400, **cookie_options())
                return response
            else:
                access.login_failed(request.remote_addr)
                error, status = "That is not the access token.", 401
        return render_template("login.html", destination=destination, error=error), status

    @app.route("/logout", methods=["POST"])
    def logout():
        from synchub.web import TOKEN_COOKIE
        response = redirect(url_for("login"))
        response.delete_cookie(TOKEN_COOKIE)
        return response

    @app.route("/who", methods=["GET", "POST"])
    def who():
        """'Who is watching?': pick a profile, or carry on as a guest. A profile with a PIN asks for it; either choice is remembered on this device."""
        from flask import current_app
        from synchub import access
        from synchub.web import PIN_COOKIE, cookie_options, PROFILE_COOKIE, PROFILE_COOKIE_DAYS, safe_next
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
        response.set_cookie(PROFILE_COOKIE, choice, max_age=PROFILE_COOKIE_DAYS * 86400, **cookie_options())
        if proof:
            response.set_cookie(PIN_COOKIE, proof, max_age=days * 86400, **cookie_options())
        else:
            response.delete_cookie(PIN_COOKIE)
        return response

    @app.route("/resolution")
    def resolution_center():
        with db.connection() as conn:
            reviews = catalog.pending_reviews(conn)
            reviewed = {(r["source"], r["key"]) for r in reviews}
            unmatched = [u for u in catalog.unmatched_sources(conn) if (u["source"], u["key"]) not in reviewed]
            in_jellyfin = catalog.unlinked_jellyfin_movies(conn)
        return render_template("resolution.html", reviews=reviews, unmatched=unmatched, in_jellyfin=in_jellyfin)

    @app.route("/tools")
    def tools():
        return render_template("tools.html", task_statuses=tasks.all_statuses(), scheduler_off=app.config.get("SCHEDULER_OFF", False))

    @app.route("/settings")
    def settings_page():
        with db.connection() as conn:
            profile_list = profiles.list_all(conn)
            request_status = automation.request_status(conn)
            worker_list = [{"id": w["id"], "name": w["name"]} for w in workers.list_all(conn) if not w["revoked"]]
        values = {**settings.DEFAULTS, **settings.get_all()}
        from synchub.dashboard import config as dash_config
        dash_tvs = []
        try:
            from synchub.tv import runtime
            dash_tvs = [{"id": d.id, "name": d.name, "label": d.label} for d in runtime.get().devices.values() if "stream_view" in d.capabilities and d.type != "roku"]
        except Exception:
            pass                                                # the TV part is optional; the page works without it
        in_docker = os.getenv("SYNCHUB_IN_DOCKER") == "1"
        return render_template("settings.html", settings=values, profiles=profile_list, relay_workers=worker_list, webhook_status=webhooks.status_for_all(), suggested_synchub_url=request.host_url.rstrip("/"),
                               local_addresses=backup.addresses_on_this_machine(values) if in_docker else [], last_backup=backup.latest(), backup_folder=backup.describe_folder(), request_status=request_status, notify=dispatch.state(),
                               dash_tvs=dash_tvs, dash_url=f"/dash/{dash_config.token()}", now_label=dash_config.now().tzname() or "server time")

    @app.route("/payloads")
    def payloads_page():
        return render_template("payloads.html", settings={**settings.DEFAULTS, **settings.get_all()})

    @app.route("/logs")
    def logs_page():
        return render_template("logs.html")

    @app.route("/activity")
    def activity_page():
        return render_template("activity.html")

    @app.route("/ddns")
    def ddns_page():
        creds = ddns.credentials()
        return render_template("ddns.html", creds=creds, ddns_status=ddns.status(), ddns_history=ddns.history(10), ddns_interval=ddns.interval_minutes(), timeline=ddns.last_timeline())
