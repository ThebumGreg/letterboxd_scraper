"""The morning dashboard: the page the TV opens (its own long address, no login) and the buttons and tests on the Settings page."""
import secrets

from flask import abort, jsonify, render_template, request

from synchub import settings, unifi_network
from synchub.dashboard import calendars, config, data, school, show


def _checked(token):
    if not secrets.compare_digest(str(token).encode(), config.token().encode()):
        abort(404)


def register(app):
    @app.get("/dash/<token>")
    def dash_view(token):
        _checked(token)
        response = app.make_response(render_template("dashboard_tv.html", token=token))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/dash/<token>/data")
    def dash_data(token):
        _checked(token)
        response = jsonify(data.build())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/dashboard/status")
    def api_dashboard_status():
        import json
        try:
            problem = json.loads(settings.get("dash_problem") or "{}")
        except ValueError:
            problem = {}
        try:
            state = json.loads(settings.get("dash_state") or "{}")
        except ValueError:
            state = {}
        today = config.now().date().isoformat()
        presence = school.load_state(config.now().date())
        return jsonify(ok=True, url=f"/dash/{config.token()}", shown=bool(state.get("day") == today and state.get("shown")), problem=problem,
                       presence={name: p.get("state") for name, p in presence["kids"].items()}, presence_error=presence.get("error", ""))

    @app.post("/api/tv/dashboard/<action>")
    def api_tv_dashboard_action(action):
        if action not in ("show", "hide"):
            abort(404)
        try:
            steps = show.start(manual=True) if action == "show" else show.stop(manual=True)
        except show.ShowError as exc:
            return jsonify(ok=False, message=str(exc))
        except Exception as exc:
            return jsonify(ok=False, message=f"{exc.__class__.__name__}: {exc}")
        return jsonify(ok=True, message=steps[-1] if steps else "Done.", steps=steps)

    @app.get("/api/dashboard/clients")
    def api_dashboard_clients():
        try:
            clients = unifi_network.NetworkClient().clients()
        except unifi_network.NetworkError as exc:
            return jsonify(ok=False, message=str(exc))
        return jsonify(ok=True, clients=clients)

    @app.post("/api/dashboard/test_calendar")
    def api_dashboard_test_calendar():
        url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
        if not url:
            return jsonify(ok=False, message="Paste the calendar link first.")
        try:
            calendars._cache.pop(url.replace("webcal://", "https://"), None)
            found = calendars.for_person(url, config.now())
        except calendars.CalendarError as exc:
            return jsonify(ok=False, message=str(exc))
        except Exception as exc:
            return jsonify(ok=False, message=f"The calendar could not be read ({exc.__class__.__name__}).")
        titles = [e["title"] for e in found["today"] if not e["all_day"]][:3]
        return jsonify(ok=True, message=f"Read the calendar. {len(found['today'])} event(s) today" + (f": {', '.join(titles)}" if titles else "") + ".")
