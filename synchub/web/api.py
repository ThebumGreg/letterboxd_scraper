"""JSON endpoints used by the pages."""
import json
import queue
import threading
import time
from datetime import date

from flask import Response, jsonify, request

from synchub import automation, catalog, db, matching, settings, states, tasks
from synchub.automation import RequestError
from synchub.clients import ApiError, ConfigError
from synchub.clients.arr import RadarrClient, SonarrClient


class MessageAnnouncer:
    """Fan-out of small server-sent events to every open dashboard."""
    def __init__(self):
        self.listeners, self._lock = [], threading.Lock()

    def listen(self):
        q = queue.Queue(maxsize=5)
        with self._lock:
            self.listeners.append(q)
        return q

    def forget(self, q):
        with self._lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def announce(self, message):
        with self._lock:
            for q in list(self.listeners):
                try:
                    q.put_nowait(message)
                except queue.Full:
                    self.listeners.remove(q)


announcer = MessageAnnouncer()


def sse(data, event=None):
    return (f"event: {event}\n" if event else "") + f"data: {data}\n\n"


def fail(message, status=400):
    return jsonify(ok=False, message=message), status


def _guard(fn):
    """Runs a request action, turning the expected failures into a JSON message."""
    try:
        return jsonify(ok=True, **fn())
    except (RequestError, ConfigError) as exc:
        return fail(str(exc))
    except ApiError as exc:
        return fail(str(exc), 502)


def register(app):
    # ---------------------------------------------------------------- tasks and live events
    @app.route("/api/tasks")
    def api_tasks():
        return jsonify(tasks=tasks.all_statuses())

    @app.route("/api/diagnostics")
    def api_diagnostics():
        from synchub import diagnostics
        return jsonify(diagnostics.snapshot())

    @app.route("/api/stream/events")
    def stream_events():
        def stream():
            q = announcer.listen()
            try:
                while True:
                    try:
                        yield q.get(timeout=15)
                    except queue.Empty:
                        yield sse('{"status": "ping"}', "ping")
            finally:
                announcer.forget(q)
        return Response(stream(), mimetype="text/event-stream")

    @app.route("/api/webhook/<service>", methods=["POST"])
    def api_webhook(service):
        payload = request.get_json(silent=True)
        payload = payload if isinstance(payload, dict) else {}
        event = payload.get("eventType") or payload.get("NotificationType") or "WebhookTrigger"
        if service in ("radarr", "sonarr"):
            job = tasks.with_reconcile(tasks.sync_radarr if service == "radarr" else tasks.sync_sonarr)
            tasks.refresh_soon(service, job)
            if event in ("Grab", "Download"):
                movie, series = payload.get("movie") or {}, payload.get("series") or {}
                announcer.announce(sse(json.dumps({"service": service, "event": event, "tmdb_id": movie.get("tmdbId"), "tvdb_id": series.get("tvdbId"),
                                                   "queue_status": "Downloading" if event == "Grab" else "Downloaded"})))
        elif service == "jellyfin":
            tasks.refresh_soon("jellyfin", tasks.with_reconcile(tasks.sync_jellyfin_movies, tasks.sync_jellyfin_tv))
        return jsonify(status="received", service=service)

    # ---------------------------------------------------------------- requests to Radarr / Sonarr
    def after_change(service):
        job = tasks.with_reconcile(tasks.sync_radarr if service == "radarr" else tasks.sync_sonarr)
        tasks.refresh_soon(service, job)

    @app.route("/api/request/movie/<int:media_id>", methods=["POST"])
    def api_request_movie(media_id):
        def go():
            with db.connection() as conn:
                title = automation.request_movie(conn, RadarrClient(), media_id)
            after_change("radarr")
            return {"message": f"Sent '{title}' to Radarr."}
        return _guard(go)

    @app.route("/api/request/show/<int:media_id>/seasons", methods=["POST"])
    def api_request_seasons(media_id):
        seasons = (request.get_json(silent=True) or {}).get("seasons")
        if not isinstance(seasons, list):
            return fail("Send a list of seasons.")

        def go():
            with db.connection() as conn:
                result = automation.request_seasons(conn, SonarrClient(), media_id, seasons)
            after_change("sonarr")
            note = f" (search problems: {'; '.join(result['search_errors'])})" if result["search_errors"] else ""
            return {"message": f"Requested season(s) {', '.join(map(str, result['seasons']))} in Sonarr.{note}"}
        return _guard(go)

    @app.route("/api/request/show/<int:media_id>/episodes", methods=["POST"])
    def api_request_episodes(media_id):
        ids = (request.get_json(silent=True) or {}).get("episode_ids")
        if not isinstance(ids, list):
            return fail("Send a list of episode IDs.")

        def go():
            with db.connection() as conn:
                result = automation.request_episodes(conn, SonarrClient(), media_id, ids, resync=tasks.resync_sonarr)
            after_change("sonarr")
            return {"message": f"Requested {result['episodes']} episode(s) in Sonarr."}
        return _guard(go)

    @app.route("/api/request/show/<int:media_id>/all", methods=["POST"])
    def api_request_all(media_id):
        def go():
            with db.connection() as conn:
                result = automation.request_all(conn, SonarrClient(), media_id)
            after_change("sonarr")
            return {"message": f"Requested season(s) {', '.join(map(str, result['seasons']))} in Sonarr."}
        return _guard(go)

    # ---------------------------------------------------------------- show data
    @app.route("/api/show/<int:media_id>/season/<int:season_number>")
    def api_season(media_id, season_number):
        today = date.today().isoformat()
        with db.connection() as conn:
            defs = states.definitions(conn)
            eps = states.season_episodes(conn, media_id, season_number, today)
        for e in eps:
            info = defs.get(e["state"], defs["missing"])
            e.update(state_label=info["label"], state_color=info["color"], state_icon=info.get("icon"),
                     progress_pct=round(e["progress"] * 100) if e["progress"] is not None else None,
                     can_request=e["state"] == "missing")
        return jsonify(ok=True, season=season_number, label="Specials (Season 0)" if season_number == 0 else f"Season {season_number}", episodes=eps)

    @app.route("/api/show/<int:media_id>/refresh", methods=["POST"])
    @app.route("/api/movie/<int:media_id>/refresh", methods=["POST"])
    def api_refresh(media_id):
        def go():
            try:
                return {"message": tasks.refresh_media(media_id)}
            except LookupError:
                raise RequestError("Unknown title.")
            except Exception as exc:
                if isinstance(exc, (ConfigError, ApiError)):
                    raise
                raise ApiError(str(exc))
        return _guard(go)

    # ---------------------------------------------------------------- resolution center
    def reconcile_soon():
        tasks.start_async(tasks.reconcile)

    @app.route("/api/resolution/approve", methods=["POST"])
    def api_resolution_approve():
        review_id = (request.get_json(silent=True) or {}).get("review_id")
        with db.connection() as conn:
            media_id = matching.approve_review(conn, review_id) if isinstance(review_id, int) else None
        if media_id is None:
            return fail("That suggestion is no longer pending.")
        reconcile_soon()
        return jsonify(ok=True, message="Linked.")

    @app.route("/api/resolution/dismiss", methods=["POST"])
    def api_resolution_dismiss():
        review_id = (request.get_json(silent=True) or {}).get("review_id")
        with db.connection() as conn:
            done = isinstance(review_id, int) and matching.dismiss_review(conn, review_id)
        return jsonify(ok=True, message="Dismissed.") if done else fail("That suggestion is no longer pending.")

    @app.route("/api/resolution/link", methods=["POST"])
    def api_resolution_link():
        data = request.get_json(silent=True) or {}
        source, key, kind, tmdb_id = data.get("source"), data.get("key"), data.get("kind"), str(data.get("tmdb_id") or "").strip()
        if source not in ("letterboxd", "serializd") or kind not in ("movie", "show") or not key:
            return fail("Unknown item.")
        if not tmdb_id.isdigit():
            return fail("Enter the numeric TMDB ID.")
        with db.connection() as conn:
            title = year = None
            table, col = ("src_letterboxd_entry", "slug") if source == "letterboxd" else ("src_serializd_entry", "serializd_show_id")
            row = conn.execute(f"SELECT MAX(title) AS title, MAX(year) AS year FROM {table} WHERE {col} = ?", (key,)).fetchone()
            if row and row["title"]:
                title, year = row["title"], row["year"]
            matching.link_manually(conn, source, key, kind, tmdb_id, title, year)
        reconcile_soon()
        return jsonify(ok=True, message="Linked.")

    # ---------------------------------------------------------------- settings helpers
    @app.route("/api/test-connection/<service>", methods=["POST"])
    def api_test_connection(service):
        fields = (request.get_json(silent=True) or {}).get("fields", {})
        typed = lambda name: (fields.get(name) or "").strip()

        def pick(url_name, key_name):
            url, key = typed(url_name).rstrip("/") or settings.get(url_name).rstrip("/"), typed(key_name)
            if url != settings.get(url_name).rstrip("/") and not key:
                return None, None, "Enter the API key too when testing a different URL."
            return url, key or settings.get(key_name), None

        try:
            if service == "tmdb":
                from synchub.clients.tmdb import TmdbClient
                TmdbClient(typed("tmdb_api_key") or None).configuration()
                return jsonify(ok=True, message="TMDB key accepted.")
            if service in ("jellyfin", "radarr", "sonarr"):
                url, key, problem = pick(f"{service}_url", f"{service}_api_key")
                if problem:
                    return jsonify(ok=False, message=problem)
                if not url or not key:
                    return jsonify(ok=False, message="Enter the URL and API key.")
                if service == "jellyfin":
                    from synchub.clients.jellyfin import JellyfinClient
                    info = JellyfinClient(url, key).system_info()
                    return jsonify(ok=True, message=f"Connected to {info.get('ServerName') or 'Jellyfin'} {info.get('Version') or ''}".strip())
                info = (RadarrClient if service == "radarr" else SonarrClient)(url, key).status()
                return jsonify(ok=True, message=f"Connected to {info.get('appName') or service.title()} {info.get('version') or ''}".strip())
            if service == "notifications":
                from synchub import notify
                if not notify.urls():
                    return jsonify(ok=False, message="No Apprise URLs set.")
                sent = notify.send("SyncHub Test", "Test notification from SyncHub.", force=True)
                return jsonify(ok=sent, message="Notification sent." if sent else "Sending failed.")
        except ConfigError as exc:
            return jsonify(ok=False, message=str(exc))
        except ApiError as exc:
            return jsonify(ok=False, message=str(exc))
        except Exception as exc:
            return jsonify(ok=False, message=f"Could not connect: {exc.__class__.__name__}")
        return jsonify(ok=False, message="Unknown service."), 404

    def servarr_list(service, what):
        data = request.get_json(silent=True) or {}
        url, key = (data.get("url") or "").strip().rstrip("/"), (data.get("api_key") or "").strip()
        if url and url != settings.get(f"{service}_url").rstrip("/") and not key:
            return jsonify(error="Enter the API key too when using a different URL."), 400
        try:
            client = (RadarrClient if service == "radarr" else SonarrClient)(url or None, key or None)
            return jsonify(client.quality_profiles() if what == "profiles" else client.root_folders())
        except (ConfigError, ApiError) as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"Could not connect: {exc.__class__.__name__}"), 400

    @app.route("/api/servarr/<service>/quality-profiles", methods=["POST"])
    def api_quality_profiles(service):
        return servarr_list(service, "profiles") if service in ("radarr", "sonarr") else (jsonify([]), 404)

    @app.route("/api/servarr/<service>/root-folders", methods=["POST"])
    def api_root_folders(service):
        return servarr_list(service, "folders") if service in ("radarr", "sonarr") else (jsonify([]), 404)

    @app.route("/api/jellyfin/users")
    def api_jellyfin_users():
        from synchub.clients.jellyfin import JellyfinClient
        if settings.get("disable_jellyfin_connection") == "1":
            return jsonify([])
        try:
            return jsonify([{"id": u.get("Id"), "name": u.get("Name")} for u in JellyfinClient().users()])
        except Exception:
            return jsonify([])
