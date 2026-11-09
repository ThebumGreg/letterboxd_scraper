"""The morning dashboard: the page the TV opens (its own long address, no login) and the buttons and tests on the Settings page."""
import secrets

from flask import abort, jsonify, render_template, request

from synchub import settings, unifi_network
from synchub.dashboard import calendars, config, data, school, show, welcome


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
        response = jsonify(data.build(mode=request.args.get("mode")))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/dashboard/status")
    def api_dashboard_status():
        import json
        try:
            problem = json.loads(settings.get("dash_problem") or "{}")
        except ValueError:
            problem = {}
        now = config.now()
        presence = school.load_state(now.date())
        home = welcome.load_home()
        active = welcome.current(now)
        return jsonify(ok=True, url=f"/dash/{config.token()}", shown=show.shown_now(now.date()), problem=problem,
                       presence={name: p.get("state") for name, p in presence["kids"].items()}, presence_error=presence.get("error", ""),
                       home={name: ("home" if r.get("home") else "away" if r.get("home") is False else "unknown") for name, r in home["people"].items()},
                       welcome_error=home.get("error", ""), welcome_log=home["log"], welcome_now=active["names"] if active else [],
                       birthdays=config.birthdays(now.date()))

    @app.post("/api/tv/dashboard/<action>")
    def api_tv_dashboard_action(action):
        if action not in ("show", "hide", "show_evening", "welcome_test"):
            abort(404)
        try:
            if action == "welcome_test":
                return welcome_test()
            now = config.now()
            if action == "hide":
                open_modes = [m for m in show.MODES if show._state(now.date(), m)["shown"]] or ["morning"]
                welcome.end_now()
                steps = [step for m in open_modes for step in show.stop(manual=True, mode=m)]
            else:
                steps = show.start(manual=True, mode="evening" if action == "show_evening" else show.current_mode(now))
        except show.ShowError as exc:
            return jsonify(ok=False, message=str(exc))
        except Exception as exc:
            return jsonify(ok=False, message=f"{exc.__class__.__name__}: {exc}")
        return jsonify(ok=True, message=steps[-1] if steps else "Done.", steps=steps)

    def welcome_test():
        """Shows the welcome page on the TV now for the first person who has a phone (or the first person), as if they had just come home."""
        now = config.now()
        who = welcome.people() or config.people()
        if not who:
            return jsonify(ok=False, message="Add a person first.")
        shown, why = welcome.present(now, who[:1])
        return jsonify(ok=shown, message=f"Welcome home page sent for {who[0]['name']}." + (f" ({why})" if why else "") if shown else f"Not shown: {why}")

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
