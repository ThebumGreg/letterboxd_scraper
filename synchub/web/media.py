"""The Media page: Library, Workers, Jobs, Storage and the other tabs of one page.

Each tab's own address (/library, /workers, /jobs, /storage, ...) shows it (each opens its own tab), so links and bookmarks keep working. Each tab is rendered on its own,
so the tabs keep their own template variables, and a person only gets the tabs their permissions allow."""
from flask import g, jsonify, render_template

from synchub import access, db, storage

# (key, label, endpoint that opens it, what it holds)
TABS = (
    ("library", "Library", "library_page", "Every video file on your shares and what to do with it"),
    ("originals", "Cleanup", "workers_originals_page", "Original files kept after a file was replaced or converted: restore, keep longer or delete"),
    ("workers", "Workers", "workers_page", "The PCs that do the heavy work, and the hardware behind each"),
    ("jobs", "Jobs", "jobs_page", "What the workers have been asked to do and how it is going"),
    ("storage", "Storage", "storage_page", "The shares the workers mount"),
    ("options", "Options", "library_options_page", "Where files go, when films are compressed, previews and conversion profiles"),
)


def _data(key, conn):
    if key == "library":
        from synchub.web import library, quality_web
        from synchub.web import versions
        return {**library.overview(conn), **versions.overview(conn), "qual": quality_web.overview(conn, full=False)}
    if key == "options":
        from synchub import ai_test
        from synchub.web import library, quality_web
        return {**library.overview(conn), "qual": quality_web.overview(conn, full=False), "ai": ai_test.overview(conn)}
    if key in ("workers", "jobs"):
        from synchub.web import fleet
        return fleet.snapshot(conn)
    if key == "originals":
        from synchub.web import originals
        return originals.overview(conn)
    from synchub.web import shares
    return {"roots": shares.describe(conn), "protocols": storage.PROTOCOLS, "defaults": storage.DEFAULT_OPTIONS, "marker": storage.MARKER, "smb_login": storage.smb_login_status(conn)}


def _pane(key, conn):
    data = _data(key, conn)
    return {"tab": render_template(f"_{key}_tab.html", **data), "scripts": render_template(f"_{key}_scripts.html", **data)}


def render_media(active):
    """The page with `active` open. Only that tab is built now (building all eight made every visit as slow as the slowest tab); the others are listed for the
    tabs the person may use and fetched the first time they are opened (see `pane`)."""
    permissions = getattr(g, "permissions", None)
    shown = [t for t in TABS if t[0] == active or access.allowed(permissions, access.required(t[2]))]
    with db.connection() as conn:
        panes = {active: _pane(active, conn)}
    return render_template("media.html", tabs=shown, active=active, panes=panes)


def register(app):
    @app.route("/media/pane/<key>")
    def media_pane(key):
        """One tab's content and scripts, for the page to fill in when the tab is first opened."""
        tab = next((t for t in TABS if t[0] == key), None)
        permissions = getattr(g, "permissions", None)
        if tab is None or not access.allowed(permissions, access.required(tab[2])):
            return jsonify(ok=False, message="That tab is not available to you."), 403
        with db.connection() as conn:
            return jsonify(ok=True, **_pane(key, conn))
