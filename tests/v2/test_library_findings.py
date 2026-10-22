import json

from synchub.library import duplicates, findings, store
from synchub.library import vocab as v

from .library_helpers import CFG, add, analysis, finish, refresh


def rec_of(conn, record_id):
    return store.get_record(conn, record_id)


def reasons_of(rec):
    return [tuple(r) for r in json.loads(rec["review_reasons_json"])]


# ---------------------------------------------------------------- what a record's own findings turn into
def test_a_clean_file_needs_no_review():
    d = findings.derive(finish(analysis()))
    assert (d["new_review"], d["review_categories"], d["recommended_action"], d["base_action"]) == (v.REVIEW_NOT_REQUIRED, None, "copy_rename", "copy_rename")


def test_a_naming_or_integrity_reason_escalates_to_manual_review_and_keeps_the_original_action_as_the_base():
    d = findings.derive(analysis(reasons=[("naming", "Naming confidence 0.4: No release year found")], action_reason="legacy"))
    assert d["recommended_action"] == "manual_review" and d["base_action"] == "copy_rename" and d["review_categories"] == "naming"
    assert d["action_reason"] == "legacy; Naming confidence 0.4: No release year found" and d["new_review"] == v.REVIEW_MANUAL
    d = findings.derive(analysis(integrity=v.INT_DECODE_ERROR, audit_error="x" * 500))
    assert d["recommended_action"] == "manual_review" and d["review_categories"] == "integrity" and len(json.loads(d["review_reasons_json"])[0][1]) < 260


def test_an_audio_reason_asks_for_review_without_changing_the_action():
    d = findings.derive(analysis(reasons=[("audio", "No English audio present")]))
    assert d["recommended_action"] == "copy_rename" and d["new_review"] == v.REVIEW_MANUAL and d["review_categories"] == "audio"
    assert d["review_reason"] == "[audio] No English audio present"


def test_an_action_the_worker_already_made_manual_has_no_base_action():
    d = findings.derive(analysis(action="manual_review", action_reason="Audit incomplete"))
    assert d["recommended_action"] == "manual_review" and d["base_action"] is None and d["new_review"] == v.REVIEW_MANUAL


# ---------------------------------------------------------------- destinations
def test_a_file_is_in_place_only_at_exactly_the_right_path_in_the_destination_share(conn):
    right = finish(analysis(root="media", rel="Movies/Heat (1995)/Heat (1995).mkv"))
    assert findings.in_place(right, CFG) and findings.in_place({**right, "relative_path": "movies/HEAT (1995)/heat (1995).MKV"}, CFG)
    assert not findings.in_place({**right, "root_name": "tfiles"}, CFG)                          # right path, wrong share
    assert not findings.in_place({**right, "relative_path": "Movies/Heat.mkv"}, CFG)
    assert not findings.in_place({**right, "recommended_name": "A:B.mkv"}, CFG)                    # no valid destination at all


def test_two_different_files_for_one_destination_are_a_collision_for_both(conn):
    a = add(conn, rel="Movies/Heat.1995.1080p.mkv", sha256="a" * 64)
    b = add(conn, rel="Downloads/Heat.1995.720p.mkv", sha256="b" * 64)
    refresh(conn)
    for record_id in (a, b):
        rec = rec_of(conn, record_id)
        assert "collision" in rec["naming_flags"].lower() and rec["recommended_action"] == "manual_review" and "naming" in rec["review_categories"]
    assert rec_of(conn, a)["review_status"] == v.REVIEW_MANUAL


def test_exact_copies_of_each_other_are_one_file_not_a_collision(conn):
    a = add(conn, rel="Movies/Heat.1995.1080p.mkv", sha256="a" * 64)
    b = add(conn, rel="Downloads/Heat.1995.1080p.mkv", sha256="a" * 64)
    duplicates.finish(conn, CFG)
    conn.commit()
    assert not [r for r in (rec_of(conn, a), rec_of(conn, b)) if r["naming_flags"] and "collision" in r["naming_flags"].lower()]


def test_a_file_already_at_the_destination_is_not_at_fault_but_a_different_file_wanting_it_is_told(conn):
    holder = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", sha256="a" * 64)
    newcomer = add(conn, root="tfiles", rel="Downloads/Heat.1995.mkv", sha256="b" * 64)
    refresh(conn)
    assert rec_of(conn, holder)["naming_flags"] is None and rec_of(conn, holder)["recommended_action"] == "copy_rename"
    flags = rec_of(conn, newcomer)["naming_flags"]
    assert "already holds a different file" in flags and "media:Movies/Heat (1995)/Heat (1995).mkv" in flags and rec_of(conn, newcomer)["recommended_action"] == "manual_review"


def test_files_without_a_valid_destination_or_with_a_naming_error_do_not_collide(conn):
    a = add(conn, rel="x/a.mkv", name="A:B.mkv")
    b = add(conn, rel="y/b.mkv", name="A:B.mkv")
    c = add(conn, rel="x/c.mkv", name="C.mkv", naming_error=True)
    d = add(conn, rel="y/d.mkv", name="C.mkv", naming_error=True)
    refresh(conn)
    assert all("collision" not in (rec_of(conn, i)["naming_flags"] or "").lower() for i in (a, b, c, d))


def test_tv_episodes_collide_only_within_the_same_show_and_season(conn):
    a = add(conn, rel="a/s.mkv", name="Show - S01E01.mkv", media_type="TV Episode", show="Show", season=1)
    b = add(conn, rel="b/s.mkv", name="Show - S01E01.mkv", media_type="TV Episode", show="Show", season=1, sha256="b" * 64)
    other = add(conn, rel="c/s.mkv", name="Show - S01E01.mkv", media_type="TV Episode", show="Other Show", season=1)
    refresh(conn)
    assert "collision" in rec_of(conn, a)["naming_flags"].lower() and "collision" in rec_of(conn, b)["naming_flags"].lower()
    assert rec_of(conn, other)["naming_flags"] is None


def test_a_collision_clears_when_one_of_the_files_goes_away(conn):
    a = add(conn, rel="Movies/Heat.1995.1080p.mkv", sha256="a" * 64)
    b = add(conn, rel="Downloads/Heat.1995.720p.mkv", sha256="b" * 64)
    refresh(conn)
    assert rec_of(conn, a)["review_status"] == v.REVIEW_MANUAL
    conn.execute("UPDATE library_file SET missing_since = '2025-02-01 00:00:00' WHERE id = ?", (b,))
    conn.commit()
    refresh(conn)
    rec = rec_of(conn, a)
    assert rec["naming_flags"] is None and rec["recommended_action"] == "copy_rename" and rec["review_status"] == v.REVIEW_NOT_REQUIRED


def test_a_new_collision_withdraws_the_approval_given_before_it_existed(conn):
    a = add(conn, rel="Movies/Heat.1995.1080p.mkv", sha256="a" * 64)
    store.set_approval(conn, a, v.APPROVAL_APPROVED, "ann")
    add(conn, rel="Downloads/Heat.1995.720p.mkv", sha256="b" * 64)
    refresh(conn)
    rec = rec_of(conn, a)
    assert rec["approval_status"] == v.APPROVAL_PENDING and rec["recommended_action"] == "manual_review"
    assert [e for e in store.events_for(conn, a) if e["event_type"] == "decision_changed" and e["detail"]["why"]]


def test_refresh_changes_nothing_when_nothing_changed_and_skips_records_being_migrated(conn):
    a = add(conn, rel="Movies/Heat.1995.1080p.mkv", sha256="a" * 64)
    b = add(conn, rel="Downloads/Heat.1995.720p.mkv", sha256="b" * 64)
    assert findings.refresh(conn, CFG) == 2
    conn.commit()
    assert findings.refresh(conn, CFG) == 0
    conn.execute("UPDATE library_file SET migration_status = 'in_progress' WHERE id = ?", (a,))
    conn.execute("UPDATE library_file SET missing_since = '2025-02-01 00:00:00' WHERE id = ?", (b,))
    conn.commit()
    before = rec_of(conn, a)
    findings.refresh(conn, CFG)
    assert rec_of(conn, a)["review_reasons_json"] == before["review_reasons_json"]


# ---------------------------------------------------------------- duplicates
def hashed(conn, **kw):
    return add(conn, **kw)


def test_candidates_are_files_of_the_same_size_and_only_unread_ones_are_planned(conn):
    a = add(conn, root="media", rel="Movies/A/A.mkv", name="A.mkv", size=5000)
    b = add(conn, root="tfiles", rel="x/A.mkv", name="A.mkv", size=5000)
    add(conn, rel="x/solo.mkv", name="solo.mkv", size=7777)
    add(conn, rel="x/empty1.mkv", name="e1.mkv", size=0)
    add(conn, rel="x/empty2.mkv", name="e2.mkv", size=0)
    plans = duplicates.plan_jobs(conn)
    assert len(plans) == 1 and plans[0]["op"] == "hash" and [g["size"] for g in plans[0]["groups"]] == [5000]
    assert sorted(f["id"] for f in plans[0]["groups"][0]["files"]) == sorted([a, b]) and plans[0]["groups"][0]["files"][0]["root"] in ("media", "tfiles")
    conn.execute("UPDATE library_file SET sha256 = ? WHERE id = ?", ("a" * 64, a))
    conn.execute("UPDATE library_file SET sha256 = ? WHERE id = ?", ("b" * 64, b))
    conn.commit()
    assert duplicates.plan_jobs(conn) == []                                       # everything is known: no reading needed


def test_a_search_is_split_into_jobs_of_bounded_size(conn, monkeypatch):
    monkeypatch.setattr(duplicates, "MAX_FILES_PER_JOB", 3)
    for size in (100, 200, 300):
        for i in range(2):
            add(conn, rel=f"x/{size}-{i}.mkv", name=f"{size}-{i}.mkv", size=size)
    plans = duplicates.plan_jobs(conn)
    assert [sum(len(g["files"]) for g in p["groups"]) for p in plans] == [2, 2, 2]
    assert sum(len(g["files"]) for p in plans for g in p["groups"]) == 6


def result(conn, record_id, **kw):
    row = conn.execute("SELECT size_bytes, mtime FROM library_file WHERE id = ?", (record_id,)).fetchone()
    return {"id": record_id, "size": row["size_bytes"], "mtime": row["mtime"], **kw}


def test_hash_results_form_groups_with_a_stable_id_and_one_primary_candidate(conn):
    a = add(conn, root="tfiles", rel="x/Heat.mkv", name="Heat (1995).mkv", size=5000)
    b = add(conn, root="tfiles", rel="y/Heat.mkv", name="Heat (1995).mkv", size=5000)
    c = add(conn, root="tfiles", rel="z/Other.mkv", name="Other.mkv", size=5000)
    assert duplicates.apply_results(conn, [result(conn, a, sha256="a" * 64), result(conn, b, sha256="a" * 64), result(conn, c, sha256="c" * 64)]) == 3
    groups, _ = duplicates.finish(conn, CFG)
    conn.commit()
    assert groups == 1
    ra, rb, rc = rec_of(conn, a), rec_of(conn, b), rec_of(conn, c)
    assert ra["duplicate_group"] == rb["duplicate_group"] == "DUP-" + "a" * 12 and rc["duplicate_group"] is None and rc["duplicate_status"] == v.DUP_NONE
    assert (ra["duplicate_status"], rb["duplicate_status"]) == (v.DUP_PRIMARY, v.DUP_OTHER)                 # the first by path
    assert rb["recommended_action"] == "manual_review" and "Exact SHA-256 duplicate" in rb["review_reason"] and "Member of exact duplicate group" in ra["review_reason"]


def test_the_copy_already_in_the_destination_share_is_the_one_kept(conn):
    outside = add(conn, root="tfiles", rel="a/Heat.mkv", name="Heat (1995).mkv", size=5000)
    placed = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=5000)
    misplaced = add(conn, root="media", rel="Other/Heat.mkv", name="Heat (1995).mkv", size=5000)
    duplicates.apply_results(conn, [result(conn, i, sha256="d" * 64) for i in (outside, placed, misplaced)])
    duplicates.finish(conn, CFG)
    conn.commit()
    assert rec_of(conn, placed)["duplicate_status"] == v.DUP_PRIMARY and rec_of(conn, outside)["duplicate_status"] == v.DUP_OTHER and rec_of(conn, misplaced)["duplicate_status"] == v.DUP_OTHER
    assert duplicates.choose_primary([rec_of(conn, outside), rec_of(conn, misplaced)], CFG)["id"] == misplaced          # then any copy in the destination share
    assert duplicates.choose_primary([rec_of(conn, outside)], CFG)["id"] == outside


def test_a_file_whose_size_or_date_changed_since_the_job_was_planned_is_ignored(conn):
    a = add(conn, rel="x/a.mkv", name="a.mkv", size=5000)
    stale = {**result(conn, a, sha256="a" * 64), "size": 4999}
    later = {**result(conn, a, sha256="a" * 64), "mtime": 1_800_000_000.0}
    assert duplicates.apply_results(conn, [stale, later, {"id": 999, "size": 1, "mtime": 1, "sha256": "x"}, {"id": a, "size": 5000, "mtime": 1_700_000_000.0}]) == 0
    assert rec_of(conn, a)["sha256"] is None


def test_a_file_the_prefilter_ruled_out_is_not_read_again_and_an_error_is_remembered(conn):
    a = add(conn, rel="x/a.mkv", name="a.mkv", size=5000)
    b = add(conn, rel="x/b.mkv", name="b.mkv", size=5000)
    duplicates.apply_results(conn, [result(conn, a, ruled_out=True, partial="p1"), result(conn, b, error="I/O error")])
    conn.commit()
    assert rec_of(conn, a)["duplicate_status"] == v.DUP_RULED_OUT and rec_of(conn, a)["partial_signature"] == "p1"
    assert rec_of(conn, b)["duplicate_status"] == v.DUP_INCOMPLETE
    assert [f["id"] for p in duplicates.plan_jobs(conn) for g in p["groups"] for f in g["files"]] == [b]               # only the one that failed is tried again
    duplicates.finish(conn, CFG)
    conn.commit()
    assert "Hash incomplete" in rec_of(conn, b)["review_reason"] and rec_of(conn, b)["review_status"] == v.REVIEW_MANUAL


def test_a_duplicate_group_dissolves_when_a_copy_is_gone_or_changes(conn):
    a = add(conn, root="tfiles", rel="x/a.mkv", name="A.mkv", size=5000)
    b = add(conn, root="tfiles", rel="y/a.mkv", name="A.mkv", size=5000)
    duplicates.apply_results(conn, [result(conn, a, sha256="a" * 64), result(conn, b, sha256="a" * 64)])
    duplicates.finish(conn, CFG)
    conn.commit()
    assert rec_of(conn, b)["duplicate_group"]
    conn.execute("UPDATE library_file SET missing_since = '2025-02-01 00:00:00' WHERE id = ?", (a,))
    conn.commit()
    duplicates.finish(conn, CFG)
    conn.commit()
    rec = rec_of(conn, b)
    assert rec["duplicate_group"] is None and rec["duplicate_status"] == v.DUP_NONE and rec["recommended_action"] == "copy_rename" and rec["review_status"] == v.REVIEW_NOT_REQUIRED
    changed = finish(analysis(root="tfiles", rel="y/a.mkv", name="A.mkv", size=6000, mtime=1_700_100_000.0, run="run-2"))
    store.upsert_records(conn, [changed], "tfiles", "run-2")
    conn.commit()
    assert rec_of(conn, b)["sha256"] is None and rec_of(conn, b)["source_revision"] == 2                      # a changed file's old hash is not trusted
