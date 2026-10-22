"""Exact-duplicate detection across every share: same size, then (cheaply) same first and last megabyte, then the same SHA-256.

A worker reads the files (a hash job); this module decides which files need reading, stores what comes back and forms the groups. Nothing is
ever deleted because of a duplicate: one copy is the "primary candidate" (a person decides), the others are suggested for skipping.
"""
from collections import defaultdict

from synchub import db
from synchub.library import findings, store
from synchub.library import vocab as v

PARTIAL_READ_BYTES = 1024 * 1024
MAX_FILES_PER_JOB = 200


def candidate_groups(conn):
    """[(size, [records])] for every size shared by two or more readable files that are still there."""
    sizes = [r["size_bytes"] for r in conn.execute(
        "SELECT size_bytes FROM library_file WHERE missing_since IS NULL AND unreadable_reason IS NULL AND size_bytes > 0 "
        "GROUP BY size_bytes HAVING COUNT(*) > 1 ORDER BY size_bytes")]
    groups = []
    for size in sizes:
        members = [dict(r) for r in conn.execute("SELECT * FROM library_file WHERE size_bytes = ? AND missing_since IS NULL AND unreadable_reason IS NULL ORDER BY id", (size,))]
        groups.append((size, members))
    return groups


def needs_reading(members):
    return [m for m in members if not m["sha256"] and m["duplicate_status"] != v.DUP_RULED_OUT]


def plan_jobs(conn):
    """The hash jobs a search needs: the files whose content has not been read yet, grouped by size, a few size groups per job.
    Returns a list of job plans (empty when everything is already known)."""
    plans, batch, files = [], [], 0
    for size, members in candidate_groups(conn):
        need = needs_reading(members)
        if not need:
            continue
        entry = {"size": size, "partial_only": False, "files": [{"id": m["id"], "root": m["root_name"], "rel": m["relative_path"], "size": m["size_bytes"], "mtime": m["mtime"]}
                                                           for m in need], "known": sum(1 for m in members if m["sha256"])}
        if batch and files + len(entry["files"]) > MAX_FILES_PER_JOB:
            plans.append({"op": "hash", "groups": batch})
            batch, files = [], 0
        batch.append(entry)
        files += len(entry["files"])
    if batch:
        plans.append({"op": "hash", "groups": batch})
    return plans


def apply_results(conn, results, now=None):
    """Stores what a worker read. Each result echoes the file's size and modified time: a file that changed since the job was planned is ignored.
    Returns how many were stored."""
    stored = 0
    store.begin(conn)
    for res in results or []:
        row = conn.execute("SELECT id, size_bytes, mtime, sha256 FROM library_file WHERE id = ?", (res.get("id"),)).fetchone()
        if row is None or row["size_bytes"] != res.get("size") or abs(row["mtime"] - float(res.get("mtime") or 0)) > v.MTIME_TOLERANCE_SECONDS:
            continue
        if res.get("sha256"):
            conn.execute("UPDATE library_file SET sha256 = ?, partial_signature = COALESCE(?, partial_signature) WHERE id = ?", (res["sha256"], res.get("partial"), row["id"]))
        elif res.get("ruled_out"):
            conn.execute("UPDATE library_file SET duplicate_status = ?, partial_signature = COALESCE(?, partial_signature) WHERE id = ? AND sha256 IS NULL",
                         (v.DUP_RULED_OUT, res.get("partial"), row["id"]))
        elif res.get("error"):
            conn.execute("UPDATE library_file SET duplicate_status = ? WHERE id = ? AND sha256 IS NULL", (v.DUP_INCOMPLETE, row["id"]))
        else:
            continue
        stored += 1
    return stored


def choose_primary(members, cfg):
    """The copy to keep. One already in the right place in the destination share wins; then any copy in the destination share; then the first by path.
    (The original tool simply took the first path.)"""
    def rank(m):
        return (0 if findings.in_place(m, cfg) else 1 if m["root_name"] == cfg.destination_root else 2, m["root_name"], m["relative_path"].casefold())
    return min(members, key=rank)


def regroup(conn, cfg, now=None):
    """Forms duplicate groups from the hashes known so far and updates every record's duplicate fields. Returns the number of groups."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM library_file WHERE sha256 IS NOT NULL AND missing_since IS NULL AND unreadable_reason IS NULL")]
    by_hash = defaultdict(list)
    for rec in rows:
        by_hash[rec["sha256"]].append(rec)
    store.begin(conn)
    groups = 0
    for digest, members in by_hash.items():
        if len(members) > 1:
            groups += 1
            group_id = f"DUP-{digest[:12]}"        # stable: the same identical files keep the same id on every run
            primary = choose_primary(members, cfg)
            for m in members:
                _set_duplicate(conn, m, group_id, v.DUP_PRIMARY if m is primary else v.DUP_OTHER)
        else:
            _set_duplicate(conn, members[0], None, v.DUP_NONE)
    # a file with a stale group (its copies are gone or changed) and no hash any more
    for r in conn.execute("SELECT id FROM library_file WHERE sha256 IS NULL AND duplicate_group IS NOT NULL"):
        conn.execute("UPDATE library_file SET duplicate_group = NULL, duplicate_status = ? WHERE id = ?", (v.DUP_NOT_CHECKED, r["id"]))
    return groups


def _set_duplicate(conn, rec, group, status):
    if rec["duplicate_group"] != group or rec["duplicate_status"] != status:
        conn.execute("UPDATE library_file SET duplicate_group = ?, duplicate_status = ? WHERE id = ?", (group, status, rec["id"]))


def finish(conn, cfg, run_id=None, now=None):
    """After hash results arrive: form the groups, then recompute every finding that depends on them. Returns (groups, records changed)."""
    groups = regroup(conn, cfg, now)
    return groups, findings.refresh(conn, cfg, run_id, now)
