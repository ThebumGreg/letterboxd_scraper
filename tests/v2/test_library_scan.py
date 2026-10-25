import json

import pytest

from synchub import jobs, settings, storage, workers
from synchub.library import duplicates, scan, store
from synchub.library import vocab as v

from .library_helpers import CFG, add, analysis, finish
from .test_library_findings import rec_of


class Lib:
    """A SyncHub with the two shares and one worker, to drive the scan protocol the way an agent does."""

    def __init__(self, conn):
        self.conn = conn
        for name, host, share in (("media", "tower", "Media"), ("tfiles", "tower", "TFiles")):
            storage.create(conn, {"name": name, "protocol": "smb", "host": host, "share": share, "mount_point": f"/mnt/tower/{share}"})
        self.worker_id, _ = workers.create(conn, "w1", slots=2)
        conn.commit()

    def start(self, **kw):
        run_id = scan.start_scan(self.conn, kw.pop("root", "tfiles"), **kw)
        self.conn.commit()
        return run_id

    def claim(self):
        worker = workers.get(self.conn, self.worker_id)
        claimed = jobs.claim(self.conn, worker, 2)
        self.conn.commit()
        return [jobs.get(self.conn, c["id"]) for c in claimed]

    def scan_job(self, **kw):
        self.start(**kw)
        return self.claim()[0]

    def file(self, rel, size=1000, mtime=1_700_000_000.0):
        return {"rel": rel, "size": size, "mtime": mtime}

    def report(self, job, rel, **kw):
        """What a worker reports for one analysed file."""
        a = analysis(root=job["plan"]["root"], rel=rel, name=kw.pop("name", rel.rsplit("/", 1)[-1]), **kw)
        rec = {k: a.get(k) for k in scan.AGENT_FIELDS}
        rec.update(rel=rel, file_name=a["file_name"], size_bytes=a["size_bytes"], mtime=a["mtime"], logic_version=a["analysis_logic_version"], findings=json.loads(a["findings_json"]))
        return rec


@pytest.fixture
def lib(conn):
    return Lib(conn)


def call(lib, fn, *args, **kw):
    out = fn(lib.conn, *args, **kw)
    lib.conn.commit()
    return out


# ---------------------------------------------------------------- starting a scan
def test_a_scan_queues_one_job_on_the_share_with_the_settings_in_its_plan(lib):
    run_id = lib.start(prefix="TV Shows", by="ann")
    run = scan.list_runs(lib.conn)[0]
    assert run["id"] == run_id and run["status"] == "queued" and run["kind"] == "scan" and run["root_name"] == "tfiles" and run["prefix"] == "TV Shows" and run["started_by"] == "ann"
    (job,) = scan.run_jobs(lib.conn, run_id)
    assert job["kind"] == "scan" and (job["source_root"], job["source_rel"]) == ("tfiles", "TV Shows") and job["title"] == "Scan tfiles/TV Shows"
    plan = job["plan"]
    assert plan["op"] == "scan" and plan["run_id"] == run_id and plan["integrity"] == "tail" and plan["tail_seconds"] == 30 and plan["force"] is False and plan["logic_version"] == v.ANALYSIS_LOGIC_VERSION
    assert "@eaDir" in plan["exclude"] and ".Trash-*" in plan["exclude"]


def test_a_scan_uses_the_libraries_settings_unless_told_otherwise(lib):
    settings.set_value("library_integrity_mode", "full", conn=lib.conn)
    settings.set_value("library_tail_seconds", "45", conn=lib.conn)
    settings.set_value("library_exclude_folders", "Extras\nSample", conn=lib.conn)
    lib.conn.commit()
    plan = scan.run_jobs(lib.conn, lib.start())[0]["plan"]
    assert (plan["integrity"], plan["tail_seconds"], plan["exclude"]) == ("full", 45, ["Extras", "Sample"])
    other = scan.run_jobs(lib.conn, lib.start(root="media", integrity="metadata", exclude="a\nb", force=True))[0]["plan"]
    assert (other["integrity"], other["exclude"], other["force"]) == ("metadata", ["a", "b"], True)


@pytest.mark.parametrize("kw, fragment", [({"root": "nowhere"}, "no share called"), ({"prefix": "../etc"}, "inside the share"), ({"integrity": "sometimes"}, "metadata, tail or full")])
def test_a_scan_that_cannot_run_says_why_and_queues_nothing(lib, kw, fragment):
    with pytest.raises(v.LibraryError, match=fragment):
        lib.start(**kw)
    assert scan.list_runs(lib.conn) == [] and lib.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


def test_two_scans_of_overlapping_places_cannot_run_at_once_but_separate_ones_can(lib):
    lib.start(prefix="TV Shows")
    for root, prefix in (("tfiles", ""), ("tfiles", "TV Shows"), ("tfiles", "TV Shows/Show")):
        with pytest.raises(v.LibraryError, match="already queued"):
            lib.start(root=root, prefix=prefix)
    lib.start(root="tfiles", prefix="Movies")
    lib.start(root="media")
    assert len(scan.list_runs(lib.conn)) == 3


def test_the_state_of_a_run_follows_its_job(lib):
    run_id = lib.start()
    (job,) = lib.claim()
    get = lambda: scan.describe_run(lib.conn, lib.conn.execute("SELECT * FROM library_run WHERE id = ?", (run_id,)).fetchone())      # noqa: E731
    assert get()["status"] == "running" and get()["active"]
    jobs.progress(lib.conn, job["id"], lib.worker_id, stage="scan", progress=0.4, message="Analysing 4 of 10")
    lib.conn.commit()
    assert get()["progress"] == pytest.approx(0.4) and get()["message"] == "Analysing 4 of 10"
    jobs.complete(lib.conn, job["id"], lib.worker_id, {})
    lib.conn.commit()
    assert get()["status"] == "completed" and not get()["active"] and get()["progress"] == 1.0
    scan.bump(lib.conn, run_id, errors=2)
    assert get()["status"] == "completed_with_errors"


def test_a_failed_or_cancelled_job_is_a_failed_or_cancelled_run_and_frees_the_share(lib):
    run_id = lib.start()
    (job,) = lib.claim()
    jobs.fail(lib.conn, job["id"], lib.worker_id, "bad_job", "no such folder", retryable=False)
    lib.conn.commit()
    run = scan.list_runs(lib.conn)[0]
    assert run["status"] == "failed" and run["error"] == "no such folder"
    second = lib.start()                                                                  # a failed scan does not block the next one
    jobs.cancel(lib.conn, scan.run_jobs(lib.conn, second)[0]["id"])
    lib.conn.commit()
    assert [r["status"] for r in scan.list_runs(lib.conn)] == ["cancelled", "failed"] and run_id != second


# ---------------------------------------------------------------- the inventory
def test_the_inventory_asks_only_for_files_that_are_new_or_need_another_look(lib):
    old = analysis(root="tfiles", rel="Movies/Old.mkv", name="Old (2000).mkv", logic=0)
    unchanged = finish(analysis(root="tfiles", rel="Movies/Same.mkv", name="Same (2000).mkv", logic=v.ANALYSIS_LOGIC_VERSION))
    changed = finish(analysis(root="tfiles", rel="Movies/Changed.mkv", name="Changed (2000).mkv", logic=v.ANALYSIS_LOGIC_VERSION))
    unfinished = finish(analysis(root="tfiles", rel="Movies/Unfinished.mkv", name="U (2000).mkv", logic=v.ANALYSIS_LOGIC_VERSION, status=v.ANALYSIS_INCOMPLETE, integrity=v.INT_TIMEOUT, action="manual_review"))
    store.upsert_records(lib.conn, [finish(old), unchanged, changed, unfinished], "tfiles:", "run-0")
    lib.conn.commit()
    job = lib.scan_job()
    files = [lib.file("Movies/Same.mkv", 1_000_000), lib.file("Movies/Changed.mkv", 2_000_000), lib.file("Movies/Old.mkv", 1_000_000), lib.file("Movies/Unfinished.mkv", 1_000_000), lib.file("Movies/Brand New.mkv", 5)]
    need = call(lib, scan.inventory, job, files)
    assert sorted(need) == ["Movies/Brand New.mkv", "Movies/Changed.mkv", "Movies/Old.mkv", "Movies/Unfinished.mkv"]
    same = store.by_key(lib.conn, v.record_key("tfiles", "Movies/Same.mkv"))
    assert same["last_seen_run_id"] == job["plan"]["run_id"] and same["source_revision"] == 1
    counts = scan.list_runs(lib.conn)[0]["counts"]
    assert (counts["found"], counts["need"], counts["unchanged"], counts["new"]) == (5, 4, 1, 1)


def test_force_and_a_different_integrity_check_make_every_file_need_a_new_look(lib):
    store.upsert_records(lib.conn, [finish(analysis(root="tfiles", rel="a.mkv", name="A.mkv", logic=v.ANALYSIS_LOGIC_VERSION))], "tfiles:", "run-0")
    lib.conn.commit()
    files = [lib.file("a.mkv", 1_000_000)]

    def end(job):
        jobs.complete(lib.conn, job["id"], lib.worker_id, {})
        lib.conn.commit()
    first = lib.scan_job()
    assert call(lib, scan.inventory, first, files) == []
    end(first)
    forced = lib.scan_job(force=True)
    assert call(lib, scan.inventory, forced, files) == ["a.mkv"]
    end(forced)
    assert call(lib, scan.inventory, lib.scan_job(integrity="full"), files) == ["a.mkv"]                     # tail was checked before, full was asked for


def test_a_file_in_the_middle_of_a_migration_is_only_noted_as_seen(lib):
    record_id = add(lib.conn, rel="a.mkv", name="A.mkv", logic=v.ANALYSIS_LOGIC_VERSION)
    lib.conn.execute("UPDATE library_file SET migration_status = 'in_progress' WHERE id = ?", (record_id,))
    lib.conn.commit()
    job = lib.scan_job()
    assert call(lib, scan.inventory, job, [lib.file("a.mkv", 99)]) == []                                  # even though its size differs
    assert rec_of(lib.conn, record_id)["last_seen_run_id"] == job["plan"]["run_id"] and rec_of(lib.conn, record_id)["size_bytes"] == 1_000_000


def test_a_file_that_could_not_be_read_is_kept_as_a_record_that_cannot_be_approved(lib):
    job = lib.scan_job()
    call(lib, scan.inventory, job, [], unreadable=[{"rel": "Movies/Bad.mkv", "file_name": "Bad.mkv", "unreadable_reason": "stat failed: I/O error"}])
    rec = store.by_key(lib.conn, v.record_key("tfiles", "Movies/Bad.mkv"))
    assert rec["unreadable_reason"] == "stat failed: I/O error" and rec["analysis_status"] == "failed" and rec["size_bytes"] == 0 and rec["source_path"] == "//tower/TFiles/Movies/Bad.mkv"
    assert rec["review_status"] == "manual_review" and rec["recommended_action"] == "manual_review"
    with pytest.raises(ValueError):
        store.set_approval(lib.conn, rec["id"], v.APPROVAL_APPROVED, "ann", mark_reviewed=True)
    assert scan.list_runs(lib.conn)[0]["counts"]["unreadable"] == 1


@pytest.mark.parametrize("files", [[{"rel": "../x.mkv", "size": 1, "mtime": 1}], [{"rel": "a.mkv", "size": "big", "mtime": 1}], [{"size": 1, "mtime": 1}], [{"rel": "", "size": 1, "mtime": 1}]])
def test_a_malformed_inventory_is_refused(lib, files):
    with pytest.raises(jobs.JobError):
        scan.inventory(lib.conn, lib.scan_job(), files)


def test_the_inventory_comes_in_bounded_batches(lib):
    job = lib.scan_job()
    with pytest.raises(jobs.JobError, match="at most"):
        scan.inventory(lib.conn, job, [lib.file(f"a{i}.mkv") for i in range(scan.MAX_INVENTORY_BATCH + 1)])


# ---------------------------------------------------------------- the records
def test_a_reported_file_becomes_a_record_with_its_findings_and_a_share_style_path(lib):
    job = lib.scan_job(prefix="Movies")
    tally = call(lib, scan.ingest_records, job, [lib.report(job, "Movies/Heat.1995.1080p.mkv", name="Heat (1995).mkv")])
    assert tally["inserted"] == 1 and not tally["failed"]
    rec = store.by_key(lib.conn, v.record_key("tfiles", "Movies/Heat.1995.1080p.mkv"))
    assert rec["root_name"] == "tfiles" and rec["source_path"] == "//tower/TFiles/Movies/Heat.1995.1080p.mkv" and rec["source_dir"] == "tfiles:Movies" and rec["recommended_name"] == "Heat (1995).mkv"
    assert rec["analysis_run_id"] == job["plan"]["run_id"] and rec["analysis_logic_version"] == v.ANALYSIS_LOGIC_VERSION and json.loads(rec["findings_json"])["action"] == "copy_rename"
    assert rec["recommended_action"] == "copy_rename" and rec["review_status"] == "not_required" and rec["duplicate_status"] == v.DUP_NOT_CHECKED and rec["sha256"] is None
    counts = scan.list_runs(lib.conn)[0]["counts"]
    assert counts["analyzed"] == 1 and counts["inserted"] == 1


def test_what_the_worker_says_is_checked_and_a_bad_record_does_not_lose_the_others(lib):
    job = lib.scan_job()
    good = lib.report(job, "good.mkv")
    broken = lib.report(job, "bad.mkv")
    del broken["findings"]
    nonsense = {**lib.report(job, "nonsense.mkv"), "size_bytes": "lots"}
    escape = {**lib.report(job, "x.mkv"), "rel": "../../etc/passwd"}
    tally = call(lib, scan.ingest_records, job, [good, broken, nonsense, escape])
    assert tally["inserted"] == 1 and len(tally["failed"]) == 3
    assert lib.conn.execute("SELECT relative_path FROM library_file").fetchall()[0][0] == "good.mkv"
    assert scan.list_runs(lib.conn)[0]["counts"]["errors"] == 3


def test_only_the_fields_a_worker_owns_are_taken_from_it(lib):
    job = lib.scan_job()
    sneaky = {**lib.report(job, "a.mkv"), "approval_status": "approved", "approved_action": "copy_rename", "migration_status": "completed", "sha256": "f" * 64,
              "duplicate_group": "DUP-x", "review_status": "reviewed", "recommended_action": "remux_copy", "id": 99, "missing_since": "2025-01-01 00:00:00"}
    call(lib, scan.ingest_records, job, [sneaky])
    rec = store.by_key(lib.conn, v.record_key("tfiles", "a.mkv"))
    assert (rec["approval_status"], rec["migration_status"], rec["sha256"], rec["duplicate_group"], rec["review_status"], rec["recommended_action"], rec["id"], rec["missing_since"]) == (
        "pending", "pending", None, None, "not_required", "copy_rename", 1, None)


def test_a_reanalysed_file_keeps_what_is_known_about_its_duplicates_and_its_approval(lib):
    a = add(lib.conn, root="tfiles", rel="a.mkv", name="A (2000).mkv", logic=0, size=5000)
    b = add(lib.conn, root="tfiles", rel="b.mkv", name="A (2000).mkv", logic=0, size=5000)
    duplicates.apply_results(lib.conn, [{"id": i, "size": 5000, "mtime": 1_700_000_000.0, "sha256": "a" * 64} for i in (a, b)])
    duplicates.finish(lib.conn, CFG)
    lib.conn.commit()
    primary = a if rec_of(lib.conn, a)["duplicate_status"] == v.DUP_PRIMARY else b
    store.set_approval(lib.conn, primary, v.APPROVAL_APPROVED, "ann", mark_reviewed=True)
    lib.conn.commit()
    job = lib.scan_job()
    rel = rec_of(lib.conn, primary)["relative_path"]
    call(lib, scan.ingest_records, job, [lib.report(job, rel, name="A (2000).mkv", size=5000)])
    rec = rec_of(lib.conn, primary)
    assert rec["duplicate_group"] == "DUP-" + "a" * 12 and rec["sha256"] == "a" * 64 and rec["analysis_logic_version"] == v.ANALYSIS_LOGIC_VERSION and rec["approval_status"] == "approved"


def test_records_come_in_bounded_batches(lib):
    job = lib.scan_job()
    with pytest.raises(jobs.JobError, match="at most"):
        scan.ingest_records(lib.conn, job, [{}] * (scan.MAX_RECORD_BATCH + 1))


# ---------------------------------------------------------------- finishing a scan
def test_files_the_scan_did_not_see_are_marked_missing_when_the_whole_listing_succeeded(lib):
    keep = add(lib.conn, root="tfiles", rel="Movies/Keep.mkv", name="Keep.mkv")
    gone = add(lib.conn, root="tfiles", rel="Movies/Gone.mkv", name="Gone.mkv")
    other = add(lib.conn, root="media", rel="Movies/Other.mkv", name="Other.mkv")
    job = lib.scan_job()
    call(lib, scan.inventory, job, [lib.file("Movies/Keep.mkv", 1_000_000)])
    summary = call(lib, scan.finish, job, {"complete": True, "walk_errors": []})
    assert rec_of(lib.conn, gone)["missing_since"] and not rec_of(lib.conn, keep)["missing_since"] and not rec_of(lib.conn, other)["missing_since"]
    counts = scan.list_runs(lib.conn)[0]["counts"]
    assert counts["missing"] == 1 and "groups" in summary


def test_nothing_is_marked_missing_when_a_folder_could_not_be_listed_or_the_listing_was_incomplete(lib):
    gone = add(lib.conn, root="tfiles", rel="Movies/Gone.mkv", name="Gone.mkv")
    job = lib.scan_job()
    call(lib, scan.finish, job, {"complete": True, "walk_errors": ["Permission denied: /mnt/tower/TFiles/Locked"]})
    assert not rec_of(lib.conn, gone)["missing_since"]
    counts = scan.list_runs(lib.conn)[0]["counts"]
    assert counts["walk_errors"] == 1 and counts["errors"] == 1 and "Permission denied" in counts["last_walk_error"]
    lib.conn.execute("UPDATE job SET status = 'done' WHERE id = ?", (job["id"],))
    lib.conn.commit()
    call(lib, scan.finish, lib.scan_job(), {"complete": False})
    assert not rec_of(lib.conn, gone)["missing_since"]


def test_files_in_folders_a_scan_left_out_are_not_marked_missing(lib):
    skipped = add(lib.conn, root="tfiles", rel="Movies/Extras/x.mkv", name="X.mkv")
    gone = add(lib.conn, root="tfiles", rel="Movies/Heat/h.mkv", name="H.mkv")
    job = lib.scan_job(exclude="extras")
    call(lib, scan.finish, job, {"complete": True, "walk_errors": []})
    assert not rec_of(lib.conn, skipped)["missing_since"] and rec_of(lib.conn, gone)["missing_since"]


def test_a_scan_of_one_folder_only_marks_missing_inside_that_folder(lib):
    inside = add(lib.conn, root="tfiles", rel="TV Shows/a.mkv", name="A.mkv")
    outside = add(lib.conn, root="tfiles", rel="Movies/b.mkv", name="B.mkv")
    call(lib, scan.finish, lib.scan_job(prefix="TV Shows"), {"complete": True, "walk_errors": []})
    assert rec_of(lib.conn, inside)["missing_since"] and not rec_of(lib.conn, outside)["missing_since"]


def test_finishing_a_scan_refreshes_what_depends_on_other_files(lib):
    a = add(lib.conn, root="tfiles", rel="Movies/Heat.1995.1080p.mkv", sha256="a" * 64)
    b = add(lib.conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", sha256="b" * 64)
    job = lib.scan_job()
    call(lib, scan.inventory, job, [lib.file("Movies/Heat.1995.1080p.mkv", 1_000_000)])
    call(lib, scan.finish, job, {"complete": False})
    assert "already holds a different file" in rec_of(lib.conn, a)["naming_flags"] and rec_of(lib.conn, b)["naming_flags"] is None


def test_the_exclude_rules_match_the_ones_on_the_worker():
    from .agent_media import analysis as agent_side
    patterns = ["Extras", "*Sample*", "@eaDir", "tfiles:Old/Stuff", "media:Keep", "  ", "Feature*"]
    server, worker = scan.ExcludeRules(patterns, "TFiles"), agent_side.ExcludeRules(patterns, "TFiles")
    for rel in ("Movies/Extras/e.mkv", "Movies/Heat/Sample/s.mkv", "@eaDir/x.mkv", "Old/Stuff/o.mkv", "old/stuff/deeper/o.mkv", "Keep/k.mkv", "Movies/Heat/h.mkv", "Oldies/x.mkv",
                "TV Shows/Show/Featurettes/f.mkv", "top.mkv", "Extras.mkv"):
        assert server.path(rel) == worker.path(rel), rel
    for folder in ("Movies/Extras", "Old/Stuff", "Old", "Keep", "Movies/Heat/Sample"):
        assert server.folder(folder) == worker.folder(folder), folder


# ---------------------------------------------------------------- duplicate searches
def test_a_search_with_nothing_to_read_finishes_at_once_and_forms_groups_from_known_hashes(lib):
    a = add(lib.conn, root="tfiles", rel="x/a.mkv", name="Heat (1995).mkv", size=5000, sha256="a" * 64)
    b = add(lib.conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=5000, sha256="a" * 64)
    run_id = scan.start_duplicate_search(lib.conn, "ann")
    lib.conn.commit()
    run = scan.list_runs(lib.conn)[0]
    assert run["id"] == run_id and run["status"] == "completed" and run["counts"]["groups"] == 1 and run["job_ids"] == []
    assert rec_of(lib.conn, b)["duplicate_status"] == v.DUP_PRIMARY and rec_of(lib.conn, a)["duplicate_status"] == v.DUP_OTHER


def test_a_search_reads_only_what_is_unknown_and_makes_a_job_per_batch(lib, monkeypatch):
    monkeypatch.setattr(duplicates, "MAX_FILES_PER_JOB", 2)
    for size in (100, 200):
        for i in range(2):
            add(lib.conn, root="tfiles" if i else "media", rel=f"x/{size}-{i}.mkv", name=f"{size}-{i}.mkv", size=size)
    add(lib.conn, rel="x/solo.mkv", name="solo.mkv", size=300)
    scan.start_duplicate_search(lib.conn, "ann")
    lib.conn.commit()
    run = scan.list_runs(lib.conn)[0]
    members = scan.run_jobs(lib.conn, run["id"])
    assert run["status"] == "queued" and len(members) == 2 and all(j["plan"]["op"] == "hash" and j["plan"]["roots"] == ["media", "tfiles"] for j in members)
    assert sum(len(g["files"]) for j in members for g in j["plan"]["groups"]) == 4
    with pytest.raises(v.LibraryError, match="already queued"):
        scan.start_duplicate_search(lib.conn, "ann")


def test_hash_results_are_stored_and_finishing_forms_the_groups(lib):
    a = add(lib.conn, root="tfiles", rel="x/a.mkv", name="Heat (1995).mkv", size=5000)
    b = add(lib.conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=5000)
    scan.start_duplicate_search(lib.conn, "ann")
    lib.conn.commit()
    (job,) = lib.claim()
    results = [{"id": i, "size": 5000, "mtime": 1_700_000_000.0, "sha256": "d" * 64} for i in (a, b)]
    assert call(lib, scan.ingest_hashes, job, results) == 2
    summary = call(lib, scan.finish, job, {})
    assert summary["groups"] == 1 and rec_of(lib.conn, a)["duplicate_status"] == v.DUP_OTHER and rec_of(lib.conn, b)["duplicate_status"] == v.DUP_PRIMARY
    counts = scan.list_runs(lib.conn)[0]["counts"]
    assert counts["hashed"] == 2 and counts["groups"] == 1 and "ruled_out" in counts


def test_a_job_can_only_be_used_for_what_it_is(lib):
    job = lib.scan_job()
    with pytest.raises(jobs.JobError, match="not a hash job"):
        scan.ingest_hashes(lib.conn, job, [])
    with pytest.raises(jobs.JobError, match="not a scan job"):
        scan.inventory(lib.conn, {**job, "plan": {"op": "hash"}}, [])
    with pytest.raises(jobs.JobError):
        scan.finish(lib.conn, {**job, "plan": {"op": "copy", "run_id": "x"}})
