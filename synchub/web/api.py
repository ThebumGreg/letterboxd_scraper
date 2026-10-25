"""JSON endpoints used by the pages."""
import json
import queue
import threading
import logging
from datetime import date

from flask import Response, g, jsonify, request

from synchub import automation, catalog, db, jellyfin_events, matching, ratings, search, settings, states, tasks, watch, webhooks
from synchub.automation import RequestError
from synchub.clients import ApiError, ConfigError
from synchub.clients.arr import RadarrClient, SonarrClient
from synchub.clients.jellyfin import JellyfinClient
from synchub.clients.tmdb import TmdbClient
from synchub.web import current_limit, title_allowed

logger = logging.getLogger(__name__)


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


def announce_rows(media_ids):
    """Tells every open dashboard the new state of the titles that just changed (a new label, colour and download progress)."""
    with db.connection() as conn:
        rows = catalog.list_catalog(conn, only=media_ids)
    payload = []
    for r in rows:
        in_library = r["state"] == "in_library"
        payload.append({"id": r["id"], "state": r["state"], "label": r["state_label"], "color": r["state_color"], "icon": r["state_icon"],
                        "progress": r["progress_pct"], "link": r["jellyfin_link"] if in_library else "", "jellyfin_item": r["jellyfin_item_id"],
                        "summary": f"{r['in_library']} of {r['aired']} episodes" if r["kind"] == "show" and r.get("aired") else "",
                        "added": r["date_added"] or "", "added_label": r["date_added_label"] or ""})
    announcer.announce(sse(json.dumps({"event": "rows", "rows": payload})))


if announce_rows not in tasks.state_listeners:
    tasks.state_listeners.append(announce_rows)


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
    # ---------------------------------------------------------------- shared by the settings helpers and webhook setup
    def _endpoint(fields, service):
        """(url, api key, problem) for a service from what is typed in the form, falling back to what is saved. The saved key is
        never sent to a different URL than the saved one."""
        typed = lambda name: (fields.get(name) or "").strip()
        url, key = typed(f"{service}_url").rstrip("/") or settings.get(f"{service}_url").rstrip("/"), typed(f"{service}_api_key")
        if url != settings.get(f"{service}_url").rstrip("/") and not key:
            return None, None, "Enter the API key too when testing a different URL."
        return url, key or settings.get(f"{service}_api_key"), None

    # ---------------------------------------------------------------- tasks and live events
    @app.route("/api/tasks")
    def api_tasks():
        return jsonify(tasks=tasks.all_statuses())

    @app.route("/api/activity")
    def api_activity():
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
        if service not in ("radarr", "sonarr", "jellyfin"):
            return fail("Unknown service.", 404)
        if service == "jellyfin":
            # Kept whatever it is (the plugin sends text/plain unless told otherwise, so the body is read as JSON regardless of its type).
            try:
                event, payload = jellyfin_events.store(request.get_data(), request.remote_addr)
            except Exception as exc:
                logger.error(f"Could not keep a Jellyfin event: {exc}")
                event, payload = jellyfin_events.event_type({}), {}
        else:
            payload = request.get_json(silent=True)
            payload = payload if isinstance(payload, dict) else {}
            event = payload.get("eventType") or payload.get("NotificationType") or "WebhookTrigger"
        webhooks.record(service, event)
        if service == "jellyfin":
            try:
                tasks.watch_event(event, payload)               # someone stopped playing something: their watch record is read again
            except Exception as exc:
                logger.warning(f"Could not act on a Jellyfin {event} event for watch history: {exc}")
        action = webhooks.classify(service, event)
        if action == "ignore":
            return jsonify(status="received", service=service)
        if service in ("radarr", "sonarr"):
            if action == "queue":
                tasks.refresh_soon(f"{service}_queue", lambda: tasks.poll_queue(service, force=True))
            else:
                tasks.refresh_soon(service, tasks.with_reconcile(tasks.sync_radarr if service == "radarr" else tasks.sync_sonarr))
            if event in ("Grab", "Download"):
                movie, series = payload.get("movie") or {}, payload.get("series") or {}
                announcer.announce(sse(json.dumps({"service": service, "event": event, "tmdb_id": movie.get("tmdbId"), "tvdb_id": series.get("tvdbId"),
                                                   "state": "downloading" if event == "Grab" else "downloaded",
                                                   "queue_status": "Downloading" if event == "Grab" else "Downloaded"})))
        else:
            tasks.refresh_soon("jellyfin", tasks.with_reconcile(tasks.sync_jellyfin_movies, tasks.sync_jellyfin_tv))
        return jsonify(status="received", service=service)

    @app.route("/api/webhook-setup/<service>", methods=["POST"])
    def api_webhook_setup(service):
        """Creates the SyncHub webhook inside Radarr/Sonarr or Jellyfin's Webhook plugin, using the address typed in the form (the service must be able to reach it)."""
        if service not in ("radarr", "sonarr", "jellyfin"):
            return fail("Unknown service.", 404)
        fields = (request.get_json(silent=True) or {}).get("fields", {})
        try:
            base = webhooks.check_base_url(fields.get("synchub_url") or settings.get("synchub_url"))
            url, key, problem = _endpoint(fields, service)
            if problem:
                return fail(problem)
            if not url or not key:
                return fail("Enter the URL and API key.")
            hook = webhooks.webhook_url(base, service)
            if service == "jellyfin":
                try:
                    result = webhooks.ensure_jellyfin_webhook(JellyfinClient(url, key), hook)
                except ApiError as exc:
                    raise webhooks.WebhookSetupError(f"Jellyfin would not take the webhook settings. {exc}")
            else:
                client = (RadarrClient if service == "radarr" else SonarrClient)(url, key)
                try:
                    result = webhooks.ensure_webhook(client, hook)
                except ApiError as exc:
                    raise webhooks.WebhookSetupError(f"{client.label} would not save the webhook. If it says it could not send a test message, it cannot reach {hook} from where it runs. {exc}")
        except webhooks.WebhookSetupError as exc:
            return fail(str(exc))
        except ConfigError as exc:
            return fail(str(exc))
        except ApiError as exc:
            return fail(str(exc), 502)
        settings.set_value("synchub_url", base)
        label = {"radarr": "Radarr", "sonarr": "Sonarr", "jellyfin": "Jellyfin"}[service]
        if service == "jellyfin":
            message = {"exists": f"Jellyfin's Webhook plugin already sends everything to {hook}.",
                       "created": f"Added a destination to Jellyfin's Webhook plugin. It sends all {result['types']} notification types, for every kind of item and every user, to {hook}.",
                       "updated": f"Updated Jellyfin's Webhook destination for {hook}: all {result['types']} notification types, every kind of item, every user."}[result["status"]]
        else:
            message = (f"{label} already has a webhook for {hook}." if result["status"] == "exists"
                       else f"Created the webhook in {label}. It will post to {hook} on grab, import, upgrade, rename, add and delete.")
        return jsonify(ok=True, message=message, **result)

    # ---------------------------------------------------------------- parental controls: what this person's rating limit lets them touch
    def _check_title(media_id):
        with db.connection() as conn:
            if not title_allowed(conn, media_id):
                raise RequestError("That title is not available for your profile.")

    def _check_tmdb(kind, tmdb_id):
        """For a title found by search, which may not be in SyncHub yet: look its rating up (once) before anything is sent for it."""
        limit = current_limit()
        if limit is None:
            return
        try:
            known = ratings.ensure(TmdbClient(), kind, tmdb_id)
        except ApiError:
            known = None
        if known is None or not ratings.permitted(known["rank"], limit):
            raise RequestError("That title is not available for your profile.")

    # ---------------------------------------------------------------- requests to Radarr / Sonarr
    def after_change(service):
        job = tasks.with_reconcile(tasks.sync_radarr if service == "radarr" else tasks.sync_sonarr)
        tasks.refresh_soon(service, job)

    @app.route("/api/request/movie/<int:media_id>", methods=["POST"])
    def api_request_movie(media_id):
        def go():
            _check_title(media_id)
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
            _check_title(media_id)
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
            _check_title(media_id)
            with db.connection() as conn:
                result = automation.request_episodes(conn, SonarrClient(), media_id, ids, resync=tasks.resync_sonarr)
            after_change("sonarr")
            return {"message": f"Requested {result['episodes']} episode(s) in Sonarr."}
        return _guard(go)

    @app.route("/api/request/show/<int:media_id>/all", methods=["POST"])
    def api_request_all(media_id):
        def go():
            _check_title(media_id)
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
            if not title_allowed(conn, media_id):
                return fail("That title is not available for your profile.", 403)
            defs = states.definitions(conn)
            eps = states.season_episodes(conn, media_id, season_number, today)
            viewer = watch.user_of(getattr(g, "profile", None))
            watched = watch.item_states(conn, viewer, [e["jellyfin_item_id"] for e in eps])        # what the person using this page has watched, by Jellyfin item
        for e in eps:
            w = watched.get(watch.norm(e["jellyfin_item_id"]))
            e["watch"] = {k: w[k] for k in ("state", "state_label", "percent", "position_label", "runtime_label", "last_played_label", "play_count", "resume_label")} if w else None
            info = defs.get(e["state"], defs["missing"])
            e.update(state_label=info["label"], state_color=info["color"], state_icon=info.get("icon"),
                     progress_pct=round(e["progress"] * 100) if e["progress"] is not None else None,
                     can_request=e["state"] == "missing",
                     jellyfin_link=catalog.jellyfin_item_link(e["jellyfin_item_id"]))
        return jsonify(ok=True, season=season_number, label="Specials (Season 0)" if season_number == 0 else f"Season {season_number}", episodes=eps)

    @app.route("/api/show/<int:media_id>/refresh", methods=["POST"])
    @app.route("/api/movie/<int:media_id>/refresh", methods=["POST"])
    def api_refresh(media_id):
        def go():
            _check_title(media_id)
            try:
                return {"message": tasks.refresh_media(media_id)}
            except LookupError:
                raise RequestError("Unknown title.")
            except Exception as exc:
                if isinstance(exc, (ConfigError, ApiError)):
                    raise
                raise ApiError(str(exc))
        return _guard(go)

    # ---------------------------------------------------------------- search and add
    def _tmdb_id(value):
        text = str(value or "").strip()
        if not text.isdigit():
            raise RequestError("That is not a TMDB ID.")
        return text

    def _who():
        from synchub.web import current_profile
        with db.connection() as conn:
            return current_profile(conn)

    def _credit(profile):
        return f" Credited to {profile['name']}." if profile else " Not credited to anyone: choose a profile at the top to credit your requests."

    @app.route("/api/search/show/<int:tmdb_id>/seasons")
    def api_search_seasons(tmdb_id):
        def go():
            _check_tmdb("show", tmdb_id)
            return search.season_choices(TmdbClient(), tmdb_id)
        return _guard(go)

    @app.route("/api/search/add/movie", methods=["POST"])
    def api_search_add_movie():
        data = request.get_json(silent=True) or {}

        def go():
            profile = _who()
            tmdb_id = _tmdb_id(data.get("tmdb_id"))
            _check_tmdb("movie", tmdb_id)
            got = search.add_movie(TmdbClient(), RadarrClient(), tmdb_id, profile["id"] if profile else None)
            after_change("radarr")
            return {"message": f"Sent '{got['title']}' to Radarr." + _credit(profile), "media_id": got["media_id"], "url": f"/movie/{got['media_id']}"}
        return _guard(go)

    @app.route("/api/search/add/show", methods=["POST"])
    def api_search_add_show():
        data = request.get_json(silent=True) or {}
        seasons = data.get("seasons")
        if not isinstance(seasons, list) or not all(isinstance(s, int) and not isinstance(s, bool) for s in seasons):
            return fail("Send the list of seasons to add.")

        def go():
            profile = _who()
            tmdb_id = _tmdb_id(data.get("tmdb_id"))
            _check_tmdb("show", tmdb_id)
            got = search.add_show(TmdbClient(), SonarrClient(), tmdb_id, seasons, profile["id"] if profile else None)
            after_change("sonarr")
            verb = "Sent" if got["status"] == "added" else "Updated"
            return {"message": f"{verb} '{got['title']}' in Sonarr (seasons {', '.join(str(s) for s in got['seasons'])})." + _credit(profile),
                    "media_id": got["media_id"], "url": f"/show/{got['media_id']}"}
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

    @app.route("/api/resolution/approve-all", methods=["POST"])
    def api_resolution_approve_all():
        ids = (request.get_json(silent=True) or {}).get("review_ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            return fail("Send the list of suggestion IDs to approve.")
        with db.connection() as conn:
            result = matching.approve_best(conn, ids)
        if result["approved"]:
            reconcile_soon()
        parts = [f"Approved {len(result['approved'])}."]
        if result["skipped"]:
            parts.append(f"{len(result['skipped'])} left for you because several suggestions are equally likely.")
        if result["failed"]:
            parts.append(f"{len(result['failed'])} could not be linked (see the log).")
        if result["stale"]:
            parts.append(f"{len(result['stale'])} had already been decided.")
        return jsonify(ok=True, message=" ".join(parts), **result)

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

        def pick(url_name):
            return _endpoint(fields, url_name.removesuffix("_url"))

        try:
            if service == "tmdb":
                from synchub.clients.tmdb import TmdbClient
                TmdbClient(typed("tmdb_api_key") or None).configuration()
                return jsonify(ok=True, message="TMDB key accepted.")
            if service in ("jellyfin", "radarr", "sonarr"):
                url, key, problem = pick(f"{service}_url")
                if problem:
                    return jsonify(ok=False, message=problem)
                if not url or not key:
                    return jsonify(ok=False, message="Enter the URL and API key.")
                if service == "jellyfin":
                    from synchub.clients.jellyfin import JellyfinClient
                    info = JellyfinClient(url, key).system_info()
                    return jsonify(ok=True, message=f"Connected to {info.get('ServerName') or 'Jellyfin'} {info.get('Version') or ''}".strip())
                client = (RadarrClient if service == "radarr" else SonarrClient)(url, key)
                info = client.status()
                hello = f"Connected to {info.get('appName') or service.title()} {info.get('version') or ''}".strip()
                # Connecting is not enough: new titles need a quality profile and root folder this service actually has.
                try:
                    automation.resolve_destination(client, typed(f"{service}_quality_profile_id") or None, typed(f"{service}_root_folder") or None)
                except RequestError as exc:
                    return jsonify(ok=False, message=f"{hello}, but adding titles would fail: {exc}")
                return jsonify(ok=True, message=f"{hello}. The quality profile and root folder are valid.")
            if service == "pushover":
                from synchub.notifications import dispatch
                from synchub.notifications.providers import pushover
                token = typed("pushover_app_token") or pushover.app_token()
                in_use = token == pushover.app_token()               # a token only being tried says nothing about the one in use
                result = pushover.check_token(token, remember_quota=in_use)
                if not result.ok:
                    return jsonify(ok=False, message=result.detail)
                if in_use:
                    dispatch.clear_pause("pushover")                # the token in use works: any hold put on by an earlier failure can end
                left = result.data
                return jsonify(ok=True, message=f"Pushover accepts that token. {left['remaining']} of {left['limit']} messages are left this month." if left["limit"] is not None and left["remaining"] is not None
                               else "Pushover accepts that token.")
            if service == "notifications":
                from synchub.notifications import providers
                from synchub.notifications.providers.base import Message
                result = providers.get("apprise").send({"urls": typed("apprise_urls") or settings.get("apprise_urls")}, Message("SyncHub Test", "Test notification from SyncHub."))
                return jsonify(ok=result.ok, message="Notification sent." if result.ok else result.detail)
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
