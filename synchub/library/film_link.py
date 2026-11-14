"""The saved link between a Library file and its film (`library_file.media_id`).

SyncHub still finds a film's files the same way (quality.find_movie_files: the Jellyfin path, the same folder, the name); this module saves what was found,
for one film (the film page) or for every film at once (Match & update, a Library scan, the "Refresh links" button), then recomputes the labels.
A person's choice ('manual': "not this film") is never changed by a scan.
"""
import bisect
from collections import defaultdict

from synchub import quality, storage
from synchub.library import file_labels
from synchub.library import vocab as v

AUTO, MANUAL = "auto", "manual"


def _write(conn, file_id, media_id, source):
    conn.execute("UPDATE library_file SET media_id = ?, media_link_source = ? WHERE id = ? AND (media_id IS NOT ? OR media_link_source IS NOT ?)",
                 (media_id, source, file_id, media_id, source))


def usable(rec, media_id):
    """False for a file a person said is not this film (or belongs to another one)."""
    return not (rec.get("media_link_source") == MANUAL and rec.get("media_id") != media_id)


def save_film(conn, media_id, files):
    """Saves the links of one film from the files found for it and drops auto links to files that are no longer found. Returns the files that belong."""
    keep = [f for f in files if usable(f, media_id)]
    ids = {f["id"] for f in keep}
    for f in keep:
        if f.get("media_link_source") != MANUAL:
            _write(conn, f["id"], media_id, AUTO)
    for row in conn.execute("SELECT id FROM library_file WHERE media_id = ? AND media_link_source = ?", (media_id, AUTO)).fetchall():
        if row["id"] not in ids:
            conn.execute("UPDATE library_file SET media_id = NULL, media_link_source = NULL WHERE id = ?", (row["id"],))
    file_labels.refresh(conn, [media_id])
    return keep


def not_this_film(conn, file_id):
    """A person says the file is not this film: the link is cleared and no scan sets it again."""
    conn.execute("UPDATE library_file SET media_id = NULL, media_link_source = ? WHERE id = ?", (MANUAL, file_id))


def relink_all(conn):
    """Finds the files of every film with a Jellyfin item, the same way as find_movie_files but with the Library read once, then saves the links and the labels.
    Returns {"films": films with files, "files": files linked}."""
    items = conn.execute("""SELECT l.entity_id AS media_id, t.path, m.title, m.year FROM link l JOIN src_jellyfin_item t ON t.item_id = l.source_key AND t.type = 'movie'
                            JOIN media m ON m.id = l.entity_id AND m.kind = 'movie'
                            WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND t.removed_at IS NULL AND t.path IS NOT NULL ORDER BY t.item_id""").fetchall()
    roots = storage.list_all(conn)
    by_folder = defaultdict(list)                       # (root, folder) -> rows
    by_name = []                                        # (title key of the name, id) for movies
    rows = {}
    for r in conn.execute("SELECT * FROM library_file WHERE missing_since IS NULL"):
        r = dict(r)
        rows[r["id"]] = r
        by_folder[(r["root_name"], quality.folder_of(r["relative_path"]))].append(r)
        if str(r.get("media_type") or "").startswith("Movie"):
            by_name.append((quality._title_key(quality._stem(r["relative_path"])), r["id"]))
    by_name.sort()
    keys = [k for k, _ in by_name]
    claimed = {}                                        # file id -> media id (first claim wins; a path match beats a name match)
    for item in items:
        mapped = storage.from_app_path(roots, item["path"], "jellyfin")
        if mapped is None:
            continue
        root, rel = mapped
        folder, stem = quality.folder_of(rel), quality._stem(rel)
        own_folder = bool(folder) and quality._title_key(item["title"]) and quality._title_key(folder.rsplit("/", 1)[-1]).startswith(quality._title_key(item["title"]))
        for r in by_folder.get((root, folder), ()):
            name = quality._stem(r["relative_path"])
            if v.clean_relative(r["relative_path"]).casefold() == v.clean_relative(rel).casefold() or name == stem or name.startswith(stem + " - ") or own_folder:
                claimed.setdefault(r["id"], item["media_id"])
    for item in items:
        if not item["year"]:
            continue
        wanted = quality._title_key(item["title"]) + str(item["year"])
        i = bisect.bisect_left(keys, wanted)
        while i < len(keys) and keys[i].startswith(wanted):
            claimed.setdefault(by_name[i][1], item["media_id"])
            i += 1
    linked = 0
    for fid, rec in rows.items():
        if rec.get("media_link_source") == MANUAL:
            continue
        media_id = claimed.get(fid)
        if media_id is None:
            if rec.get("media_id") is not None:
                conn.execute("UPDATE library_file SET media_id = NULL, media_link_source = NULL WHERE id = ?", (fid,))
        else:
            _write(conn, fid, media_id, AUTO)
            linked += 1
    films = file_labels.refresh(conn)
    return {"films": len({m for m in claimed.values()}), "files": linked, "with_more_than_one": films}
