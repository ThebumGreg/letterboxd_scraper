"""The Media page: Library, Workers, Jobs, Storage and the other tabs of one page.

Each tab's own address (/library, /workers, /jobs, /storage, ...) shows it (each opens its own tab), so links and bookmarks keep working. Each tab is rendered on its own,
so the tabs keep their own template variables, and a person only gets the tabs their permissions allow."""
from flask import g, render_template

from synchub import access, db, storage

# (key, label, endpoint that opens it, what it holds)
TABS = (
    ("library", "Library", "library_page", "Every video file on your shares and what to do with it"),
    ("versions", "Versions", "library_versions_page", "Smaller extra copies of big films and Dolby Vision 7 conversions, for you to approve"),
    ("originals", "Originals", "workers_originals_page", "Original files kept after a file was replaced: restore, keep longer or delete"),
    ("workers", "Workers", "workers_page", "The PCs that do the heavy work, and the hardware behind each"),
    ("jobs", "Jobs", "jobs_page", "What the workers have been asked to do and how it is going"),
    ("storage", "Storage", "storage_page", "The shares the workers mount"),
    ("options", "Options", "library_options_page", "Where files go, when films are compressed, previews and conversion profiles"),
)


def _data(key, conn):
    if key == "library":
        from synchub.web import library
        return library.overview(conn)
    if key == "versions":
        from synchub.web import versions
        return versions.overview(conn)
    if key == "options":
        from synchub.web import library
        return library.overview(conn)
    if key in ("workers", "jobs"):
        from synchub.web import fleet
        return fleet.snapshot(conn)
    if key == "originals":
        from synchub.web import originals
        return originals.overview(conn)
    from synchub.web import shares
    return {"roots": shares.describe(conn), "protocols": storage.PROTOCOLS, "defaults": storage.DEFAULT_OPTIONS, "marker": storage.MARKER, "smb_login": storage.smb_login_status(conn)}


def render_media(active):
    """The page with `active` open. The other tabs are included when the person may use them."""
    permissions = getattr(g, "permissions", None)
    shown = [t for t in TABS if t[0] == active or access.allowed(permissions, access.required(t[2]))]
    panes = {}
    with db.connection() as conn:
        for key, *_ in shown:
            data = _data(key, conn)
            panes[key] = {"tab": render_template(f"_{key}_tab.html", **data), "scripts": render_template(f"_{key}_scripts.html", **data)}
    return render_template("media.html", tabs=shown, active=active, panes=panes)
