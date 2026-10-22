"""The v2 web application. Run it with: python -m synchub.web"""
import logging
import os
import secrets
from pathlib import Path

from flask import Flask, g, redirect, request, url_for

from config import BASE_DIR
from synchub import db, tasks

logger = logging.getLogger(__name__)

PROFILE_COOKIE = "synchub_profile"      # a profile's ID, or "0" for "just looking". This says who is using the app; it is not a login.
PROFILE_COOKIE_DAYS = 365
# Pages are gated behind "Who is watching?"; these are not (scripts, images, the live stream and webhook callers cannot choose).
UNGATED_PREFIXES = ("/api/", "/static/", "/poster/")


def current_profile(conn):
    """The profile named by the cookie, or None (a guest, nobody chosen, or a profile that no longer exists)."""
    raw = request.cookies.get(PROFILE_COOKIE, "")
    if not raw.isdigit() or raw == "0":
        return None
    row = conn.execute("SELECT id, name FROM profile WHERE id = ?", (int(raw),)).fetchone()
    return dict(row) if row else None


def safe_next(target):
    """Where to go after choosing: a path on this site only (never another site)."""
    target = (target or "").strip()
    if target.startswith("/") and not target.startswith(("//", "/\\", "/who")):
        return target
    return "/"


def create_app(start_scheduler=False):
    import logger as app_logging      # noqa: F401  (importing it sets up the rotating log file)
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"), static_folder=str(BASE_DIR / "static"))
    app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)

    try:
        from flask_wtf import CSRFProtect
        csrf = CSRFProtect(app)
    except ImportError:
        csrf = None

    db.migrate()
    tasks.reset_stale()

    @app.before_request
    def identify():
        """Works out who is using the app from the cookie, and sends a browser that has not chosen yet to the chooser."""
        g.profile, g.has_profiles = None, False
        if request.path.startswith(UNGATED_PREFIXES):
            return None
        with db.connection() as conn:
            rows = [dict(r) for r in conn.execute("SELECT id, name, jellyfin_user_id FROM profile")]
        g.has_profiles = bool(rows)
        raw = request.cookies.get(PROFILE_COOKIE, "")
        chosen = raw == "0"
        if raw.isdigit() and raw != "0":
            g.profile = next((r for r in rows if r["id"] == int(raw)), None)
            chosen = g.profile is not None
        if rows and not chosen and request.method == "GET" and request.endpoint != "who":
            return redirect(url_for("who", next=request.path))
        return None

    @app.context_processor
    def identity_and_jellyfin_app():
        from synchub import catalog
        with db.connection() as conn:
            config = catalog.jellyfin_app_config(conn)
        profile = getattr(g, "profile", None)
        config["current_user"] = (profile or {}).get("jellyfin_user_id") or ""
        return {"jellyfin_app": config, "current_profile": profile, "has_profiles": getattr(g, "has_profiles", False)}

    from synchub.web import admin, api, fleet, library, pages, shares, worker_api
    for module in (pages, api, admin, fleet, library, shares, worker_api):
        module.register(app)
    if csrf:
        csrf.exempt(app.view_functions["api_webhook"])
        for name, view in app.view_functions.items():
            if name.startswith("worker_api_"):
                csrf.exempt(view)                     # agents have no browser session; they send their own token

    if start_scheduler:
        from synchub import scheduler
        scheduler.start()
    return app
