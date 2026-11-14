"""What each file of a film is to the film: one definition used by Home, the Library, the film page and the record page.

Labels (a film with only one file has none):
  edition      a person marked it as a different edition (a hand choice always wins)
  converted    the file SyncHub made by converting or copying another file of the film
  original     the file a converted copy was made from (it may be hidden from Jellyfin)
  mobile       a smaller extra copy next to the film (`Film (Year) - 1080p.mkv`)
  duplicate    the very same content as another file (same hash); one of the group is the "keep this one" candidate
  unexplained  more than one file and none of the above

The saved film-to-file link (`library_file.media_id`) is filled when a film's files are found (the film page, Match & update, and a Library scan) so that the
labels and counts never need to guess again. A link a person set or removed (`media_link_source` = 'manual') is never changed by a scan.
"""
import os
from collections import defaultdict

from synchub import versions
from synchub.library import vocab as v

EDITION, CONVERTED, ORIGINAL, MOBILE, DUPLICATE, UNEXPLAINED = "edition", "converted", "original", "mobile", "duplicate", "unexplained"
ORDER = (MOBILE, CONVERTED, ORIGINAL, DUPLICATE, EDITION, UNEXPLAINED)
TEXT = {EDITION: "Different edition", CONVERTED: "Converted copy", ORIGINAL: "Original", MOBILE: "Mobile copy", DUPLICATE: "Duplicate file", UNEXPLAINED: "Unexplained"}
TONE = {EDITION: "primary", CONVERTED: "success", ORIGINAL: "secondary", MOBILE: "info", DUPLICATE: "warning", UNEXPLAINED: "danger"}
HINT = {
    EDITION: "A different cut or release of the same film that you marked.",
    CONVERTED: "The file SyncHub made by converting or copying another file of this film.",
    ORIGINAL: "The file a converted copy was made from. It may be hidden from Jellyfin or renamed .synchub-original.",
    MOBILE: "A smaller extra copy (for phones and remote viewing) kept next to the film.",
    DUPLICATE: "Exactly the same content as another file. Keep one, set the other aside.",
    UNEXPLAINED: "Another file of this film that SyncHub cannot explain. Mark it as an edition, set it aside, or say it is not this film.",
}


def text(label, rec=None):
    """The words on the badge. A duplicate says whether it is the one to keep, or that you chose to keep both."""
    if label == DUPLICATE and rec is not None:
        if rec.get("dup_review") == "keep_both":
            return "Duplicate · keeping both"
        if rec.get("dup_review") == "aside":
            return "Duplicate · set aside"
        if rec.get("duplicate_status") == v.DUP_PRIMARY:
            return "Duplicate · keep this one"
    if label == EDITION and rec is not None and rec.get("edition_name"):
        return f"Edition: {rec['edition_name']}"
    return TEXT.get(label, "")


def _key(root, path):
    return f"{root}:{v.clean_relative(path)}".casefold()


def _dest_key(dest):
    root, _, rel = str(dest or "").partition(":")
    return _key(root, rel) if rel else ""


def classify(files):
    """{file id: label} for the files of ONE film (dicts with library_file columns). Fewer than two files give no labels."""
    files = list(files)
    if len(files) < 2:
        return {f["id"]: None for f in files}
    here = {_key(f["root_name"], f["relative_path"]) for f in files}
    made = {_dest_key(f.get("migration_destination_path")) for f in files
            if f.get("migration_status") == v.MIGRATION_COMPLETED and f.get("migrated_how") in ("copy", "convert")}
    out = {}
    for f in files:
        dest = str(f.get("migration_destination_path") or "")
        if f.get("is_edition"):
            out[f["id"]] = EDITION
        elif (f.get("migration_status") == v.MIGRATION_COMPLETED and f.get("migrated_how") in ("copy", "convert") and ":" in dest
              and dest.split(":", 1)[0] != f.get("root_name") and _dest_key(dest) in here):
            out[f["id"]] = ORIGINAL
        elif _key(f["root_name"], f["relative_path"]) in made:
            out[f["id"]] = CONVERTED
        elif versions.is_version_file(f.get("file_name") or os.path.basename(f["relative_path"])):
            out[f["id"]] = MOBILE
        elif f.get("duplicate_group"):
            out[f["id"]] = DUPLICATE
        else:
            out[f["id"]] = UNEXPLAINED
    return out


def _set(conn, rec_id, **values):
    """Writes only what changed, so a refresh that changes nothing leaves the Library's change counter alone."""
    names = list(values)
    where = " OR ".join(f"{n} IS NOT ?" for n in names)
    conn.execute(f"UPDATE library_file SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ? AND ({where})", [*values.values(), rec_id, *values.values()])


def refresh(conn, media_ids=None):
    """Recomputes the label of every file that has a saved film link (all films, or just `media_ids`). Returns how many films have more than one file."""
    rows = defaultdict(list)
    sql = "SELECT * FROM library_file WHERE media_id IS NOT NULL AND missing_since IS NULL"
    args = []
    if media_ids is not None:
        media_ids = list(media_ids)
        if not media_ids:
            return 0
        sql += f" AND media_id IN ({','.join('?' * len(media_ids))})"
        args = media_ids
    for r in conn.execute(sql, args):
        rows[r["media_id"]].append(dict(r))
    many = 0
    for files in rows.values():
        labels = classify(files)
        many += len(files) > 1
        for f in files:
            _set(conn, f["id"], file_label=labels[f["id"]])
    # files that are gone or lost their film keep no label
    conn.execute("UPDATE library_file SET file_label = NULL WHERE file_label IS NOT NULL AND (missing_since IS NOT NULL OR media_id IS NULL)")
    return many


def counts(conn):
    """For Home and the Library header: {"films": films with more than one file, "files": {label: n}, "different_shares": films whose files sit on more than one share,
    "unlinked": Library movie files with no film, "saved_bytes": space saved by conversions}. Reads what refresh() stored."""
    files = {r["file_label"]: r["n"] for r in conn.execute("SELECT file_label, COUNT(*) AS n FROM library_file WHERE file_label IS NOT NULL AND missing_since IS NULL GROUP BY file_label")}
    films = conn.execute("SELECT COUNT(*) AS n FROM (SELECT media_id FROM library_file WHERE media_id IS NOT NULL AND missing_since IS NULL GROUP BY media_id HAVING COUNT(*) > 1)").fetchone()["n"]
    shares = conn.execute("SELECT COUNT(*) AS n FROM (SELECT media_id FROM library_file WHERE media_id IS NOT NULL AND missing_since IS NULL GROUP BY media_id "
                          "HAVING COUNT(*) > 1 AND COUNT(DISTINCT root_name) > 1)").fetchone()["n"]
    unlinked = conn.execute("SELECT COUNT(*) AS n FROM library_file WHERE media_id IS NULL AND missing_since IS NULL AND media_type LIKE 'Movie%'").fetchone()["n"]
    return {"films": films, "files": {k: files.get(k, 0) for k in ORDER}, "different_shares": shares, "unlinked": unlinked, "saved_bytes": saved_bytes(conn)}


def saved_bytes(conn):
    """Space saved by converted copies: the size of the original minus the size of the copy, for pairs where both files are still known."""
    row = conn.execute("""SELECT COALESCE(SUM(o.size_bytes - c.size_bytes), 0) AS n FROM library_file o
                          JOIN library_file c ON lower(c.root_name || ':' || c.relative_path) = lower(o.migration_destination_path)
                          WHERE o.migration_status = ? AND o.migrated_how = 'convert' AND c.missing_since IS NULL AND o.size_bytes > c.size_bytes""", (v.MIGRATION_COMPLETED,)).fetchone()
    return int(row["n"] or 0)


def saved_for(rec, files):
    """Bytes saved by `rec` (a converted copy) against the file it was made from, among the film's `files`; None when unknown."""
    here = _key(rec["root_name"], rec["relative_path"])
    for f in files:
        if f["id"] != rec["id"] and f.get("migration_status") == v.MIGRATION_COMPLETED and f.get("migrated_how") == "convert" \
                and _dest_key(f.get("migration_destination_path")) == here and (f.get("size_bytes") or 0) > (rec.get("size_bytes") or 0):
            return f["size_bytes"] - rec["size_bytes"]
    return None
