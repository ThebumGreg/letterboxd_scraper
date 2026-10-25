"""The Media page: Library, Workers and Storage as tabs of one page.

/library, /workers and /storage all show it (each opens its own tab), so links and bookmarks keep working. Each tab is rendered on its own,
so the three keep their own template variables, and a person only gets the tabs their permissions allow."""
from flask import g, render_template

from synchub import access, db, storage

# (key, label, endpoint that opens it, what it holds)
TABS = (
    ("library", "Library", "library_page", "Every video file on your shares and what to do with it"),
    ("workers", "Workers", "workers_page", "The PCs that do the heavy work, and their jobs"),
    ("storage", "Storage", "storage_page", "The shares the workers mount"),
)


def _data(key, conn):
    if key == "library":
        from synchub.web import library
        return library.overview(conn)
    if key == "workers":
        from synchub.web import fleet
        return fleet.snapshot(conn)
    from synchub.web import shares
    return {"roots": shares.describe(conn), "protocols": storage.PROTOCOLS, "defaults": storage.DEFAULT_OPTIONS, "marker": storage.MARKER}


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
