import csv
import io
import json
import re

import pytest

from synchub import jobs, settings, storage, workers
from synchub.library import duplicates, scan, store
from synchub.library import vocab as v

from .library_helpers import CFG, add, plan, track
from .test_library_findings import rec_of


def shares(conn):
    for name, share in (("media", "Media"), ("tfiles", "TFiles")):
        storage.create(conn, {"name": name, "protocol": "smb", "host": "tower", "share": share, "mount_point": f"/mnt/tower/{share}"})
    conn.commit()


def post(client, url, body=None):
    return client.post(url, json=body or {})


def row_for(page, record_id):
    return re.search(r'<tr data-record="%d">.*?</tr>' % record_id, page, re.S).group(0)


# ---------------------------------------------------------------- the overview
def test_the_library_is_in_the_nav_and_an_empty_one_says_how_to_begin(client, conn):
    page = client.get("/library").get_data(as_text=True)
    assert 'href="/library"' in page and "Nothing has been scanned yet" in page and 'href="/storage"' in page
    shares(conn)
    page = client.get("/library").get_data(as_text=True)
    assert "Choose a share below and press" in page and '<option value="tfiles">tfiles</option>' in page


def test_the_overview_counts_each_queue_by_confidence_and_each_share(client, conn):
    shares(conn)
    add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=2 * 1024 ** 3)
    add(conn, root="tfiles", rel="a.mkv", name="A (2000).mkv", size=1024 ** 3)
    add(conn, root="tfiles", rel="b.mkv", name="B (2000).mkv", audio=[track(1, "und")], english="unknown", reasons=[("audio", "English cannot be confirmed")])
    add(conn, root="tfiles", rel="c.mkv", name="C.mkv", integrity=v.INT_DECODE_ERROR, audit_error="x")
    page = client.get("/library").get_data(as_text=True)
    card = lambda key: re.search(r'data-queue="%s">.*?</div>\s*</div>\s*</div>' % key, page, re.S).group(0)      # noqa: E731
    assert ">1</span>" in card("ready") and "1 high" in card("ready") and "1 low" in card("check") and "1 low" in card("needs_you") and ">1</span>" in card("done")
    summary = re.search(r'<table[^>]*id="shareSummary".*?</table>', page, re.S).group(0)
    assert re.search(r"media.*?destination.*?<td class=\"text-end\">1</td><td class=\"text-end\">2.00 GB</td>", summary, re.S) and re.search(r"tfiles.*?<td class=\"text-end\">3</td><td class=\"text-end\">1.00 GB</td>", summary, re.S)


def test_a_scan_is_started_from_the_page_and_shows_up_as_queued_with_a_stop_button(client, conn):
    shares(conn)
    r = post(client, "/api/library/scan", {"root": "tfiles", "prefix": "TV Shows", "integrity": "full", "force": True, "exclude": "Extras\nSample"})
    assert r.get_json()["ok"] and r.get_json()["run"]
    plan = scan.run_jobs(conn, r.get_json()["run"])[0]["plan"]
    assert (plan["root"], plan["prefix"], plan["integrity"], plan["force"], plan["exclude"]) == ("tfiles", "TV Shows", "full", True, ["Extras", "Sample"])
    page = client.get("/library/fragment").get_data(as_text=True)
    assert "Scan <strong>tfiles</strong>/TV Shows" in page and "every file" in page and ">queued<" in page and "data-cancel-run" in page and 'data-active="1"' in page
    again = post(client, "/api/library/scan", {"root": "tfiles", "prefix": "TV Shows"})
    assert again.status_code == 400 and "already queued" in again.get_json()["message"]


@pytest.mark.parametrize("body, fragment", [({"root": "nowhere"}, "no share called"), ({"root": "tfiles", "prefix": "../x"}, "inside the share"), ({"root": "tfiles", "integrity": "sometimes"}, "metadata, tail or full")])
def test_a_scan_that_cannot_run_is_explained_on_the_page(client, conn, body, fragment):
    shares(conn)
    r = post(client, "/api/library/scan", body)
    assert r.status_code == 400 and fragment in r.get_json()["message"]


def test_a_run_can_be_stopped_from_the_page(client, conn):
    shares(conn)
    run_id = post(client, "/api/library/scan", {"root": "tfiles"}).get_json()["run"]
    r = post(client, f"/api/library/runs/{run_id}/cancel")
    assert r.get_json()["ok"] and "Nothing was running" not in r.get_json()["message"]
    assert [j["status"] for j in scan.run_jobs(conn, run_id)] == ["cancelled"]
    assert post(client, f"/api/library/runs/{run_id}/cancel").get_json()["message"] == "Nothing was running."
    assert post(client, "/api/library/runs/nope/cancel").status_code == 404


def test_a_duplicate_search_is_started_from_the_page(client, conn):
    shares(conn)
    add(conn, root="tfiles", rel="x/a.mkv", name="A.mkv", size=5000)
    add(conn, root="media", rel="y/a.mkv", name="A.mkv", size=5000)
    r = post(client, "/api/library/duplicates")
    assert r.get_json()["ok"] and "queued" in r.get_json()["message"]
    assert "Duplicate search" in client.get("/library/fragment").get_data(as_text=True)
    assert post(client, "/api/library/duplicates").status_code == 400


def test_the_library_settings_are_validated_saved_and_change_what_is_in_place(client, conn):
    shares(conn)
    placed = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")
    assert "done" in client.get("/library/queue/done").get_data(as_text=True) and f'data-record="{placed}"' in client.get("/library/queue/done").get_data(as_text=True)
    bad = post(client, "/api/library/settings", {"library_destination_root": "nowhere", "library_tail_seconds": "2", "library_movie_folder": "Mo/vies"})
    assert bad.status_code == 400 and "no share called" in bad.get_json()["message"] and "library_tail_seconds" in bad.get_json()["message"] and "library_movie_folder" in bad.get_json()["message"]
    assert settings.get("library_destination_root", conn=conn) == "media"
    ok = post(client, "/api/library/settings", {"library_destination_root": "tfiles", "library_movie_folder": "Films", "library_integrity_mode": "full", "library_tail_seconds": "45", "library_exclude_folders": " Extras \n\n Sample "})
    assert ok.get_json()["ok"]
    assert (settings.get("library_destination_root", conn=conn), settings.get("library_movie_folder", conn=conn), settings.get("library_exclude_folders", conn=conn)) == ("tfiles", "Films", "Extras\nSample")
    assert f'data-record="{placed}"' not in client.get("/library/queue/done").get_data(as_text=True)        # media is no longer where things belong
    page = client.get("/library").get_data(as_text=True)
    assert 'value="Films"' in page and "Last 45 s" in page and "Films/&lt;name&gt;/&lt;name&gt;" in page


def test_no_library_page_reuses_an_id_the_site_wide_script_attaches_to(client, conn):
    """app.js hooks #settingsForm on the Settings page and expects its unsaved-changes badge; a Library form with that id broke on every keystroke."""
    record_id = add(conn)
    for url in ("/library", "/library/queue/ready", f"/library/record/{record_id}", "/library/bulk/ready"):
        assert 'id="settingsForm"' not in client.get(url).get_data(as_text=True)


# ---------------------------------------------------------------- the lists
def test_a_list_shows_each_file_what_we_suggest_and_how_sure_we_are(client, conn):
    a = add(conn, root="tfiles", rel="Downloads/Heat.1995.1080p.mkv", name="Heat (1995).mkv", size=1024 ** 3)
    b = add(conn, root="tfiles", rel="Downloads/Multi.mkv", name="Multi (2001).mkv", audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], default_audio=1)])
    page = client.get("/library/queue/ready").get_data(as_text=True)
    row = row_for(page, a)
    assert "Heat (1995).mkv" in row and "tfiles: Downloads/Heat.1995.1080p.mkv" in row and "Looks good: ready to copy" in row and ">high<" in row and "1.00 GB" in row and 'data-decide="approve"' in row
    suggested = row_for(client.get("/library/queue/suggested").get_data(as_text=True), b)
    assert "Keep English tracks only" in suggested and ">medium<" in suggested and "Remove audio: #2 Italian" in suggested
    assert row_for(client.get("/library/queue/ready").get_data(as_text=True), a) and "Looks good" not in client.get("/library/queue/suggested").get_data(as_text=True)


def test_a_list_can_be_filtered_by_text_share_and_confidence(client, conn):
    shares(conn)
    a = add(conn, root="tfiles", rel="Downloads/Heat.mkv", name="Heat (1995).mkv")
    b = add(conn, root="archive", rel="Other/Alien.mkv", name="Alien (1979).mkv")
    c = add(conn, root="tfiles", rel="Downloads/Weak.mkv", name="Weak (2000).mkv", naming_label="Medium", reasons=[("naming", "Naming confidence 0.7: No release year")])
    ids = lambda url: set(int(i) for i in re.findall(r'data-record="(\d+)"', client.get(url).get_data(as_text=True)))      # noqa: E731
    assert ids("/library/queue/ready") == {a, b} and ids("/library/queue/ready?q=heat") == {a} and ids("/library/queue/ready?root=archive") == {b} and ids("/library/queue/ready?conf=medium") == set()
    assert ids("/library/queue/suggested?conf=medium") == {c} and ids("/library/queue/suggested?conf=high") == set()
    assert "Nothing here with this filter" in client.get("/library/queue/ready?q=zzz").get_data(as_text=True)


def test_a_long_list_is_paged_and_tv_is_grouped_by_show_and_season(client, conn, monkeypatch):
    from synchub.web import library
    monkeypatch.setattr(library, "PAGE_SIZE", 2)
    for ep in (1, 2, 3):
        add(conn, rel=f"s/{ep}.mkv", name=f"Show - S01E0{ep}.mkv", media_type="TV Episode", show="Show", season=1)
    add(conn, rel="s/other.mkv", name="Other - S02E01.mkv", media_type="TV Episode", show="Other", season=2)
    first = client.get("/library/queue/ready").get_data(as_text=True)
    assert "Other &middot; Season 2" in first and first.count('data-record="') == 2 and 'aria-label="Pages"' in first and "4 files in this list" in first
    second = client.get("/library/queue/ready?page=2").get_data(as_text=True)
    assert "Show &middot; Season 1" in second and second.count('data-record="') == 2
    on_first, on_second = set(re.findall(r'data-record="(\d+)"', first)), set(re.findall(r'data-record="(\d+)"', second))
    assert len(on_first) == len(on_second) == 2 and not on_first & on_second                # every file once, none twice
    assert client.get("/library/queue/ready?page=abc").status_code == 200


def test_decided_files_offer_undo_and_blocked_ones_only_open(client, conn):
    done = add(conn, rel="a.mkv", name="A (2000).mkv")
    store.set_approval(conn, done, v.APPROVAL_APPROVED, "ann")
    conn.commit()
    blocked = add(conn, rel="b.mkv", name="B.mkv", size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed", action="manual_review")
    page = client.get("/library/queue/queued").get_data(as_text=True)         # approved, not finished: Approved and queued, not Done
    assert 'data-decide="undo"' in row_for(page, done)
    placed = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")
    assert "data-decide" not in row_for(client.get("/library/queue/done").get_data(as_text=True), placed)             # nobody decided it, so there is nothing to undo
    needs = row_for(client.get("/library/queue/needs_you").get_data(as_text=True), blocked)
    assert "Open</a>" in needs and "data-decide" not in needs and "Cannot read this file" in needs


def test_the_approve_together_bar_counts_only_what_qualifies(client, conn):
    add(conn, rel="a.mkv", name="A (2000).mkv")
    add(conn, rel="b.mkv", name="B (2001).mkv")
    add(conn, rel="m.mkv", name="M (2002).mkv", audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], default_audio=1)])
    ready = client.get("/library/queue/ready").get_data(as_text=True)
    assert "Approve 2 (high and medium)" in ready and "Only the" not in ready
    suggested = client.get("/library/queue/suggested").get_data(as_text=True)
    assert "Approve 1 (high and medium)" in suggested
    add(conn, rel="c.mkv", name="C (2003).mkv", subtitles=[track(3, "fra", "subrip", "subtitle")], proposals=[plan(remove_subtitle=[(3, "fra")])])
    suggested = client.get("/library/queue/suggested").get_data(as_text=True)
    assert "Approve 2 (high and medium)" in suggested and "Only the 1 high" in suggested
    assert client.get("/library/queue/nope").status_code == 302 and client.get("/library/bulk/nope").status_code == 302


# ---------------------------------------------------------------- one by one
def test_review_walks_the_queue_skips_and_ends_with_a_thank_you(client, conn):
    ids = [add(conn, rel=f"{n}.mkv", name=f"{n.upper()} (2000).mkv") for n in "abc"]
    first = client.get("/library/review/ready")
    assert first.status_code == 302 and f"/library/record/{ids[0]}" in first.headers["Location"] and "group=ready" in first.headers["Location"]
    assert f"/library/record/{ids[1]}" in client.get(f"/library/review/ready?after={ids[0]}").headers["Location"]
    assert f"/library/record/{ids[0]}" in client.get(f"/library/review/ready?after={ids[2]}").headers["Location"]              # wraps round
    for i in ids:
        post(client, f"/api/library/record/{i}/approve")
    end = client.get("/library/review/ready")
    assert end.status_code == 302 and "/library?done=ready" in end.headers["Location"]
    assert "Nothing left in &ldquo;ready&rdquo;" in client.get("/library?done=ready").get_data(as_text=True)


def test_the_record_page_shows_the_plan_the_tracks_the_facts_and_the_history(client, conn):
    record_id = add(conn, root="tfiles", rel="Downloads/Multi.mkv", name="Multi (2001).mkv", audio=[track(1), track(2, "ita")], subtitles=[track(3, "fra", "subrip", "subtitle"), track(4, "eng", "subrip", "subtitle")],
                    proposals=[plan(remove_audio=[(2, "ita")], remove_subtitle=[(3, "fra")], keep_subtitle=[4], default_audio=1)], reasons=[("tracks", "Proposed: keep English tracks only")])
    store.set_approval(conn, record_id, v.APPROVAL_PENDING, "ann", note="looked once")
    conn.commit()
    page = client.get(f"/library/record/{record_id}?group=suggested").get_data(as_text=True)
    assert "Multi (2001).mkv" in page and "tfiles: Downloads/Multi.mkv" in page and "Keep English tracks only" in page and "<code>Movies/Multi (2001)/Multi (2001).mkv</code>" in page
    assert 'id="newName"' in page and 'value="Multi (2001).mkv"' in page and "Suggested: <code>Multi (2001).mkv</code>" in page
    tracks = re.search(r'<table[^>]*id="tracks".*?</table>', page, re.S).group(0)
    assert tracks.count("data-remove=") == 2 and 'data-remove="2"' in tracks and 'data-remove="3"' in tracks and "Italian" in tracks and "French" in tracks
    assert 'id="act-approve"' in page and 'id="act-reject"' in page and 'id="act-skip"' in page and "approval pending" in page
    assert "Container" in page and "matroska" in page and "1920x1080" in page and "Tail Sample Passed" in page and "ann" in page
    assert "Playback in Jellyfin" in page and "Container MKV" in page and "Suggested" in page


def test_a_track_matroska_cannot_hold_cannot_be_unticked(client, conn):
    plans = [plan(remove_subtitle=[(3, "eng")], convert={"remux": True})]
    plans[0]["remove_subtitle"][0]["reason"] = "subtitle format cannot be stored in MKV"
    record_id = add(conn, rel="a.avi", name="A (2000).mkv", container="avi", action="remux_copy", subtitles=[track(3, "eng", "mov_text", "subtitle")], proposals=plans)
    page = client.get(f"/library/record/{record_id}").get_data(as_text=True)
    assert re.search(r'data-remove="3"[^>]*checked[^>]* disabled', page) and "MKV cannot hold this subtitle format" in page


def test_a_file_that_needs_a_playback_test_offers_the_tested_it_form_and_a_missing_or_unreadable_file_says_so(client, conn):
    flagged = add(conn, rel="d.mkv", name="D (2000).mkv", integrity=v.INT_DECODE_ERROR, audit_error="bad frames")
    page = client.get(f"/library/record/{flagged}").get_data(as_text=True)
    assert "Played it and it is fine?" in page and 'id="testNote"' in page and 'data-act="tested"' in page and 'id="act-approve"' not in page and "bad frames" in page
    gone = add(conn, rel="g.mkv", name="G (2000).mkv")
    conn.execute("UPDATE library_file SET missing_since = '2025-02-01 00:00:00' WHERE id = ?", (gone,))
    bad = add(conn, rel="u.mkv", name="U.mkv", size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed: I/O error", action="manual_review")
    conn.commit()
    assert "was not found at its place" in client.get(f"/library/record/{gone}").get_data(as_text=True)
    unreadable = client.get(f"/library/record/{bad}").get_data(as_text=True)
    assert "could not be read: stat failed: I/O error" in unreadable and 'id="act-approve"' not in unreadable and 'data-act="reject"' in unreadable


def test_sizes_use_a_unit_that_fits_and_subtitles_show_no_channel_count(client, conn):
    from synchub.web.library import _size
    assert [_size(n) for n in (None, 0, 900, 2048, 5 * 1024 ** 2, 3 * 1024 ** 3)] == ["?", "0 B", "900 B", "2.00 KB", "5.00 MB", "3.00 GB"]
    record_id = add(conn, subtitles=[track(3, "eng", "subrip", "subtitle")], proposals=[plan(remove_subtitle=[(3, "eng")])])         # the table is shown when something is proposed
    page = client.get(f"/library/record/{record_id}").get_data(as_text=True)
    tracks = re.search(r'<table[^>]*id="tracks".*?</table>', page, re.S).group(0)
    assert "aac 2ch" in tracks and "subrip 2ch" not in tracks and "subrip" in tracks


def test_the_network_path_is_shown_once_and_only_when_it_adds_something(client, conn):
    plain = add(conn, root="tfiles", rel="a/x.mkv", name="X (2000).mkv")
    assert client.get(f"/library/record/{plain}").get_data(as_text=True).count("tfiles:a/x.mkv") == 0
    conn.execute("UPDATE library_file SET source_path = '//tower/TFiles/a/x.mkv' WHERE id = ?", (plain,))
    conn.commit()
    assert "//tower/TFiles/a/x.mkv" in client.get(f"/library/record/{plain}").get_data(as_text=True)


def test_the_record_page_of_a_duplicate_lists_the_identical_copies(client, conn):
    a = add(conn, root="tfiles", rel="x/a.mkv", name="Heat (1995).mkv", size=5000)
    b = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=5000)
    duplicates.apply_results(conn, [{"id": i, "size": 5000, "mtime": 1_700_000_000.0, "sha256": "d" * 64} for i in (a, b)])
    duplicates.finish(conn, CFG)
    conn.commit()
    page = client.get(f"/library/record/{a}").get_data(as_text=True)
    assert "Identical copies" in page and f"/library/record/{b}" in page and "media: Movies/Heat (1995)/Heat (1995).mkv" in page and "Skip this duplicate" in page
    assert "already at its place in the library" in client.get(f"/library/record/{b}").get_data(as_text=True)


def test_everything_from_the_files_is_escaped_and_an_unknown_record_goes_home(client, conn):
    record_id = add(conn, rel="<script>alert(1)</script>.mkv", name="<img src=x onerror=alert(2)>.mkv", reasons=[("audio", "<b>bold</b> reason")], show="<i>show</i>")
    hostile = add(conn, rel="h.mkv", name="H (2000).mkv", audio=[track(1), track(2, "<u>ita</u>")], proposals=[plan(remove_audio=[(2, "<u>ita</u>")], default_audio=1)])      # a language tag the file made up
    for url in (f"/library/record/{record_id}", f"/library/record/{hostile}", "/library/queue/check", "/library/queue/needs_you", "/library/queue/ready", "/library/queue/suggested"):
        page = client.get(url).get_data(as_text=True)
        assert "<script>alert(1)</script>" not in page and "<img src=x" not in page and "<b>bold</b>" not in page and "<u>ita</u>" not in page
    assert "&lt;u&gt;ita&lt;/u&gt;" in client.get("/library/queue/suggested").get_data(as_text=True)
    assert client.get("/library/record/9999").status_code == 302


# ---------------------------------------------------------------- decisions, recorded with who made them
def test_approving_from_the_page_records_the_profile_that_did_it(client, conn):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    client.set_cookie("synchub_profile", "1")
    record_id = add(conn)
    r = post(client, f"/api/library/record/{record_id}/approve", {"note": "fine"})
    assert r.get_json() == {"ok": True, "message": "Approved."}
    rec = rec_of(conn, record_id)
    assert (rec["approval_status"], rec["approval_decided_by"]) == ("approved", "Alice")
    events = store.events_for(conn, record_id)          # newest first: the job it queued, then the decision
    assert [e["event_type"] for e in events] == ["migration_queued", "approval_approved"] and {e["detail"]["by"] for e in events} == {"Alice"}
    guest = add(conn, rel="g.mkv", name="G (2000).mkv")
    client.set_cookie("synchub_profile", "0")
    post(client, f"/api/library/record/{guest}/approve")
    assert rec_of(conn, guest)["approval_decided_by"] == "guest"


def test_approve_can_change_the_name_and_keep_tracks_and_reject_and_undo_work(client, conn):
    record_id = add(conn, name="Heat 1995.mkv", audio=[track(1), track(2, "ita")], proposals=[plan(remove_audio=[(2, "ita")], default_audio=1)])
    r = post(client, f"/api/library/record/{record_id}/approve", {"name": "Heat (1995).mkv", "keep_streams": [2]})
    assert r.get_json()["ok"]
    rec = rec_of(conn, record_id)
    assert rec["approved_name"] == "Heat (1995).mkv" and json.loads(rec["approved_track_changes_json"] or "[]") == []
    assert post(client, f"/api/library/record/{record_id}/undo").get_json()["message"] == "Decision undone." and rec_of(conn, record_id)["approval_status"] == "pending"
    assert post(client, f"/api/library/record/{record_id}/reject", {"note": "wrong cut"}).get_json()["message"].startswith("Rejected")
    assert (rec_of(conn, record_id)["approval_status"], rec_of(conn, record_id)["approval_note"]) == ("rejected", "wrong cut")


def test_a_skipped_duplicate_says_so_and_a_decision_that_cannot_be_made_says_why(client, conn):
    a = add(conn, root="tfiles", rel="x/a.mkv", name="A (2000).mkv", size=5000)
    b = add(conn, root="tfiles", rel="y/a.mkv", name="A (2000).mkv", size=5000)
    duplicates.apply_results(conn, [{"id": i, "size": 5000, "mtime": 1_700_000_000.0, "sha256": "e" * 64} for i in (a, b)])
    duplicates.finish(conn, CFG)
    conn.commit()
    other = b if rec_of(conn, a)["duplicate_status"] == v.DUP_PRIMARY else a
    assert post(client, f"/api/library/record/{other}/approve").get_json()["message"] == "Skipped: that copy will not be migrated."
    blocked = add(conn, rel="u.mkv", name="U.mkv", size=0, mtime=0.0, status=v.ANALYSIS_FAILED, unreadable="stat failed", action="manual_review")
    r = post(client, f"/api/library/record/{blocked}/approve")
    assert r.status_code == 400 and r.get_json()["message"] == "Cannot read this file"
    assert post(client, "/api/library/record/9999/approve").status_code == 400
    assert post(client, f"/api/library/record/{a}/frobnicate").status_code == 404


def test_tested_it_needs_a_note_and_records_it(client, conn):
    record_id = add(conn, integrity=v.INT_DECODE_ERROR, audit_error="bad frames")
    r = post(client, f"/api/library/record/{record_id}/tested", {"note": "  "})
    assert r.status_code == 400 and "short note" in r.get_json()["message"]
    assert post(client, f"/api/library/record/{record_id}/tested", {"note": "played all of it"}).get_json()["ok"]
    assert rec_of(conn, record_id)["approval_note"] == "tested by hand: played all of it"


def test_a_name_that_cannot_be_used_is_refused_with_the_reason(client, conn):
    record_id = add(conn, name="Heat: Part 1 (1995).mkv", reasons=[("naming", "invalid characters")])
    assert "a valid name is needed" in client.get(f"/library/record/{record_id}").get_data(as_text=True)
    assert "needs a valid name" in post(client, f"/api/library/record/{record_id}/approve").get_json()["message"]
    assert "cannot be used" in post(client, f"/api/library/record/{record_id}/approve", {"name": "Heat: Two.mkv"}).get_json()["message"]
    assert post(client, f"/api/library/record/{record_id}/approve", {"name": "Heat - Part 1 (1995).mkv"}).get_json()["ok"]


# ---------------------------------------------------------------- bulk approval
def test_bulk_approval_asks_first_then_approves_only_what_qualifies_and_records_who(client, conn):
    ids = [add(conn, rel=f"{n}.mkv", name=f"{n.upper()} (2000).mkv", size=1024 ** 3) for n in "ab"]
    low = add(conn, rel="l.mkv", name="L (2000).mkv", audio=[track(1, "und")], english="unknown", reasons=[("audio", "English cannot be confirmed")])
    page = client.get("/library/bulk/ready?conf=medium").get_data(as_text=True)
    assert "approve <strong>2</strong> files (2.00 GB)" in page and "high and medium" in page and "confirmBulk" in page and page.count("<code>Movies/") == 2
    assert "Nothing qualifies" in client.get("/library/bulk/check?conf=high").get_data(as_text=True)
    r = post(client, "/api/library/bulk/ready", {"conf": "medium"})
    assert r.get_json() == {"ok": True, "done": 2, "skipped": 0, "message": "Approved 2 files."}
    assert {rec_of(conn, i)["approval_status"] for i in ids} == {"approved"} and rec_of(conn, low)["approval_status"] == "pending"
    assert rec_of(conn, ids[0])["approval_decided_by"] == "guest" and rec_of(conn, ids[0])["approval_note"] == "bulk approved on the Library page (ready)"
    assert post(client, "/api/library/bulk/check", {"conf": "low"}).get_json()["done"] == 0               # asking for low does not let low in
    assert post(client, "/api/library/bulk/nope").status_code == 404


def test_bulk_approval_can_be_narrowed_to_a_search_or_a_share(client, conn):
    a = add(conn, root="tfiles", rel="Show A/a.mkv", name="A (2000).mkv")
    b = add(conn, root="archive", rel="Other/b.mkv", name="B (2000).mkv")
    assert post(client, "/api/library/bulk/ready", {"conf": "medium", "root": "tfiles"}).get_json()["done"] == 1
    assert rec_of(conn, a)["approval_status"] == "approved" and rec_of(conn, b)["approval_status"] == "pending"
    assert post(client, "/api/library/bulk/ready", {"conf": "medium", "q": "OTHER/"}).get_json()["done"] == 1


# ---------------------------------------------------------------- the report
def test_the_csv_export_has_the_original_columns_and_follows_the_filters(client, conn):
    a = add(conn, root="tfiles", rel="Downloads/Heat.mkv", name="Heat (1995).mkv")
    b = add(conn, root="media", rel="Movies/Alien.mkv", name="Alien (1979).mkv", integrity=v.INT_DECODE_ERROR, audit_error="x")
    res = client.get("/library/export.csv")
    assert res.mimetype == "text/csv" and re.match(r'attachment; filename="library_export_all_\d{8}_\d{6}\.csv"', res.headers["Content-Disposition"])
    text = res.get_data(as_text=True)
    assert text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert sorted(r["Relative Path"] for r in rows) == ["Downloads/Heat.mkv", "Movies/Alien.mkv"] and "Suggested Clean Name" in rows[0] and "Approval Status" in rows[0]
    only = list(csv.DictReader(io.StringIO(client.get("/library/export.csv?group=needs_you").get_data(as_text=True).lstrip("﻿"))))
    assert [r["Relative Path"] for r in only] == ["Movies/Alien.mkv"]
    assert [r["Root"] for r in csv.DictReader(io.StringIO(client.get("/library/export.csv?root=tfiles").get_data(as_text=True).lstrip("﻿")))] == ["tfiles"]
    assert "library_export_needs_you_" in client.get("/library/export.csv?group=needs_you").headers["Content-Disposition"] and a and b
