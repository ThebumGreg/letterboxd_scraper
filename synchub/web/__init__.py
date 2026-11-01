"""The v2 web application. Run it with: python -m synchub.web"""
import logging
import os
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

from config import BASE_DIR
from synchub import __version__, access, backup, db, ratings, tasks

logger = logging.getLogger(__name__)

PROFILE_COOKIE = "synchub_profile"      # a profile's ID, or "0" for "just looking": who is using this device
PROFILE_COOKIE_DAYS = 365
TOKEN_COOKIE = "synchub_token"          # signed proof that the site's access token was entered on this device (only when SYNCHUB_ACCESS_TOKEN is set)
PIN_COOKIE = "synchub_pin"              # signed proof that this profile's PIN was entered on this device (only for a profile that has one)

# The main menu. An (endpoint, label) is a link; a (label, [items]) is a dropdown, whose items are links or (None, heading) group titles.
# What a person sees is what their permissions allow, and a heading with nothing under it is left out.
NAV = (
    ("index", "Dashboard"),
    ("tv_remote_page", "Remote"),
    ("Discover", (("discover_page", "New & trending"), ("search_page", "Find & request"), ("watchlist_page", "My watchlist"))),
    ("Media", (                                                                 # these seven are the tabs of one page (synchub/web/media.py)
        ("library_page", "Library"), ("library_versions_page", "Versions"), ("workers_originals_page", "Originals"),
        ("workers_page", "Workers"), ("jobs_page", "Jobs"), ("storage_page", "Storage"), ("library_options_page", "Options"),
        (None, "Matching"), ("resolution_center", "Resolution Center"),
    )),
    ("System", (
        (None, "Run"), ("tools", "Tools & Operations"),
        (None, "Monitor"), ("status_page", "System status"), ("sessions_page", "Sessions"), ("activity_page", "What it's doing now"), ("logs_page", "Logs"), ("jellyfin_events_page", "Jellyfin events"), ("payloads_page", "Payloads"),
        (None, "Admin"), ("ddns_page", "Dynamic DNS"), ("people_page", "People"),
        (None, "Remote"), ("tv_settings_page", "Remote Apps"), ("tv_setup_devices_page", "TVs"), ("tv_setup_advanced_page", "Remote Tools"),
    )),
    ("settings_page", "Settings"),
)


def current_profile(conn):
    """The profile using this request (None for a guest, nobody chosen, a profile that is gone, or one whose PIN has not been entered here)."""
    if hasattr(g, "profile"):
        return g.profile
    rows = _profile_rows(conn)
    raw = request.cookies.get(PROFILE_COOKIE, "")
    row = next((r for r in rows if raw.isdigit() and r["id"] == int(raw)), None)
    return _public(row) if row and (not row["has_pin"] or _pin_entered(row, conn)) else None


def current_limit():
    """The most mature rating (0-3) the person using this request may see, or None for no limit. See synchub.ratings."""
    return getattr(g, "max_rating", None)


def title_allowed(conn, media_id):
    """Whether the person using this request may see this title (their rating limit; see synchub.ratings)."""
    return ratings.media_allowed(conn, media_id, current_limit())


def safe_next(target):
    """Where to go after choosing: a path on this site only (never another site)."""
    target = (target or "").strip()
    if target.startswith("/") and not target.startswith(("//", "/\\", "/who", "/login")):
        return target
    return "/"


def _profile_rows(conn):
    return [dict(r) for r in conn.execute("SELECT id, name, jellyfin_user_id, pin_hash IS NOT NULL AS has_pin, pin_version, permissions, max_rating FROM profile")]


def _public(row):
    return {"id": row["id"], "name": row["name"], "jellyfin_user_id": row["jellyfin_user_id"]}


def _pin_entered(row, conn):
    from flask import current_app
    return access.proof_valid(current_app.secret_key, request.cookies.get(PIN_COOKIE), row["id"], row["pin_version"], access.session_days(conn))


def _wants_json():
    return request.path.startswith("/api/") or request.is_json or request.accept_mimetypes.best == "application/json"


def _deny(label, status=403, message=None):
    if _wants_json():
        return jsonify(ok=False, message=message or f"You don't have access to {label}."), status
    return render_template("forbidden.html", label=label, message=message), status


def create_app(start_scheduler=False, csrf=True):
    """`csrf=False` is for tests that post without a browser's token; the app is never run without it.
    `SYNCHUB_SCHEDULER=0` starts the app without its background jobs (for trying a copy of a live database without a second set of syncs, DDNS updates and notifications)."""
    import logger as app_logging      # noqa: F401  (importing it sets up the rotating log file)
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"), static_folder=str(BASE_DIR / "static"))
    if csrf:
        try:
            from flask_wtf import CSRFProtect
        except ImportError as exc:
            raise RuntimeError("Flask-WTF is needed for CSRF protection: run  pip install -r requirements.txt") from exc
        csrf = CSRFProtect(app)
    else:
        csrf = None

    try:
        saved = backup.copy_before_upgrade()
        if saved:
            logger.info(f"The database is older than this version: a copy was saved as {saved} before upgrading it.")
    except Exception as exc:
        logger.warning(f"Could not save a copy of the database before upgrading it: {exc}")
    db.migrate()
    app.secret_key = os.getenv("SECRET_KEY") or access.persistent_secret()
    tasks.reset_stale()

    @app.before_request
    def identify():
        """Works out who is using the app from the cookies, sends a browser that has not chosen (or has not entered its PIN) to the chooser,
        and refuses what the person's permissions do not allow."""
        g.profile, g.has_profiles, g.permissions, g.pin_profile, g.max_rating = None, False, None, None, None
        endpoint = request.endpoint
        if access.access_token() and endpoint is not None and not access.token_exempt(endpoint):
            header = request.headers.get("Authorization", "")
            bearer = header[7:].strip() if header[:7].lower() == "bearer " else ""
            if not (access.token_matches(bearer) or access.token_proof_valid(app.secret_key, request.cookies.get(TOKEN_COOKIE), 365)):
                if request.path.startswith("/api/") or request.method != "GET":
                    return jsonify(ok=False, message="Enter the access token first.", login=url_for("login")), 401
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if endpoint is None or endpoint in ("static", "poster"):
            return None
        if access.required(endpoint) == access.PUBLIC and endpoint != "who":
            return None                                               # webhooks and workers (polling every few seconds) have their own way in: nobody to look up
        with db.connection() as conn:
            rows = _profile_rows(conn)
            guest = access.guest_permissions(conn)
            days = access.session_days(conn)
            guest_cap = ratings.guest_limit(conn)
        g.has_profiles = bool(rows)
        raw = request.cookies.get(PROFILE_COOKIE, "")
        chosen = raw == "0"
        row = next((r for r in rows if raw.isdigit() and raw != "0" and r["id"] == int(raw)), None)
        if row is not None:
            if row["has_pin"] and not access.proof_valid(app.secret_key, request.cookies.get(PIN_COOKIE), row["id"], row["pin_version"], days):
                g.pin_profile = row                                   # chosen, but its PIN has not been entered on this device lately
            else:
                g.profile, chosen = _public(row), True
        # Until some profile has a PIN nobody can be told apart, so nothing is restricted (the first profile added must be able to open People
        # and set its PIN). After that, only a profile whose PIN was entered here gets its own pages and rating limit: a profile without
        # a PIN can be chosen by anyone on the network, so it is limited to what guests may do (it is still the one requests are credited to).
        lockdown = any(r["has_pin"] for r in rows)
        if not lockdown:
            g.permissions, g.max_rating = None, None
        elif g.profile and row["has_pin"]:
            g.permissions, g.max_rating = access.parse(row["permissions"]), ratings.parse_level(row["max_rating"])
        else:
            g.permissions, g.max_rating = guest, guest_cap
        need = access.required(endpoint)
        if need == access.PUBLIC:
            return None
        api_call = request.path.startswith("/api/")
        if g.pin_profile is not None:
            if api_call or request.method != "GET":
                return jsonify(ok=False, message="Enter this profile's PIN again.", login=url_for("who", profile=g.pin_profile["id"])), 401
            return redirect(url_for("who", next=request.path, profile=g.pin_profile["id"]))
        if rows and not chosen and request.method == "GET" and not api_call:
            return redirect(url_for("who", next=request.path))
        if need is None:
            logger.warning(f"Refused {request.method} {request.path}: the endpoint '{endpoint}' has no access rule (add it to access.RULES).")
            return _deny("this page", message="This page has not been given an access rule yet, so it is closed.")
        if need != access.OPEN and not access.allowed(g.permissions, need):
            return _deny(access.LABELS.get(need, "this page"))
        return None

    @app.context_processor
    def identity_menu_and_jellyfin_app():
        from synchub import catalog
        with db.connection() as conn:
            config = catalog.jellyfin_app_config(conn)
        profile = getattr(g, "profile", None)
        config["current_user"] = (profile or {}).get("jellyfin_user_id") or ""
        permissions = getattr(g, "permissions", None)

        def can(key):
            return access.allowed(permissions, key)

        config["tv"], config["tv_setup"] = can("tv"), can("tv_setup")     # whether "Watch now" may offer the TVs, and a link to add one

        def link(endpoint, label):
            return {"href": url_for(endpoint), "label": label, "endpoint": endpoint} if can(access.required(endpoint)) else None

        menu = []
        for first, second in NAV:
            if isinstance(second, str):
                item = link(first, second)
                if item:
                    menu.append(item)
            else:
                children = []
                for endpoint, label in second:
                    if endpoint is None:
                        children.append({"heading": label})
                    elif item := link(endpoint, label):
                        children.append(item)
                shown = []
                for c in children:
                    if "heading" in c and shown and "heading" in shown[-1]:
                        shown[-1] = c                       # the heading before this one had nothing under it
                    else:
                        shown.append(c)
                while shown and "heading" in shown[-1]:
                    shown.pop()
                children = shown
                if any("href" in c for c in children):
                    menu.append({"label": first, "children": children})
        return {"jellyfin_app": config, "current_profile": profile, "has_profiles": getattr(g, "has_profiles", False), "can": can, "menu": menu,
                "profile_link": link("my_profile", "My profile"),
                "max_rating": getattr(g, "max_rating", None), "app_version": __version__}

    from synchub.web import admin, api, fleet, library, notifications, pages, security, shares, tv, watchlist, worker_api, originals, sessions as sessions_web, versions as versions_web
    for module in (pages, api, admin, fleet, library, notifications, shares, security, tv, watchlist, worker_api, originals, sessions_web, versions_web):
        module.register(app)
    if csrf:
        csrf.exempt(app.view_functions["api_webhook"])
        for name, view in app.view_functions.items():
            if name.startswith("worker_api_"):
                csrf.exempt(view)                     # agents have no browser session; they send their own token

    app.config["SCHEDULER_OFF"] = os.getenv("SYNCHUB_SCHEDULER") == "0"
    if start_scheduler and app.config["SCHEDULER_OFF"]:
        logger.warning("SYNCHUB_SCHEDULER=0: the background jobs are switched off. Nothing runs by itself; the Run buttons on Tools & Operations still work.")
    elif start_scheduler:
        from synchub import scheduler
        scheduler.start()
    return app
