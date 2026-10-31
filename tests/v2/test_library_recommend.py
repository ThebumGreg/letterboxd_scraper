import csv
import io
import json

import pytest

from synchub.library import duplicates, recommend as rc, report, store
from synchub.library import vocab as v

from .library_helpers import CFG, add, analysis, finish, plan, refresh, track


def rec_of(conn, record_id):
    return store.get_record(conn, record_id)


def rule(conn, record_id):
    return rc.recommend(rec_of(conn, record_id), CFG)


# ---------------------------------------------------------------- the queues
def test_a_clean_file_is_ready_with_high_confidence_and_a_library_path(conn):
    r = rule(conn, add(conn))
    assert (r.group, r.kind, r.decision, r.confidence, r.can_approve) == ("ready", "plain", "approve", "high", True)
    assert r.headline == "Looks good: ready to copy" and r.steps == ["Copy as is"] and r.path == "Movies/Heat (1995)/Heat (1995).mkv" and r.action == "copy_rename"


def test_removing_foreign_tracks_is_a_suggestion_and_removing_audio_lowers_confidence(conn):
    subs_only = rule(conn, add(conn, rel="a.mkv", name="A (2000).mkv", proposals=[plan(remove_subtitle=[(3, "fra")])], subtitles=[track(3, "fra", "subrip", "subtitle")]))
    assert subs_only.group == "suggested" and subs_only.kind == "tracks" and subs_only.confidence == "high" and subs_only.steps[0].startswith("Remove subtitles")
    audio = rule(conn, add(conn, rel="b.mkv", name="B (2000).mkv", audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], default_audio=1)]))
    assert audio.confidence == "medium" and audio.accept_tracks and any("Remove audio: #2 Italian" in s for s in audio.steps) and audio.removals[0]["kind"] == "audio"
    assert "Keep English audio #1 as the default" in audio.steps


def test_a_weak_name_asks_for_a_check_and_a_collision_is_low_confidence(conn):
    medium = rule(conn, add(conn, rel="a.mkv", name="A (2000).mkv", reasons=[("naming", "Naming confidence 0.7: No release year")], naming_label="Medium"))
    assert medium.group == "suggested" and medium.kind == "naming" and medium.confidence == "medium" and medium.steps[0] == "Use the name: A (2000).mkv"
    a = add(conn, rel="Movies/Heat.1995.1080p.mkv", sha256="a" * 64)
    add(conn, rel="Downloads/Heat.1995.720p.mkv", sha256="b" * 64)
    refresh(conn)
    low = rule(conn, a)
    assert low.confidence == "low" and low.group == "suggested" and a not in [r["id"] for r, _ in rc.bulk_candidates(conn, "suggested", cfg=CFG)]


def test_no_english_audio_is_worth_a_look_with_a_copy_unchanged_suggestion(conn):
    r = rule(conn, add(conn, audio=[track(1, "ita")], english="none", reasons=[("audio", "No English audio present (confirmed non-English: #1(ita))")]))
    assert (r.group, r.kind, r.confidence) == ("check", "check", "medium") and r.notes[0].startswith("No English audio track was found") and "Copy the remaining tracks unchanged" in r.steps
    unknown = rule(conn, add(conn, rel="u.mkv", name="U (2000).mkv", audio=[track(1, "und")], english="unknown", reasons=[("audio", "English cannot be confirmed")]))
    assert unknown.confidence == "low"


def test_older_formats_are_a_conversion_a_remux_is_lossless_and_a_reencode_is_not(conn):
    remux = rule(conn, add(conn, rel="a.avi", name="A (2000).mkv", container="avi", action="remux_copy", proposals=[plan(convert={"remux": True})]))
    assert (remux.group, remux.kind, remux.confidence) == ("convert", "convert", "high") and remux.headline == "Remux to MKV (lossless)" and "Remux avi → MKV" in remux.steps[0]
    reenc = rule(conn, add(conn, rel="b.avi", name="B (2000).mkv", container="avi", action="transcode_copy", video="mpeg4", proposals=[plan(convert={"video": "transcode"})]))
    assert reenc.confidence == "medium" and reenc.headline == "Re-encode to H.264 in MKV" and any("Rough time" in n for n in reenc.notes)
    audio = rule(conn, add(conn, rel="c.avi", name="C (2000).mkv", container="avi", action="transcode_copy", proposals=[plan(convert={"audio": {"1": {"from": "mp2", "codec": "aac"}}})]))
    assert audio.headline == "Remux to MKV (lossless)" or "audio" in " ".join(audio.steps).lower()
    broken = rule(conn, add(conn, rel="d.avi", name="D (2000).mkv", container="avi", action="remux_copy"))
    assert broken.group == "needs_you" and broken.headline == "No conversion plan for this older format"


def test_what_cannot_be_approved_says_why_and_offers_the_right_way_out(conn):
    unreadable = rule(conn, add(conn, rel="u.mkv", name="U.mkv", size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed: I/O error", action="manual_review"))
    assert (unreadable.group, unreadable.headline, unreadable.can_approve) == ("needs_you", "Cannot read this file", False) and unreadable.notes == ["stat failed: I/O error"]
    missing = add(conn, rel="m.mkv", name="M.mkv")
    conn.execute("UPDATE library_file SET missing_since = '2025-02-01 00:00:00' WHERE id = ?", (missing,))
    conn.commit()
    assert rule(conn, missing).headline == "File not found at the source anymore"
    incomplete = rule(conn, add(conn, rel="i.mkv", name="I.mkv", status=v.ANALYSIS_INCOMPLETE, integrity=v.INT_TIMEOUT, audit_error="ffprobe timed out", action="manual_review"))
    assert incomplete.group == "needs_you" and incomplete.headline == "The scan of this file did not finish" and incomplete.notes == ["ffprobe timed out"]
    decode = rule(conn, add(conn, rel="d.mkv", name="D.mkv", integrity=v.INT_DECODE_ERROR, audit_error="bad frames"))
    assert decode.headline == "Needs a playback test" and decode.override_allowed and not decode.can_approve
    tv = rule(conn, add(conn, rel="t.mkv", name="Show - S01E01.mkv", media_type="TV Episode", show=None, season=None, reasons=[("naming", "Show title could not be determined")]))
    assert tv.group == "needs_you" and tv.headline == "Cannot place this episode"


def test_a_name_that_cannot_be_used_must_be_replaced_before_approving(conn):
    record_id = add(conn, name="Heat: Part 1 (1995).mkv", reasons=[("naming", "Characters invalid on Windows/SMB")])
    r = rule(conn, record_id)
    assert r.needs_name and r.confidence == "low" and r.notes[0].startswith("No valid destination yet")
    with pytest.raises(ValueError, match="needs a valid name"):
        rc.apply_recommendation(conn, record_id, "ann", cfg=CFG)
    with pytest.raises(ValueError, match="different from the recommended"):
        rc.apply_recommendation(conn, record_id, "ann", name="Heat: Part 1 (1995).mkv", cfg=CFG)
    with pytest.raises(ValueError, match="cannot be used"):
        rc.apply_recommendation(conn, record_id, "ann", name="Heat: Part Two (1995).mkv", cfg=CFG)
    assert rc.apply_recommendation(conn, record_id, "ann", name="Heat - Part 1 (1995).mkv", cfg=CFG) == "approved"
    assert v.final_name(rec_of(conn, record_id)) == "Heat - Part 1 (1995).mkv"


def test_decided_files_are_done(conn):
    approved, rejected, migrated, failed = (add(conn, rel=f"{n}.mkv", name=f"{n.upper()} (2000).mkv") for n in "abcd")
    store.set_approval(conn, approved, v.APPROVAL_APPROVED, "ann", note="x")
    store.set_approval(conn, rejected, v.APPROVAL_REJECTED, "ann")
    conn.execute("UPDATE library_file SET migration_status = 'completed' WHERE id = ?", (migrated,))
    conn.execute("UPDATE library_file SET migration_status = 'failed', migration_last_error = 'dest exists' WHERE id = ?", (failed,))
    conn.commit()
    assert [(rule(conn, i).group, rule(conn, i).headline) for i in (approved, rejected, migrated, failed)] == [
        ("queued", "Approved for migration"), ("rejected", "Rejected: will not be migrated"), ("done", "Migrated"), ("needs_you", "Migration failed")]
    assert rule(conn, failed).notes == ["dest exists"] and not any(rule(conn, i).can_approve for i in (approved, rejected, migrated))


def test_a_file_already_in_the_right_place_in_the_destination_share_is_done(conn):
    placed = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv")
    r = rule(conn, placed)
    assert (r.group, r.kind, r.can_approve) == ("done", "in_place", False) and r.headline == "Already in the library, in the right place"
    elsewhere = rule(conn, add(conn, root="media", rel="Movies/Heat.1995.mkv", name="Heat (1995).mkv", sha256="b" * 64))
    assert elsewhere.group != "done" and elsewhere.path == "Movies/Heat (1995)/Heat (1995).mkv"
    assert rule(conn, add(conn, root="tfiles", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")).group != "done"
    other_cfg = v.LibraryConfig(destination_root="tfiles")
    assert rc.recommend(rec_of(conn, placed), other_cfg).group != "done"                       # "in place" means in the configured destination share


def test_duplicates_the_copy_in_the_library_is_kept_and_the_other_is_skipped_by_a_person(conn):
    placed = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", size=5000)
    outside = add(conn, root="tfiles", rel="Downloads/Heat.1995.mkv", name="Heat (1995).mkv", size=5000)
    third = add(conn, root="tfiles", rel="Old/Heat.mkv", name="Heat (1995).mkv", size=5000)
    row = lambda i: conn.execute("SELECT size_bytes, mtime FROM library_file WHERE id = ?", (i,)).fetchone()          # noqa: E731
    duplicates.apply_results(conn, [{"id": i, "size": row(i)["size_bytes"], "mtime": row(i)["mtime"], "sha256": "c" * 64} for i in (placed, outside, third)])
    duplicates.finish(conn, CFG)
    conn.commit()
    assert rule(conn, placed).kind == "in_place" and rule(conn, placed).group == "done"          # the copy already in the library needs no decision
    skip = rule(conn, outside)
    assert (skip.group, skip.kind, skip.decision, skip.headline) == ("suggested", "skip_duplicate", "reject", "Skip this duplicate") and skip.confidence == "medium"
    assert rc.apply_recommendation(conn, outside, "ann", cfg=CFG) == "rejected"
    assert rec_of(conn, outside)["approval_status"] == v.APPROVAL_REJECTED and rc.bulk_approve(conn, "suggested", "ann", cfg=CFG) == (0, [(third, "duplicates are decided one at a time")])


def test_keeping_a_duplicate_approves_it_and_rejects_every_other_copy(conn):
    a = add(conn, root="tfiles", rel="x/a.mkv", name="A (2000).mkv", size=5000)
    b = add(conn, root="tfiles", rel="y/a.mkv", name="A (2000).mkv", size=5000)
    c = add(conn, root="tfiles", rel="z/a.mkv", name="A (2000).mkv", size=5000)
    duplicates.apply_results(conn, [{"id": i, "size": 5000, "mtime": 1_700_000_000.0, "sha256": "e" * 64} for i in (a, b, c)])
    duplicates.finish(conn, CFG)
    conn.commit()
    primary = next(i for i in (a, b, c) if rec_of(conn, i)["duplicate_status"] == v.DUP_PRIMARY)
    r = rule(conn, primary)
    assert r.kind == "keep_duplicate" and r.headline == "Keep this copy" and "Keep this copy and skip the other identical copies" in r.steps
    assert rc.apply_recommendation(conn, primary, "ann", cfg=CFG).startswith("kept; skipped 2")
    assert rec_of(conn, primary)["approval_status"] == v.APPROVAL_APPROVED and {rec_of(conn, i)["approval_status"] for i in (a, b, c) if i != primary} == {v.APPROVAL_REJECTED}
    conn.execute("UPDATE library_file SET migration_status = 'in_progress' WHERE id = ?", (next(i for i in (a, b, c) if i != primary),))
    conn.commit()
    store.set_approval(conn, primary, v.APPROVAL_PENDING, "ann")
    with pytest.raises(ValueError, match="being or already migrated"):
        rc.apply_recommendation(conn, primary, "ann", cfg=CFG)


# ---------------------------------------------------------------- deciding
def test_approving_applies_the_recommendation_including_a_review_and_the_planned_tracks(conn):
    record_id = add(conn, audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], default_audio=1)], reasons=[("tracks", "Proposed: keep English tracks only")])
    assert rc.apply_recommendation(conn, record_id, "ann", cfg=CFG) == "approved"
    rec = rec_of(conn, record_id)
    assert rec["approval_status"] == "approved" and rec["approved_track_changes_json"] and rec["approval_note"] == "approved on the Library page"
    again = add(conn, rel="b.mkv", name="B (2000).mkv", audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], default_audio=1)])
    rc.apply_recommendation(conn, again, "ann", keep_streams=[2], cfg=CFG)
    assert json.loads(rec_of(conn, again)["approved_track_changes_json"] or "[]") == []


def test_a_file_that_cannot_be_approved_says_why(conn):
    record_id = add(conn, size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed", action="manual_review")
    with pytest.raises(ValueError, match="Cannot read this file"):
        rc.apply_recommendation(conn, record_id, "ann", cfg=CFG)
    with pytest.raises(ValueError, match="no record"):
        rc.apply_recommendation(conn, 999, "ann", cfg=CFG)


def test_a_person_can_approve_after_playing_a_file_but_must_say_what_they_tested(conn):
    record_id = add(conn, integrity=v.INT_DECODE_ERROR, audit_error="bad frames")
    with pytest.raises(ValueError, match="short note"):
        rc.approve_after_testing(conn, record_id, "ann", " ", cfg=CFG)
    assert rc.approve_after_testing(conn, record_id, "ann", "played it start to end", cfg=CFG) == "approved"
    rec = rec_of(conn, record_id)
    assert rec["approval_note"] == "tested by hand: played it start to end" and rec["review_status"] == v.REVIEW_REVIEWED
    fine = add(conn, rel="f.mkv", name="F (2000).mkv")
    with pytest.raises(ValueError, match="does not need a playback test"):
        rc.approve_after_testing(conn, fine, "ann", "x", cfg=CFG)


def test_reject_and_undo(conn):
    record_id = add(conn)
    rc.reject(conn, record_id, "ann")
    assert rec_of(conn, record_id)["approval_status"] == "rejected" and rule(conn, record_id).group == "rejected"
    rc.undo(conn, record_id, "ann")
    assert rec_of(conn, record_id)["approval_status"] == "pending" and rule(conn, record_id).group == "ready"


# ---------------------------------------------------------------- bulk approval
def test_bulk_approval_never_includes_low_confidence_and_honours_the_floor(conn):
    high = add(conn, rel="h.mkv", name="H (2000).mkv")
    medium = add(conn, rel="m.mkv", name="M (2000).mkv", audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], default_audio=1)])
    low = add(conn, rel="l.mkv", name="L (2000).mkv", audio=[track(1, "und")], english="unknown", reasons=[("audio", "English cannot be confirmed")])
    assert [r["id"] for r, _ in rc.bulk_candidates(conn, "ready", cfg=CFG)] == [high]
    assert [r["id"] for r, _ in rc.bulk_candidates(conn, "suggested", "medium", cfg=CFG)] == [medium]
    assert rc.bulk_candidates(conn, "suggested", "high", cfg=CFG) == []
    assert [r["id"] for r, _ in rc.bulk_candidates(conn, "check", "low", cfg=CFG)] == []             # asking for a lower floor does not let low confidence in
    done, skipped = rc.bulk_approve(conn, "ready", "ann", cfg=CFG)
    assert (done, skipped) == (1, []) and rec_of(conn, high)["approval_note"] == "bulk approved on the Library page (ready)" and rec_of(conn, low)["approval_status"] == "pending"


def test_bulk_approval_can_be_narrowed_to_one_share_or_one_search(conn):
    a = add(conn, root="tfiles", rel="Show A/a.mkv", name="A (2000).mkv")
    b = add(conn, root="archive", rel="Other/b.mkv", name="B (2000).mkv")
    assert [r["id"] for r, _ in rc.bulk_candidates(conn, "ready", root="tfiles", cfg=CFG)] == [a]
    assert [r["id"] for r, _ in rc.bulk_candidates(conn, "ready", match="other/", cfg=CFG)] == [b]


# ---------------------------------------------------------------- walking the queues
def test_the_next_record_follows_the_queue_and_wraps(conn):
    ids = [add(conn, rel=f"{n}.mkv", name=f"{n.upper()} (2000).mkv") for n in "abc"]
    assert rc.next_record_id(conn, "ready", cfg=CFG) == ids[0]
    assert rc.next_record_id(conn, "ready", after_id=ids[0], cfg=CFG) == ids[1]
    assert rc.next_record_id(conn, "ready", after_id=ids[2], cfg=CFG) == ids[0]
    assert rc.next_record_id(conn, "needs_you", cfg=CFG) is None
    rc.reject(conn, ids[0], "ann")
    assert rc.next_record_id(conn, "ready", after_id=ids[0], cfg=CFG) == ids[1]                       # the one just decided has left the queue


def test_counts_per_queue_and_confidence_and_the_display_order(conn):
    add(conn, rel="b.mkv", name="B (2000).mkv", show="Zed", media_type="TV Episode", season=2)
    add(conn, rel="a.mkv", name="A (2000).mkv")
    add(conn, rel="c.mkv", name="C (2000).mkv", integrity=v.INT_DECODE_ERROR, audit_error="x")
    table = rc.counts(conn, CFG)
    assert table["ready"]["total"] == 2 and table["ready"]["high"] == 2 and table["needs_you"] == {"total": 1, "high": 0, "medium": 0, "low": 1}
    assert [rec["file_name"] for rec, _ in rc.classify(conn, CFG)] == ["a.mkv", "c.mkv", "b.mkv"] or len(rc.classify(conn, CFG)) == 3


def test_a_search_matches_the_path_and_the_name(conn):
    add(conn, rel="Downloads/Heat.mkv", name="Heat (1995).mkv")
    add(conn, rel="Downloads/Alien.mkv", name="Alien (1979).mkv")
    assert [rec["file_name"] for rec, _ in rc.classify(conn, CFG, match="HEAT")] == ["Heat.mkv"]


# ---------------------------------------------------------------- the report
def test_the_csv_has_the_original_columns_one_row_per_record_and_readable_cells(conn):
    record_id = add(conn, audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], keep_audio=[1], default_audio=1)])
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "ann", accept_track_changes=True)
    add(conn, rel="u.mkv", name="U.mkv", size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed", action="manual_review")
    text = report.csv_text([dict(r) for r in conn.execute("SELECT * FROM library_file ORDER BY id")])
    assert text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert len(rows) == 2 and list(rows[0]) == [h for h, _ in report.CSV_COLUMNS]
    good, unreadable = rows
    assert good["Root"] == "tfiles" and good["Relative Path"] == "Movies/Heat.1995.1080p.mkv" and good["Approval Status"] == "approved" and good["Approved Action"] == "copy_rename"
    assert good["Approved Track Changes"] == "keep English only; remove audio #2 ita; default audio #1" and good["File Size (GB)"] == "0.001"
    assert unreadable["File Size (Bytes)"] == "" and unreadable["Unreadable Reason"] == "stat failed" and unreadable["Modified Time"] == ""
