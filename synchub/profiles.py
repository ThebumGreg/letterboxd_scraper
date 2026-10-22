"""Profiles: one person, with at most one Letterboxd, Serializd and Jellyfin identity each (enforced by the database)."""
import sqlite3

_COLUMN_LABELS = {"name": "profile name", "letterboxd_username": "Letterboxd username", "serializd_username": "Serializd username", "jellyfin_user_id": "Jellyfin user"}


class ProfileError(Exception):
    """A change that cannot be saved; the message is meant for the user."""


def _blank_to_none(value):
    value = (value or "").strip()
    return value or None


def list_all(conn):
    """Every profile. The PIN itself is never included, only whether there is one."""
    return [dict(r) for r in conn.execute("""SELECT id, name, letterboxd_username, serializd_username, jellyfin_user_id, is_active, created_at,
                                                    pin_hash IS NOT NULL AS has_pin FROM profile ORDER BY name COLLATE NOCASE""")]


def _owner_of(conn, column, value, exclude_id):
    row = conn.execute(f"SELECT name FROM profile WHERE {column} = ? AND id <> ?", (value, exclude_id or 0)).fetchone()
    return row["name"] if row else None


def save(conn, profile_id, name, letterboxd_username=None, serializd_username=None, jellyfin_user_id=None, only_given=False):
    """Creates (profile_id None) or updates a profile. Returns its ID.

    With only_given=True, identities passed as None keep their stored value (used by the Jellyfin import, which only knows a name and an ID).
    A blank string clears an identity. An identity already used by another profile is refused with a clear message.
    """
    name = (name or "").strip()
    if not name:
        raise ProfileError("A profile name is required.")
    given = {"letterboxd_username": letterboxd_username, "serializd_username": serializd_username, "jellyfin_user_id": jellyfin_user_id}
    values = {k: _blank_to_none(v) for k, v in given.items() if not (only_given and v is None)}
    for column, value in {"name": name, **values}.items():
        if value and (owner := _owner_of(conn, column, value, profile_id)):
            raise ProfileError(f"The {_COLUMN_LABELS[column]} '{value}' already belongs to the profile '{owner}'.")
    try:
        if profile_id is None:
            cur = conn.execute("INSERT INTO profile (name, letterboxd_username, serializd_username, jellyfin_user_id) VALUES (?, ?, ?, ?)",
                               (name, values.get("letterboxd_username"), values.get("serializd_username"), values.get("jellyfin_user_id")))
            return cur.lastrowid
        if not conn.execute("SELECT 1 FROM profile WHERE id = ?", (profile_id,)).fetchone():
            raise ProfileError("That profile no longer exists.")
        changes = {"name": name, **values}
        conn.execute(f"UPDATE profile SET {', '.join(f'{c} = ?' for c in changes)} WHERE id = ?", (*changes.values(), profile_id))
        return profile_id
    except sqlite3.IntegrityError as exc:       # a race with another request; the pre-checks above cover the normal case
        raise ProfileError(f"That profile conflicts with an existing one ({exc}).")


def delete(conn, profile_id):
    """Removes the profile only. Letterboxd/Serializd entries are kept and re-attach if the profile is recreated."""
    from synchub import access
    try:
        access.ensure_profile_can_go(conn, profile_id)
    except access.AccessError as exc:
        raise ProfileError(str(exc))
    return conn.execute("DELETE FROM profile WHERE id = ?", (profile_id,)).rowcount == 1


def set_active(conn, profile_id, active):
    return conn.execute("UPDATE profile SET is_active = ? WHERE id = ?", (1 if active else 0, profile_id)).rowcount == 1
