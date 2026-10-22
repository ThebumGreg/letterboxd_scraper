"""The v2 web application. Run it with: python -m synchub.web"""
import logging
import os
import secrets
from pathlib import Path

from flask import Flask

from config import BASE_DIR
from synchub import db, tasks

logger = logging.getLogger(__name__)


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

    @app.context_processor
    def jellyfin_app():
        from synchub import catalog
        with db.connection() as conn:
            return {"jellyfin_app": catalog.jellyfin_app_config(conn)}

    from synchub.web import admin, api, pages
    for module in (pages, api, admin):
        module.register(app)
    if csrf:
        csrf.exempt(app.view_functions["api_webhook"])

    if start_scheduler:
        from synchub import scheduler
        scheduler.start()
    return app
