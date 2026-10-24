import json

import pytest

from synchub.library import store
from synchub.library import vocab as v

from .library_helpers import add, analysis, finish, plan, track


def rec_of(conn, record_id):
    return store.get_record(conn, record_id)


def rescan(conn, run, **kw):
    rec = finish(analysis(run=run, **kw))
    tally = store.upsert_records(conn, [rec], rec["source_dir"], run)
    conn.commit()
    return tally


# ---------------------------------------------------------------- identity
def test_a_file_is_one_record_whatever_the_case_of_its_path(conn):
    first = add(conn, rel="Movies/Heat (1995)/Heat (1995).mkv")
    again = rescan(conn, "run-2", rel="MOVIES/heat (1995)/HEAT (1995).MKV")
    assert again["unchanged"] == 1 and again["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 1
    assert rec_of(conn, first)["last_seen_run_id"] == "run-2"


def test_the_same_path_on_two_shares_is_two_records(conn):
    add(conn, root="media", rel="Movies/A/A.mkv")
    add(conn, root="tfiles", rel="Movies/A/A.mkv")
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 2


# ---------------------------------------------------------------- rescans keep decisions only while they are still true
def test_an_unchanged_file_keeps_its_approval_and_clears_missing(conn):
    record_id = add(conn)
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")
    conn.execute("UPDATE library_file SET missing_since = '2025-01-02 00:00:00' WHERE id = ?", (record_id,))
    conn.commit()
    tally = rescan(conn, "run-2")
    rec = rec_of(conn, record_id)
    assert tally["unchanged"] == 1 and rec["approval_status"] == v.APPROVAL_APPROVED and rec["missing_since"] is None and rec["source_revision"] == 1


def test_a_file_that_changed_gets_a_new_revision_and_loses_its_decisions(conn):
    record_id = add(conn)
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")
    conn.execute("UPDATE library_file SET migration_status = 'completed', migration_verification_status = 'verified' WHERE id = ?", (record_id,))
    conn.commit()
    tally = rescan(conn, "run-2", size=2_000_000)
    rec = rec_of(conn, record_id)
    assert tally["changed"] == 1 and tally["approvals_reset"] == 1
    assert rec["source_revision"] == 2 and rec["size_bytes"] == 2_000_000 and rec["approval_status"] == v.APPROVAL_PENDING and rec["approved_action"] is None
    assert rec["migration_status"] == v.MIGRATION_PENDING and rec["signature_changed_at"]
    kinds = [e["event_type"] for e in store.events_for(conn, record_id)]
    assert "source_changed" in kinds and "approval_approved" in kinds


def test_a_modified_time_within_the_tolerance_is_not_a_change(conn):
    record_id = add(conn)
    assert rescan(conn, "run-2", mtime=1_700_000_001.5)["unchanged"] == 1
    assert rescan(conn, "run-3", mtime=1_700_000_010.0)["changed"] == 1
    assert rec_of(conn, record_id)["source_revision"] == 2


def test_a_new_analysis_result_resets_an_approval_that_depended_on_the_old_one(conn):
    record_id = add(conn, action="copy_rename")
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")
    tally = rescan(conn, "run-2", action="remux_copy", container="avi")
    rec = rec_of(conn, record_id)
    assert tally["unchanged"] == 1 and tally["approvals_reset"] == 1 and rec["approval_status"] == v.APPROVAL_PENDING and rec["source_revision"] == 1
    assert [e for e in store.events_for(conn, record_id) if e["event_type"] == "decision_changed"]


def test_a_review_by_a_person_stands_until_the_result_changes(conn):
    record_id = add(conn, reasons=[("audio", "no English")], action="copy_rename")
    assert rec_of(conn, record_id)["review_status"] == v.REVIEW_MANUAL
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", mark_reviewed=True)
    rescan(conn, "run-2", reasons=[("audio", "no English")], action="copy_rename")
    assert rec_of(conn, record_id)["review_status"] == v.REVIEW_REVIEWED and rec_of(conn, record_id)["approval_status"] == v.APPROVAL_APPROVED
    rescan(conn, "run-3", reasons=[("audio", "no English")], action="remux_copy")
    rec = rec_of(conn, record_id)
    assert rec["review_status"] == v.REVIEW_MANUAL and rec["approval_status"] == v.APPROVAL_PENDING and rec["review_decided_by"] is None


def test_a_new_concern_about_an_approved_file_withdraws_the_approval(conn):
    record_id = add(conn)
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")
    rescan(conn, "run-2", reasons=[("audio", "No English audio present")])                       # the action is unchanged: only a new reason to look
    rec = rec_of(conn, record_id)
    assert rec["review_status"] == v.REVIEW_MANUAL and rec["recommended_action"] == "copy_rename" and rec["approval_status"] == v.APPROVAL_PENDING
    again = add(conn, rel="Movies/Other.mkv", name="Other (2001).mkv")
    store.set_approval(conn, again, v.APPROVAL_APPROVED, "ann")
    rescan(conn, "run-2", rel="Movies/Other.mkv", name="Other (2001).mkv", integrity=v.INT_DECODE_ERROR, audit_error="bad frames")
    assert rec_of(conn, again)["approval_status"] == v.APPROVAL_PENDING                           # and a decode error, which also changes the action


def test_an_unreadable_file_keeps_its_analysis_and_a_placeholder_recovers(conn):
    record_id = add(conn)
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")
    placeholder = finish(analysis(run="run-2", size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed: I/O error", action="manual_review"))
    tally = store.upsert_records(conn, [placeholder], "tfiles", "run-2")
    conn.commit()
    rec = rec_of(conn, record_id)
    assert tally["unreadable_flagged"] == 1 and rec["unreadable_reason"] == "stat failed: I/O error" and rec["approval_status"] == v.APPROVAL_APPROVED and rec["size_bytes"] == 1_000_000
    other = add(conn, rel="Movies/Other.mkv", size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed", action="manual_review")
    rescan(conn, "run-3", rel="Movies/Other.mkv", name="Other.mkv", size=500, mtime=1_700_000_500.0)
    recovered = rec_of(conn, other)
    assert recovered["unreadable_reason"] is None and recovered["size_bytes"] == 500 and recovered["analysis_status"] == v.ANALYSIS_COMPLETED and recovered["source_revision"] == 1


def test_a_migration_in_progress_owns_the_record(conn):
    record_id = add(conn)
    conn.execute("UPDATE library_file SET migration_status = 'in_progress' WHERE id = ?", (record_id,))
    conn.commit()
    tally = rescan(conn, "run-2", size=5, action="remux_copy")
    rec = rec_of(conn, record_id)
    assert tally["locked"] == 1 and rec["size_bytes"] == 1_000_000 and rec["last_seen_run_id"] == "run-2"
    with pytest.raises(v.StateError):
        store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")


def test_one_bad_record_is_reported_and_the_rest_are_kept(conn):
    good = finish(analysis(rel="Movies/Good.mkv", name="Good.mkv"))
    bad = finish(analysis(rel="Movies/Bad.mkv", name="Bad.mkv"))
    del bad["file_name"]
    tally = store.upsert_records(conn, [good, bad], "tfiles", "run-1")
    conn.commit()
    assert tally["inserted"] == 1 and [f[0] for f in tally["failed"]] == ["Movies/Bad.mkv"]
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 1


# ---------------------------------------------------------------- seen and missing
def test_files_a_scan_did_not_see_are_marked_missing_only_inside_what_was_scanned(conn):
    inside = add(conn, rel="TV Shows/Show/a.mkv", name="a.mkv")
    outside = add(conn, rel="Movies/b.mkv", name="b.mkv")
    other_share = add(conn, root="media", rel="TV Shows/Show/c.mkv", name="c.mkv")
    seen = add(conn, rel="TV Shows/Show/d.mkv", name="d.mkv")
    store.touch_seen(conn, [v.record_key("tfiles", "TV Shows/Show/d.mkv")], "run-2")
    assert store.mark_missing(conn, "run-2", "tfiles", "TV Shows") == 1
    conn.commit()
    assert rec_of(conn, inside)["missing_since"] and not rec_of(conn, outside)["missing_since"] and not rec_of(conn, other_share)["missing_since"]
    assert not rec_of(conn, seen)["missing_since"]
    assert [e["event_type"] for e in store.events_for(conn, inside)][0] == "source_missing"


def test_a_folder_left_out_of_a_scan_does_not_make_its_files_missing(conn):
    skipped = add(conn, rel="Movies/Extras/x.mkv", name="x.mkv")
    gone = add(conn, rel="Movies/Heat/y.mkv", name="y.mkv")
    store.mark_missing(conn, "run-2", "tfiles", "", skip=lambda rel: rel.startswith("Movies/Extras"))
    conn.commit()
    assert not rec_of(conn, skipped)["missing_since"] and rec_of(conn, gone)["missing_since"]


# ---------------------------------------------------------------- approvals
def test_approving_records_who_what_and_why_and_undo_wipes_it(conn):
    record_id = add(conn)
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", note="looks right")
    rec = rec_of(conn, record_id)
    assert (rec["approval_status"], rec["approved_action"], rec["approval_decided_by"], rec["approval_note"]) == ("approved", "copy_rename", "ann", "looks right")
    store.set_approval(conn, record_id, v.APPROVAL_PENDING, "ann")
    rec = rec_of(conn, record_id)
    assert rec["approval_status"] == "pending" and rec["approved_action"] is None and rec["approval_decided_by"] is None
    assert [e["event_type"] for e in store.events_for(conn, record_id)] == ["approval_pending", "approval_approved"]


def test_a_file_that_needs_review_is_approved_only_together_with_the_review(conn):
    record_id = add(conn, reasons=[("audio", "no English audio")])
    with pytest.raises(ValueError, match="manual review"):
        store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", mark_reviewed=True, note="fine")
    rec = rec_of(conn, record_id)
    assert rec["review_status"] == v.REVIEW_REVIEWED and rec["review_decided_by"] == "ann"


def test_nothing_is_approved_without_a_finished_analysis_or_a_migratable_action(conn):
    incomplete = add(conn, rel="Movies/I.mkv", name="I.mkv", status=v.ANALYSIS_INCOMPLETE, integrity=v.INT_TIMEOUT, action="manual_review")
    with pytest.raises(ValueError, match="no migratable action|analysis is not completed"):
        store.set_approval(conn, incomplete, v.APPROVAL_APPROVED, "ann", mark_reviewed=True)
    record_id = add(conn, rel="Movies/M.mkv", name="M.mkv")
    with pytest.raises(ValueError, match="no migratable action"):
        store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", action="delete_everything")
    with pytest.raises(ValueError):
        store.set_approval(conn, record_id, "maybe", "ann")
    with pytest.raises(v.StateError):
        store.set_approval(conn, 9999, v.APPROVAL_APPROVED, "ann")


def test_an_edited_name_must_be_valid_keep_its_extension_and_not_clash(conn):
    first = add(conn, rel="Movies/One.mkv", name="One (2000).mkv")
    second = add(conn, rel="Movies/Two.mkv", name="Two (2001).mkv")
    for bad, fragment in (("Bad:Name.mkv", "invalid"), ("Name.mp4", "extension"), ("CON.mkv", "reserved"), ("x" * 300 + ".mkv", "255 bytes")):
        with pytest.raises(ValueError, match=fragment):
            store.set_approval(conn, first, v.APPROVAL_APPROVED, "ann", name=bad)
    store.set_approval(conn, first, v.APPROVAL_APPROVED, "ann", name="One Edited (2000).mkv")
    assert v.final_name(rec_of(conn, first)) == "One Edited (2000).mkv"
    store.set_approval(conn, second, v.APPROVAL_APPROVED, "ann")
    with pytest.raises(ValueError, match="collides"):
        store.set_approval(conn, first, v.APPROVAL_APPROVED, "ann", name="Two (2001).mkv")
    assert v.final_name(rec_of(conn, first)) == "One Edited (2000).mkv"                        # the failed attempt changed nothing


def test_track_changes_are_approved_only_when_asked_and_a_reviewer_can_only_keep_more(conn):
    removal = plan(remove_audio=[(2, "ita")], remove_subtitle=[(4, "fra"), (5, "ita")], keep_audio=[1], keep_subtitle=[3], default_audio=1)
    record_id = add(conn, audio=[track(1), track(2, "ita")], proposals=[removal], english="present")
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann")
    assert rec_of(conn, record_id)["approved_track_changes_json"] is None                       # by default no track change is approved
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", accept_track_changes=True, keep_streams=[4])
    approved = json.loads(rec_of(conn, record_id)["approved_track_changes_json"])[0]
    assert [t["index"] for t in approved["remove_subtitle"]] == [5] and 4 in approved["keep_subtitle"] and [t["index"] for t in approved["remove_audio"]] == [2]
    with pytest.raises(ValueError, match="not in the proposed removals"):
        store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", accept_track_changes=True, keep_streams=[1])
    plain = add(conn, rel="Movies/P.mkv", name="P.mkv")
    with pytest.raises(ValueError, match="no proposed track changes"):
        store.set_approval(conn, plain, v.APPROVAL_APPROVED, "ann", accept_track_changes=True)


def test_keeping_every_proposed_removal_leaves_nothing_to_approve_for_a_plain_plan(conn):
    removal = plan(remove_audio=[(2, "ita")], keep_audio=[1])
    record_id = add(conn, audio=[track(1), track(2, "ita")], proposals=[removal])
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", accept_track_changes=True, keep_streams=[2])
    assert rec_of(conn, record_id)["approved_track_changes_json"] is None


def test_a_conversion_plan_survives_keeping_every_track(conn):
    conversion = plan(remove_audio=[(2, "ita")], keep_audio=[1], convert={"remux": True})
    record_id = add(conn, container="avi", action="remux_copy", audio=[track(1), track(2, "ita")], proposals=[conversion])
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", accept_track_changes=True, keep_streams=[2])
    assert json.loads(rec_of(conn, record_id)["approved_track_changes_json"])[0]["op"] == "convert"


def test_filename_problems_catch_what_windows_and_smb_shares_cannot_hold():
    assert v.filename_problems("Heat (1995).mkv") == []
    assert v.filename_problems("  ") == ["name is empty"]
    assert any("leading/trailing" in p for p in v.filename_problems(" lead.mkv")) and any("leading/trailing" in p for p in v.filename_problems("trail .mkv"))
    assert any("trailing dot" in p for p in v.filename_problems("dot..mkv")) and any("invalid" in p for p in v.filename_problems("a|b.mkv"))
    assert any("reserved" in p for p in v.filename_problems("com1.mkv")) and any("255" in p for p in v.filename_problems("é" * 130 + ".mkv"))


def test_identity_ignores_case_slashes_and_unicode_form():
    assert v.record_key("TFiles", "Movies\\Heat/") == v.record_key("tfiles", "movies/heat")
    assert v.record_key("media", "Cafe\u0301/x") == v.record_key("media", "Caf\u00e9/x")
    assert v.under("TV Shows/Show/a.mkv", "tv shows") and v.under("a", "") and not v.under("TV Showsx/a", "TV Shows") and v.under("TV Shows", "TV Shows")
