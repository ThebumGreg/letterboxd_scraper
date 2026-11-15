"""Which Library record a job belongs to, and which jobs belong to a record.

Jobs point at their file in different ways (the record itself, a quality check, a preview clip, a Radarr/Sonarr download, an extra copy), so the pages that
link a job to its file's report, and the report back to its jobs, ask here instead of each knowing every kind."""

# job.ref_type values whose ref_id is the library_file id itself
DIRECT = ("library_file", "library_hide", "library_source", "library_aside")
# job.ref_type -> the SQL that turns its ref_id into a library_file id
LOOKUPS = {
    "quality_check": "SELECT library_file_id FROM quality_check WHERE id = ?",
    "quality_ai_test": "SELECT library_file_id FROM quality_ai_test WHERE id = ?",
    "compression_test": "SELECT library_file_id FROM compression_test WHERE id = ?",
    "download_import": "SELECT record_id FROM download_import WHERE id = ?",
    "preview": """SELECT COALESCE(p.record_id, CASE WHEN p.subject_type = 'library_file' THEN p.subject_id END,
                                  (SELECT d.record_id FROM download_import d WHERE p.subject_type = 'download_import' AND d.id = p.subject_id))
                  FROM preview p WHERE p.id = ?""",
    "library_action": "SELECT f.id FROM library_action a JOIN library_file f ON f.path_key = a.path_key WHERE a.id = ?",
}


def record_for_job(conn, job):
    """The id of the Library record a job is about, or None for a job that has no single file (a scan, a test copy, a worker's own work) or whose record is gone."""
    kind, ref = job.get("ref_type"), job.get("ref_id")
    if not kind or ref is None:
        return None
    if kind in DIRECT:
        found = ref
    elif kind in LOOKUPS:
        row = conn.execute(LOOKUPS[kind], (ref,)).fetchone()
        found = row[0] if row else None
    else:
        return None
    if found is None:
        return None
    return found if conn.execute("SELECT 1 FROM library_file WHERE id = ?", (found,)).fetchone() else None


def jobs_for_record(conn, record_id, limit=25):
    """The jobs made for one record, newest first: conversions, hides, original deletions, quality checks, previews, downloads and extra copies."""
    refs = {kind: [record_id] for kind in DIRECT}
    one = (record_id,)
    refs["quality_check"] = [r[0] for r in conn.execute("SELECT id FROM quality_check WHERE library_file_id = ?", one)]
    refs["quality_ai_test"] = [r[0] for r in conn.execute("SELECT id FROM quality_ai_test WHERE library_file_id = ?", one)]
    refs["compression_test"] = [r[0] for r in conn.execute("SELECT id FROM compression_test WHERE library_file_id = ?", one)]
    refs["download_import"] = [r[0] for r in conn.execute("SELECT id FROM download_import WHERE record_id = ?", one)]
    refs["preview"] = [r[0] for r in conn.execute("SELECT id FROM preview WHERE record_id = ? OR (subject_type = 'library_file' AND subject_id = ?)", (record_id, record_id))]
    refs["preview"] += [r[0] for r in conn.execute("SELECT p.id FROM preview p JOIN download_import d ON d.id = p.subject_id WHERE p.subject_type = 'download_import' AND d.record_id = ?", one)]
    refs["library_action"] = [r[0] for r in conn.execute("SELECT a.id FROM library_action a JOIN library_file f ON f.path_key = a.path_key WHERE f.id = ?", one)]
    found = {}
    for kind, ids in refs.items():
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            for r in conn.execute(f"SELECT id, kind, title, status, created_at, finished_at FROM job WHERE ref_type = ? AND ref_id IN ({','.join('?' * len(chunk))})", [kind, *chunk]):
                found[r["id"]] = dict(r)
    return [found[i] for i in sorted(found, reverse=True)[:limit]]
