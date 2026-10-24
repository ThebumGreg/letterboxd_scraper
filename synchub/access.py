"""Who may open what: PINs for profiles, the permission each page needs, and the checks that apply them.

This is a household lock, not a login system. A profile with no PIN can be chosen by anyone on the network (as before), so what a profile may
do only means something once the profiles that matter have a PIN. Permissions are per page; a profile whose permission list is empty in the
database (NULL) may open everything, including pages added later, so nothing changes for an installation that never opens the Access page.
"""
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from synchub import db, settings

# key, name in the menu, what it lets a person do
PERMISSIONS = (
    ("dashboard", "Dashboard", "See the dashboard and open movie and show pages"),
    ("search", "Search", "Search for titles to add"),
    ("requests", "Requests", "Request or add titles (the buttons on the Search page and on movie and show pages)"),
    ("resolution", "Resolution Center", "Approve or dismiss title matches"),
    ("ddns", "Dynamic DNS", "See and change the dynamic DNS settings and run an update"),
    ("library", "Library", "Scan shares, review and approve files"),
    ("workers", "Workers", "Add, pause and remove workers and see their jobs"),
    ("storage", "Storage", "Define the shares the workers use"),
    ("tv", "TV remote", "Use the TV remote, choose which apps each TV shows and see what the TVs are playing"),
    ("tv_setup", "TV setup", "Add, change and remove TVs, find them on the network and use the advanced TV tools"),
    ("tools", "Tools & Operations", "Run syncs and tasks, send a test notification, export"),
    ("logs", "Logs", "Read the log and the Jellyfin events that were kept"),
    ("payloads", "Payloads", "Read and clear the API payload log"),
    ("settings", "Settings", "Change service addresses, API keys and the other settings"),
    ("access", "Access", "Create profiles, set PINs and decide who can open what"),
)
KEYS = tuple(key for key, _, _ in PERMISSIONS)
LABELS = {key: label for key, label, _ in PERMISSIONS}

PIN_RE = re.compile(r"^[0-9]{4,8}\Z")             # ASCII digits only (\d would take other scripts' digits), and no trailing newline
FAILURES_BEFORE_LOCK = 5
LOCK_STEP_SECONDS, LOCK_MAX_SECONDS = 30, 900
SESSION_DAYS_DEFAULT = 30
PROOF_SALT = "synchub-pin"

PUBLIC, OPEN = "public", "open"                       # no profile needed / any profile (or a guest) may

# Which permission each endpoint needs, first match wins. A trailing * matches a prefix. A new endpoint that matches nothing is refused
# until it is listed here (a test lists every endpoint of the app to make sure none is forgotten).
RULES = (
    ("who", PUBLIC), ("static", PUBLIC), ("poster", PUBLIC), ("healthz", PUBLIC), ("api_webhook", PUBLIC), ("worker_api_*", PUBLIC),     # webhooks and workers have their own ways in
    ("api_tasks", OPEN),
    ("index", "dashboard"), ("movie_details", "dashboard"), ("show_details", "dashboard"), ("api_season", "dashboard"), ("stream_events", "dashboard"),
    ("search_page", "search"), ("api_search_seasons", "search"),
    ("api_search_add_*", "requests"), ("api_request_*", "requests"), ("api_refresh", "requests"),
    ("resolution_center", "resolution"), ("api_resolution_*", "resolution"),
    ("ddns_*", "ddns"), ("api_ddns_*", "ddns"),
    ("library_*", "library"), ("api_library_*", "library"),
    ("workers_*", "workers"), ("api_worker*", "workers"), ("api_job*", "workers"), ("api_test_copy", "workers"),
    ("storage_page", "storage"), ("api_storage*", "storage"),
    ("tv_setup*", "tv_setup"), ("api_tv_setup*", "tv_setup"), ("tv_*", "tv"), ("api_tv_*", "tv"),
    ("tools", "tools"), ("trigger", "tools"), ("test_notification", "tools"), ("export_csv", "tools"), ("diagnostics_page", "tools"), ("api_diagnostics", "tools"),
    ("logs_page", "logs"), ("api_logs", "logs"), ("jellyfin_event*", "logs"),
    ("payloads_page", "payloads"), ("api_payload_logs", "payloads"), ("api_clear_payload_logs", "payloads"),
    ("settings_page", "settings"), ("update_settings", "settings"), ("api_test_connection", "settings"), ("api_quality_profiles", "settings"),
    ("api_root_folders", "settings"), ("api_webhook_setup", "settings"),
    ("save_profile", "access"), ("delete_profile", "access"), ("api_toggle_profile", "access"), ("api_jellyfin_users", "access"), ("access_page", "access"), ("api_access_*", "access"),
)


class AccessError(Exception):
    """A change that cannot be made; the message is meant for the person."""


def required(endpoint):
    """PUBLIC, OPEN, a permission key, or None for an endpoint nobody has listed (refused)."""
    for pattern, need in RULES:
        if endpoint == pattern or (pattern.endswith("*") and (endpoint or "").startswith(pattern[:-1])):
            return need
    return None


# ---------------------------------------------------------------- permissions
def parse(text):
    """The permissions in a stored value: None means everything (including pages added later), else the listed keys."""
    if text is None:
        return None
    return frozenset(k for k in str(text).split(",") if k in KEYS)


def clean(keys):
    """A list of permission keys in menu order, or an AccessError for one that does not exist."""
    keys = list(keys or [])
    unknown = [k for k in keys if k not in KEYS]
    if unknown:
        raise AccessError(f"There is no permission called '{unknown[0]}'.")
    return [k for k in KEYS if k in keys]


def allowed(permissions, need):
    return permissions is None or need in permissions


def guest_permissions(conn=None):
    value = settings.get("access_guest_permissions", conn=conn)
    return None if value in ("*", None) else parse(value)


def profile_permissions(row):
    return parse(row["permissions"])


def _holders_of_access(conn, change=None):
    """Names of the profiles (and 'guests') that may open the Access page, as it would be after `change` = {profile_id or 'guest': keys}."""
    change = change or {}
    holders = []
    for row in conn.execute("SELECT id, name, permissions FROM profile"):
        perms = parse(",".join(change[row["id"]])) if row["id"] in change else profile_permissions(row)
        if allowed(perms, "access"):
            holders.append(row["name"])
    guest = parse(",".join(change["guest"])) if "guest" in change else guest_permissions(conn)
    if allowed(guest, "access"):
        holders.append("guests")
    return holders


def ensure_access_remains(conn, change=None, removing=None):
    """Refuses a change after which nobody could open the Access page again."""
    holders = _holders_of_access(conn, change)
    if removing is not None:
        holders = [h for h in holders if h != removing]
    if not holders:
        raise AccessError("Somebody has to keep the Access permission, or nobody could change these settings again. Give it to a profile first.")


def set_permissions(conn, profile_id, keys):
    keys = clean(keys)
    if not conn.execute("SELECT 1 FROM profile WHERE id = ?", (profile_id,)).fetchone():
        raise AccessError("That profile no longer exists.")
    ensure_access_remains(conn, {profile_id: keys})
    conn.execute("UPDATE profile SET permissions = ? WHERE id = ?", (None if keys == list(KEYS) else ",".join(keys), profile_id))     # everything stays "everything", pages added later included
    return keys


def set_guest_permissions(conn, keys):
    keys = clean(keys)
    ensure_access_remains(conn, {"guest": keys})
    settings.set_value("access_guest_permissions", "*" if keys == list(KEYS) else ",".join(keys), conn=conn)
    return keys


def ensure_profile_can_go(conn, profile_id):
    """Deleting a profile must not take the last Access holder with it."""
    row = conn.execute("SELECT name FROM profile WHERE id = ?", (profile_id,)).fetchone()
    if row:
        ensure_access_remains(conn, removing=row["name"])


# ---------------------------------------------------------------- PINs
def validate_pin(pin):
    pin = str(pin or "").strip()
    if not PIN_RE.match(pin):
        raise AccessError("A PIN is 4 to 8 digits.")
    return pin


def set_pin(conn, profile_id, pin):
    pin = validate_pin(pin)
    done = conn.execute("UPDATE profile SET pin_hash = ?, pin_version = pin_version + 1, pin_failures = 0, pin_locked_until = NULL WHERE id = ?",
                        (generate_password_hash(pin), profile_id)).rowcount
    if not done:
        raise AccessError("That profile no longer exists.")


def clear_pin(conn, profile_id):
    if not conn.execute("UPDATE profile SET pin_hash = NULL, pin_version = pin_version + 1, pin_failures = 0, pin_locked_until = NULL WHERE id = ?", (profile_id,)).rowcount:
        raise AccessError("That profile no longer exists.")


def sign_out_everywhere(conn, profile_id):
    """Every device that entered this profile's PIN has to enter it again."""
    if not conn.execute("UPDATE profile SET pin_version = pin_version + 1 WHERE id = ?", (profile_id,)).rowcount:
        raise AccessError("That profile no longer exists.")


def unlock(conn, profile_id):
    conn.execute("UPDATE profile SET pin_failures = 0, pin_locked_until = NULL WHERE id = ?", (profile_id,))


def _utc(now=None):
    return now or datetime.now(timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def locked_for(row, now=None):
    """Seconds until a locked profile can be tried again (0 when it is not locked)."""
    if not row["pin_locked_until"]:
        return 0
    try:
        until = datetime.strptime(row["pin_locked_until"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max(0, int((until - _utc(now)).total_seconds()) + 1)


def lock_seconds(failures):
    """How long the next try is refused after this many wrong ones in a row: none before the fifth, then 30 s, doubling, up to 15 minutes."""
    if failures < FAILURES_BEFORE_LOCK:
        return 0
    return min(LOCK_STEP_SECONDS * 2 ** (failures - FAILURES_BEFORE_LOCK), LOCK_MAX_SECONDS)


def check_pin(conn, profile_id, pin, now=None):
    """Tries a PIN. Returns ('ok', row), ('wrong', tries_left), ('locked', seconds) or ('none', None) when the profile has no PIN or is gone.
    Wrong tries are counted in the database so restarting the app does not reset them."""
    row = conn.execute("SELECT * FROM profile WHERE id = ?", (profile_id,)).fetchone()
    if row is None or not row["pin_hash"]:
        return "none", None
    wait = locked_for(row, now)
    if wait:
        return "locked", wait
    if check_password_hash(row["pin_hash"], str(pin or "").strip()):
        conn.execute("UPDATE profile SET pin_failures = 0, pin_locked_until = NULL WHERE id = ?", (profile_id,))
        return "ok", row
    conn.execute("UPDATE profile SET pin_failures = pin_failures + 1 WHERE id = ?", (profile_id,))
    failures = conn.execute("SELECT pin_failures FROM profile WHERE id = ?", (profile_id,)).fetchone()[0]
    seconds = lock_seconds(failures)
    if seconds:
        conn.execute("UPDATE profile SET pin_locked_until = ? WHERE id = ?", (_stamp(_utc(now) + timedelta(seconds=seconds)), profile_id))
        return "locked", seconds
    return "wrong", FAILURES_BEFORE_LOCK - failures


# ---------------------------------------------------------------- remembering that a PIN was entered on this device
def session_days(conn=None):
    try:
        days = int(settings.get("access_session_days", conn=conn))
    except (TypeError, ValueError):
        return SESSION_DAYS_DEFAULT
    return days if 1 <= days <= 365 else SESSION_DAYS_DEFAULT


def _serializer(secret):
    return URLSafeTimedSerializer(secret, salt=PROOF_SALT)


def issue_proof(secret, profile_id, pin_version):
    """What the browser keeps after a right PIN: this profile, at this PIN version, signed."""
    return _serializer(secret).dumps({"p": profile_id, "v": pin_version})


def proof_valid(secret, token, profile_id, pin_version, days):
    """True when the token was signed by this app, is for this profile at its current PIN version, and is younger than `days`."""
    if not token:
        return False
    try:
        data = _serializer(secret).loads(token, max_age=days * 86400)
    except BadSignature:                                  # includes an expired signature
        return False
    return isinstance(data, dict) and data.get("p") == profile_id and data.get("v") == pin_version


def persistent_secret():
    """The key that signs cookies, kept in the database so a restart does not sign everyone out (and every worker process of a server agrees)."""
    with db.connection() as conn:
        conn.execute("INSERT OR IGNORE INTO app_secret (name, value) VALUES ('secret_key', ?)", (secrets.token_hex(32),))
        return conn.execute("SELECT value FROM app_secret WHERE name = 'secret_key'").fetchone()["value"]


# ---------------------------------------------------------------- for the Access page
def overview(conn, now=None):
    """Every profile with its PIN state and permissions, the guest's, and the plain warnings about what is left open."""
    people = []
    for row in conn.execute("SELECT * FROM profile ORDER BY name COLLATE NOCASE"):
        perms = profile_permissions(row)
        people.append({"id": row["id"], "name": row["name"], "has_pin": bool(row["pin_hash"]), "locked_for": locked_for(row, now), "failures": row["pin_failures"],
                       "all": perms is None, "permissions": list(KEYS) if perms is None else [k for k in KEYS if k in perms]})
    guest = guest_permissions(conn)
    guest_info = {"all": guest is None, "permissions": list(KEYS) if guest is None else [k for k in KEYS if k in guest]}
    warnings = []
    if "access" in guest_info["permissions"]:
        warnings.append("Guests (anyone who picks \"Just looking\") can open this page, so they can give themselves anything. Untick Access for guests.")
    for person in people:
        if not person["has_pin"] and set(person["permissions"]) - set(guest_info["permissions"]):
            extra = [LABELS[k] for k in person["permissions"] if k not in guest_info["permissions"]]
            warnings.append(f"{person['name']} has no PIN, so anyone on your network can choose this profile and open {', '.join(extra[:4])}{' and more' if len(extra) > 4 else ''}. Give it a PIN, or take those pages away.")
    return {"people": people, "guest": guest_info, "warnings": warnings, "session_days": session_days(conn)}


# ---------------------------------------------------------------- recovery from the command line
def reset(conn, name=None):
    """Takes the PIN off one profile (by name) or all of them, and gives back every permission. Returns the names that were reset."""
    rows = conn.execute("SELECT id, name FROM profile" + (" WHERE name = ? COLLATE NOCASE" if name else ""), (name,) if name else ()).fetchall()
    for row in rows:
        conn.execute("UPDATE profile SET pin_hash = NULL, pin_version = pin_version + 1, pin_failures = 0, pin_locked_until = NULL, permissions = NULL WHERE id = ?", (row["id"],))
    if not name:
        settings.set_value("access_guest_permissions", "*", conn=conn)
    return [r["name"] for r in rows]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python -m synchub.access reset-pin NAME | reset-all | list"
    if not argv or argv[0] not in ("reset-pin", "reset-all", "list") or (argv[0] == "reset-pin" and len(argv) != 2):
        print(usage)
        return 2
    db.migrate()
    with db.connection() as conn:
        if argv[0] == "list":
            for person in overview(conn)["people"]:
                print(f"{person['name']}: {'PIN set' if person['has_pin'] else 'no PIN'}, {'everything' if person['all'] else ', '.join(person['permissions']) or 'nothing'}")
            return 0
        done = reset(conn, argv[1] if argv[0] == "reset-pin" else None)
    if not done:
        print(f"There is no profile called '{argv[1]}'.")
        return 1
    print(("Removed the PIN and gave back every permission for: " if argv[0] == "reset-pin" else "Removed every PIN and gave back every permission (guests too) for: ") + ", ".join(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
