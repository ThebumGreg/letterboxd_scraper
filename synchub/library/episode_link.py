"""The saved link between a Library file of a TV episode and its episode (`library_file.episode_id`; `media_id` is the show).

An episode file is found three ways, in this order: the Jellyfin item for the episode (its path, season and episode numbers), the season and episode numbers in the file name
(`S02E05`, `s2e5`; a multi-episode name links to its first episode) with the show's title, and the folder's season for a name that only has the episode. The episode must exist
in SyncHub's episode list (Refresh details on the show page fetches it). A person's choice ('manual': "not this episode") is never changed by a scan.
"""
import re
from collections import defaultdict

from synchub import quality, storage
from synchub.library import vocab as v

AUTO, MANUAL = "auto", "manual"
_SE = re.compile(r"(?<![a-z0-9])s(\d{1,3})[ ._-]*e(\d{1,4})(?!\d)", re.IGNORECASE)
_X = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])", re.IGNORECASE)


def parse_numbers(name):
    """(season, episode) from a file name, or None. `S01E02`, `s1e2`, `1x02`; the first one for a multi-episode name (S01E01-E02)."""
    stem = str(name or "")
    m = _SE.search(stem) or _X.search(stem)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _shows(conn):
    """{title key: show id}, and {show id: {(season, episode): episode id}} read once."""
    titles, episodes = {}, defaultdict(dict)
    for r in conn.execute("SELECT id, title FROM media WHERE kind = 'show'"):
        titles.setdefault(quality._title_key(r["title"]), r["id"])
    for r in conn.execute("SELECT id, media_id, season_number, episode_number FROM episode"):
        episodes[r["media_id"]][(r["season_number"], r["episode_number"])] = r["id"]
    return titles, episodes


def _jellyfin(conn, roots):
    """{(share, path casefolded): (show id, season, episode)} from the Jellyfin episode items that have a path."""
    shows = {r["source_key"]: r["entity_id"] for r in conn.execute("SELECT source_key, entity_id FROM link WHERE source = 'jellyfin' AND entity_type = 'media'")}
    out = {}
    for r in conn.execute("SELECT path, series_id, season_number, episode_number FROM src_jellyfin_item WHERE type = 'episode' AND removed_at IS NULL AND path IS NOT NULL "
                          "AND season_number IS NOT NULL AND episode_number IS NOT NULL"):
        show = shows.get(r["series_id"])
        mapped = storage.from_app_path(roots, r["path"], "jellyfin") if show else None
        if mapped:
            out[(mapped[0], mapped[1].casefold())] = (show, r["season_number"], r["episode_number"])
    return out


def relink(conn):
    """Links every episode file of the Library to its episode, drops links that no longer hold, and returns {"episodes": episode files linked}."""
    titles, episodes = _shows(conn)
    by_path = _jellyfin(conn, storage.list_all(conn))
    linked = 0
    for rec in conn.execute("SELECT id, root_name, relative_path, file_name, show_title, season_number, media_id, episode_id, media_link_source, dup_review FROM library_file "
                            "WHERE media_type = 'TV Episode' AND missing_since IS NULL").fetchall():
        if rec["media_link_source"] == MANUAL or rec["dup_review"] == "aside":
            continue
        show = season = episode = None
        hit = by_path.get((rec["root_name"], str(rec["relative_path"]).replace("\\", "/").casefold()))
        if hit:
            show, season, episode = hit
        else:
            numbers = parse_numbers(rec["file_name"])
            show = titles.get(quality._title_key(rec["show_title"])) if rec["show_title"] else None
            if numbers and show:
                season, episode = numbers
        episode_id = episodes.get(show, {}).get((season, episode)) if show else None
        if episode_id is None:
            if rec["media_id"] is not None or rec["episode_id"] is not None:
                conn.execute("UPDATE library_file SET media_id = NULL, episode_id = NULL, media_link_source = NULL WHERE id = ?", (rec["id"],))
            continue
        linked += 1
        conn.execute("UPDATE library_file SET media_id = ?, episode_id = ?, media_link_source = ? WHERE id = ? AND (media_id IS NOT ? OR episode_id IS NOT ? OR media_link_source IS NOT ?)",
                     (show, episode_id, AUTO, rec["id"], show, episode_id, AUTO))
    return {"episodes": linked}


def show_groups(conn, media_id):
    """For a show page: the episodes of the show that have more than one visible file, in order, each with its files and what each one is (the stored label).
    [{"episode_id", "season", "episode", "title", "files": [{id, name, root, path, size_bytes, resolution, label, label_text, label_tone, is_main, dup_review, ...}]}]"""
    from synchub.library import file_labels
    rows = conn.execute("SELECT f.*, e.season_number AS e_season, e.episode_number AS e_episode, e.title AS e_title FROM library_file f JOIN episode e ON e.id = f.episode_id "
                        "WHERE f.media_id = ? AND f.missing_since IS NULL AND COALESCE(f.dup_review, '') != 'aside' ORDER BY e.season_number, e.episode_number, f.id", (media_id,)).fetchall()
    groups = defaultdict(list)
    for r in rows:
        groups[r["episode_id"]].append(dict(r))
    out = []
    for files in groups.values():
        if len(files) < 2:
            continue
        main = file_labels.main_id(files, {f["id"]: f["file_label"] for f in files})
        first = files[0]
        out.append({"episode_id": first["episode_id"], "season": first["e_season"], "episode": first["e_episode"], "title": first["e_title"], "files": [
            {"id": f["id"], "name": f["file_name"], "root": f["root_name"], "path": f["relative_path"], "size_bytes": f["size_bytes"], "resolution": f["resolution"], "video_codec": f["video_codec"],
             "label": f["file_label"], "label_text": file_labels.text(f["file_label"], f) if f["file_label"] else None, "label_tone": file_labels.TONE.get(f["file_label"], "secondary"),
             "label_hint": file_labels.HINT.get(f["file_label"], ""), "is_main": f["id"] == main, "dup_review": f["dup_review"], "is_edition": bool(f["is_edition"]),
             "is_duplicate": f["file_label"] == file_labels.DUPLICATE, "is_primary_duplicate": f.get("duplicate_status") == v.DUP_PRIMARY} for f in files]})
    return out
