"""Radarr/Sonarr requests: the scheduled auto-add of wanted titles and the manual season/episode/movie requests.

Rules carried over from the old app: a title is auto-added only when enough active profiles want it, it is not in
Jellyfin (even if it was deleted from Jellyfin since: those are not re-requested) and not already in Radarr/Sonarr.
Titles whose ID is only a weak guess never get here, because they have no link until a human approves them.
"""
import logging
from typing import NamedTuple

from synchub import catalog, settings
from synchub.clients import ApiError

logger = logging.getLogger(__name__)


class RequestError(Exception):
    """A request that cannot be carried out; the message is shown to the user."""


def _int_setting(key, default):
    try:
        return max(1, int(settings.get(key)))
    except (TypeError, ValueError):
        return default


def _linked(conn, source, media_id, table, key_col, active_only=True):
    where = "AND t.removed_at IS NULL" if active_only else ""
    return conn.execute(f"""SELECT l.source_key FROM link l JOIN {table} t ON t.{key_col} = l.source_key
                            WHERE l.source = ? AND l.entity_type = 'media' AND l.entity_id = ? {where} LIMIT 1""", (source, media_id)).fetchone()


def _ever_in_jellyfin(conn, media_id):
    return conn.execute("SELECT 1 FROM link WHERE source = 'jellyfin' AND entity_type = 'media' AND entity_id = ?", (media_id,)).fetchone() is not None


# ---------------------------------------------------------------- where new titles go
class Destination(NamedTuple):
    quality_profile_id: int
    root_folder: str


def _same_path(a, b):
    norm = lambda p: str(p or "").strip().rstrip("/\\")
    a, b = norm(a), norm(b)
    windows_style = ":" in a + b or "\\" in a + b
    return a == b or (windows_style and a.lower() == b.lower())


def resolve_destination(client, quality_profile_id=None, root_folder=None):
    """The quality profile and root folder a new title would be added with, checked against the service itself.

    Both default to the saved settings. Nothing is assumed: when either is missing from the service the error says what
    is wrong and lists what the service offers, so a bad setting is one clear message instead of one failure per title."""
    label, service = client.label, client.service
    where = f"in SyncHub under Settings, {label}"
    profile_id = settings.get(f"{service}_quality_profile_id") if quality_profile_id is None else quality_profile_id
    wanted_folder = settings.get(f"{service}_root_folder") if root_folder is None else root_folder
    profiles, folders = client.quality_profiles(), client.root_folders()

    offered = ", ".join(f"{p.get('id')} = {p.get('name')}" for p in profiles) or "none"
    try:
        profile_id = int(str(profile_id).strip())
    except ValueError:
        raise RequestError(f"No {label} quality profile is chosen. Choose one {where}. {label} has: {offered}.")
    if profile_id not in {p.get("id") for p in profiles}:
        raise RequestError(f"{label} has no quality profile with ID {profile_id}. Choose one {where}. {label} has: {offered}.")

    offered = ", ".join(f.get("path", "") for f in folders) or "none"
    if not folders:
        raise RequestError(f"{label} has no root folders. Add one in {label} (Settings, Media Management, Root Folders), then choose it {where}.")
    folder = next((f for f in folders if _same_path(f.get("path"), wanted_folder)), None)
    if folder is None:
        raise RequestError(f"{label} does not have the root folder '{wanted_folder}'. Choose one {where}. {label}'s root folders are: {offered}.")
    if folder.get("accessible") is False:
        raise RequestError(f"{label} reports its root folder '{folder['path']}' as not accessible. Check that the path exists and {label} may write to it.")
    return Destination(profile_id, folder["path"])        # the path as the service spells it


# ---------------------------------------------------------------- what is wanted
def pending_movies(conn, min_users):
    wanted = catalog.demand(conn)
    out = []
    for m in conn.execute("SELECT id, title, tmdb_id FROM media WHERE kind = 'movie' AND tmdb_id IS NOT NULL ORDER BY title COLLATE NOCASE"):
        users = wanted.get(m["id"], {}).get("wanted_by", [])
        if len(users) < min_users or _ever_in_jellyfin(conn, m["id"]) or _linked(conn, "radarr", m["id"], "src_radarr_movie", "radarr_id"):
            continue
        out.append({"media_id": m["id"], "tmdb_id": m["tmdb_id"], "title": m["title"], "user_count": len(users)})
    return out


def pending_shows(conn, min_users):
    """Returns (shows, skipped_without_tvdb)."""
    wanted = catalog.demand(conn)
    out, skipped = [], []
    for m in conn.execute("SELECT id, title, tvdb_id FROM media WHERE kind = 'show' ORDER BY title COLLATE NOCASE"):
        users = wanted.get(m["id"], {}).get("wanted_by", [])
        if len(users) < min_users or _ever_in_jellyfin(conn, m["id"]) or _linked(conn, "sonarr", m["id"], "src_sonarr_series", "sonarr_id"):
            continue
        if not m["tvdb_id"]:
            skipped.append(m["title"])
            continue
        seasons = sorted({r["season_number"] for r in conn.execute(
            """SELECT e.season_number FROM src_serializd_entry e JOIN link l ON l.source = 'serializd' AND l.source_key = e.serializd_show_id
               WHERE l.entity_id = ? AND e.season_number IS NOT NULL AND e.removed_at IS NULL""", (m["id"],))})
        out.append({"media_id": m["id"], "tvdb_id": m["tvdb_id"], "title": m["title"], "user_count": len(users), "seasons": seasons or None})
    return out, skipped


# ---------------------------------------------------------------- movies
def _add_movie(client, tmdb_id, destination=None):
    destination = destination or resolve_destination(client)
    movie = client.lookup(tmdb_id)
    if movie is None:
        raise RequestError(f"TMDB {tmdb_id} was not found by Radarr.")
    client.add_movie(movie, destination.quality_profile_id, destination.root_folder)
    return movie["title"]


def _blocked(result, label, waiting, exc):
    """A setup problem stops the whole run once, with one message, instead of failing every title the same way."""
    logger.warning(f"{label} auto-add blocked ({waiting} title(s) waiting): {exc}")
    result["blocked"], result["waiting"] = str(exc), waiting
    return result


def run_radarr_additions(conn, client, dry_run=False, min_users=None):
    min_users = min_users or _int_setting("radarr_min_user_count", 1)
    result = {"added": [], "would_add": [], "errors": [], "blocked": None}
    items = pending_movies(conn, min_users)
    if not items:
        return result
    try:
        destination = resolve_destination(client)
    except (RequestError, ApiError) as exc:
        return _blocked(result, "Radarr", len(items), exc)
    for item in items:
        try:
            if dry_run:
                if client.lookup(item["tmdb_id"]) is None:
                    raise RequestError(f"TMDB {item['tmdb_id']} was not found by Radarr.")
                result["would_add"].append(item["title"])
            else:
                result["added"].append(_add_movie(client, item["tmdb_id"], destination))
        except (RequestError, ApiError) as exc:
            logger.warning(f"Radarr add failed for {item['title']}: {exc}")
            result["errors"].append(f"{item['title']}: {exc}")
    return result


def request_movie(conn, client, media_id):
    m = conn.execute("SELECT title, tmdb_id FROM media WHERE id = ? AND kind = 'movie'", (media_id,)).fetchone()
    if not m or not m["tmdb_id"]:
        raise RequestError("This movie has no TMDB ID.")
    if _linked(conn, "radarr", media_id, "src_radarr_movie", "radarr_id"):
        raise RequestError("This movie is already in Radarr.")
    try:
        return _add_movie(client, m["tmdb_id"])
    except ApiError as exc:
        raise RequestError(str(exc))


# ---------------------------------------------------------------- shows
def tvdb_seasons(conn, media_id, tmdb_seasons):
    """Sonarr numbers seasons the TVDB way; map the TMDB seasons the user picked, defaulting to the same numbers."""
    result = set()
    for n in tmdb_seasons:
        mapped = {r[0] for r in conn.execute("SELECT DISTINCT tvdb_season_number FROM episode WHERE media_id = ? AND season_number = ? AND tvdb_season_number IS NOT NULL", (media_id, n))}
        result |= mapped or {int(n)}
    return sorted(result)


def run_sonarr_additions(conn, client, dry_run=False, min_users=None):
    min_users = min_users or _int_setting("sonarr_min_user_count", 1)
    shows, skipped = pending_shows(conn, min_users)
    result = {"added": [], "would_add": [], "errors": [], "skipped_no_tvdb": skipped, "blocked": None}
    if not shows:
        return result
    try:
        destination = resolve_destination(client)
    except (RequestError, ApiError) as exc:
        return _blocked(result, "Sonarr", len(shows), exc)
    for item in shows:
        try:
            show = client.lookup(item["tvdb_id"])
            if show is None:
                raise RequestError(f"TVDB {item['tvdb_id']} was not found by Sonarr.")
            if dry_run:
                result["would_add"].append(item["title"])
                continue
            client.add_series(show, destination.quality_profile_id, destination.root_folder, monitored_seasons=item["seasons"])
            result["added"].append(item["title"])
        except (RequestError, ApiError) as exc:
            logger.warning(f"Sonarr add failed for {item['title']}: {exc}")
            result["errors"].append(f"{item['title']}: {exc}")
    return result


def _show_with_tvdb(conn, media_id):
    m = conn.execute("SELECT * FROM media WHERE id = ? AND kind = 'show'", (media_id,)).fetchone()
    if not m:
        raise RequestError("Show not found.")
    if not m["tvdb_id"]:
        raise RequestError("Sonarr needs a TVDB ID and this show has none yet. Refresh its metadata or link it in the Resolution Center.")
    return m


def _add_to_sonarr(conn, client, m, seasons, search):
    destination = resolve_destination(client)
    show = client.lookup(m["tvdb_id"])
    if show is None:
        raise RequestError(f"TVDB {m['tvdb_id']} was not found by Sonarr.")
    client.add_series(show, destination.quality_profile_id, destination.root_folder, monitored_seasons=seasons, search=search)


def request_seasons(conn, client, media_id, seasons):
    """Monitors the chosen seasons (TMDB numbering) and starts a search for each. Other seasons are left as they are."""
    m = _show_with_tvdb(conn, media_id)
    try:
        wanted = tvdb_seasons(conn, media_id, [int(s) for s in seasons])
    except (TypeError, ValueError):
        raise RequestError("Season numbers must be whole numbers.")
    if not wanted:
        raise RequestError("No seasons selected.")
    try:
        existing = _linked(conn, "sonarr", media_id, "src_sonarr_series", "sonarr_id")
        if not existing:
            _add_to_sonarr(conn, client, m, wanted, search=True)
            return {"status": "added", "seasons": wanted, "search_errors": []}
        info = client.get_series(existing["source_key"])
        for s in info.get("seasons", []):
            if s.get("seasonNumber") in wanted:
                s["monitored"] = True
        info["monitored"] = True
        client.update_series(existing["source_key"], info)
        errors = []
        for season in wanted:
            try:
                client.search_season(existing["source_key"], season)
            except ApiError as exc:
                errors.append(f"S{season}: {exc}")
        return {"status": "updated", "seasons": wanted, "search_errors": errors}
    except ApiError as exc:
        raise RequestError(str(exc))


def request_episodes(conn, client, media_id, episode_ids, resync=None):
    """Monitors and searches individual episodes (by our episode IDs). `resync` refreshes Sonarr data after adding a series."""
    m = _show_with_tvdb(conn, media_id)
    ids = [int(i) for i in episode_ids]
    if not ids:
        raise RequestError("No episodes selected.")

    def sonarr_ids():
        found, missing = [], []
        for eid in ids:
            row = conn.execute("""SELECT l.source_key FROM link l JOIN src_sonarr_episode s ON s.sonarr_episode_id = l.source_key
                                  WHERE l.source = 'sonarr_episode' AND l.entity_type = 'episode' AND l.entity_id = ? AND s.removed_at IS NULL""", (eid,)).fetchone()
            (found if row else missing).append(row["source_key"] if row else eid)
        return found, missing

    try:
        found, missing = sonarr_ids()
        if missing and not _linked(conn, "sonarr", media_id, "src_sonarr_series", "sonarr_id"):
            _add_to_sonarr(conn, client, m, [], search=False)          # added with nothing monitored; the episodes below are then switched on
            if resync:
                resync()
                found, missing = sonarr_ids()
        if missing:
            raise RequestError(f"{len(missing)} episode(s) are not known to Sonarr yet (numbering may differ). Sync Sonarr and try again.")
        client.monitor_episodes(found, True)
        client.search_episodes(found)
        return {"status": "updated", "episodes": len(found)}
    except ApiError as exc:
        raise RequestError(str(exc))


def request_all(conn, client, media_id, today=None):
    """Every regular season that has an aired episode not yet in the library (Specials are not included)."""
    from datetime import date
    today = today or date.today().isoformat()
    seasons = [r[0] for r in conn.execute(
        """SELECT DISTINCT e.season_number FROM episode e LEFT JOIN item_state s ON s.target_type = 'episode' AND s.target_id = e.id
           WHERE e.media_id = ? AND e.season_number > 0 AND e.air_date IS NOT NULL AND e.air_date <= ? AND COALESCE(s.state_code, 'missing') <> 'in_library'
           ORDER BY 1""", (media_id, today))]
    if not seasons:
        raise RequestError("Nothing missing: every aired episode is already in the library.")
    return request_seasons(conn, client, media_id, seasons)
