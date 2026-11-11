"""Held files: low quality or damaged files are never converted, only renamed and moved; they live on the Below minimum quality page."""
import json

import pytest

from synchub import downloads, jobs, quality, settings, storage, versions
from synchub.library import migrate, recommend as rc
from synchub.library import vocab as v

from .library_helpers import CFG, add, plan
from .test_quality import NOW, clean_metrics, finish_job, fixture_file, greg_shares, rec_of


def low_score(conn, file_id):
    metrics = clean_metrics()
    metrics["slices"][0]["errors"] = 40
    finish_job(conn, quality.request(conn, file_id), metrics)


def convert_file(conn, rel="Old/Heat.avi", **kw):
    """A pending older-format film whose normal recommendation is a remux."""
    file_id = add(conn, root="tfiles", rel=rel, name="Heat (1995).mkv", container="avi", video="mpeg4", action="remux_copy", size=5_000_000_000,
                  proposals=[plan(convert={"remux": True}, keep_audio=(1,))], **kw)
    conn.execute("UPDATE library_file SET duration_seconds = 7200, resolution = '1920x1080', video_info_json = ?, bitrate_kbps = 8640 WHERE id = ?",
                 (json.dumps({"width": 1920, "height": 1080, "video_kbps": 8000, "hdr": "sdr", "bit_depth": 8}), file_id))
    conn.commit()
    return file_id


# ---------------------------------------------------------------- the held list
def test_a_file_is_held_for_resolution_score_or_damage_until_accepted(conn):
    small = fixture_file(conn, rel="Small (1999)/Small (1999).mkv", resolution="854x480", kbps=1500)
    scored = fixture_file(conn, rel="Scored (2000)/Scored (2000).mkv")
    damaged = fixture_file(conn, rel="Damaged (2001)/Damaged (2001).mkv")
    good = fixture_file(conn, rel="Good (2002)/Good (2002).mkv")
    low_score(conn, scored)
    finish_job(conn, quality.request(conn, good))
    errors = quality.request(conn, damaged, mode="damage")
    finish_job(conn, errors, {"mode": "damage", "errors": 3, "sample": ["x"]})
    assert quality.held_ids(conn) == {small, scored, damaged}
    assert quality.is_held(conn, small) and not quality.is_held(conn, good)
    quality.accept(conn, small)
    assert quality.held_ids(conn) == {scored, damaged}


def test_a_stale_check_does_not_hold_a_file_that_changed(conn):
    scored = fixture_file(conn)
    low_score(conn, scored)
    assert quality.held_ids(conn) == {scored}
    conn.execute("UPDATE library_file SET mtime = mtime + 9 WHERE id = ?", (scored,))
    assert quality.held_ids(conn) == set()


def test_the_flagged_list_can_be_searched_and_paged(conn):
    for i in range(3):
        fixture_file(conn, rel=f"Film{i} (200{i})/Film{i} (200{i}).mkv", resolution="854x480", kbps=1500)
    assert len(quality.low_quality(conn, 2)) == 2 and len(quality.low_quality(conn, 2, offset=2)) == 1
    assert [r["file_name"] for r in quality.low_quality(conn, match="film1")] == ["Film1 (2001).mkv"]
    assert quality.low_quality(conn, root="nowhere") == []


# ---------------------------------------------------------------- the recommendation
def test_a_held_file_gets_no_conversion_step_and_its_own_group(conn):
    file_id = convert_file(conn)
    rec = rec_of(conn, file_id)
    normal = rc.recommend(rec, CFG)
    held = rc.recommend(rec, CFG, held=True)
    assert normal.group == rc.G_CONVERT and normal.kind == "convert"
    assert held.group == rc.G_HELD and held.kind == "held" and held.can_approve and held.held
    assert held.headline == "Copy unchanged to the right folder" and held.action == v.ACTION_COPY_RENAME and not held.accept_tracks
    assert not any("Remux" in s or "Re-encode" in s or "Remove" in s for s in held.steps) and any("Held from conversion" in n for n in held.notes)


def test_a_held_file_already_on_the_destination_share_is_renamed_in_place(conn):
    file_id = add(conn, root="media", rel="Movies/Heat.1995.mkv", name="Heat (1995).mkv", size=5_000_000_000)
    held = rc.recommend(rec_of(conn, file_id), CFG, held=True)
    assert held.group == rc.G_HELD and held.mode == "rename_only" and held.headline.startswith("Rename in place")


def test_classify_moves_held_files_out_of_the_other_pages_but_leaves_queued_and_rejected(conn):
    held_id = convert_file(conn)
    queued = convert_file(conn, rel="Old/Other.avi")
    conn.execute("UPDATE library_file SET recommended_name = 'Other (1996).mkv' WHERE id = ?", (queued,))
    conn.commit()
    for fid in (held_id, queued):
        conn.execute("UPDATE library_file SET resolution = '854x480', video_info_json = ? WHERE id = ?", (json.dumps({"width": 854, "height": 480, "video_kbps": 1500, "hdr": "sdr", "bit_depth": 8}), fid))
    conn.commit()
    rc.reject(conn, queued, "Greg")
    groups = {rec["id"]: r.group for rec, r in rc.classify(conn, CFG)}
    assert groups[held_id] == rc.G_HELD and groups[queued] == rc.G_REJECTED
    table = rc.counts(conn, CFG)
    assert table[rc.G_HELD]["total"] == 1 and table[rc.G_CONVERT]["total"] == 0


def test_a_held_file_that_was_done_leaves_done(conn):
    file_id = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=5_000_000_000)
    conn.execute("UPDATE library_file SET resolution = '854x480', video_info_json = ? WHERE id = ?", (json.dumps({"width": 854, "height": 480, "video_kbps": 1500, "hdr": "sdr", "bit_depth": 8}), file_id))
    conn.commit()
    assert rc.recommend(rec_of(conn, file_id), CFG).group == rc.G_DONE
    assert dict((rec["id"], r.group) for rec, r in rc.classify(conn, CFG))[file_id] == rc.G_HELD


# ---------------------------------------------------------------- approving
def test_approving_a_held_file_makes_a_plain_copy_never_a_conversion(conn):
    file_id = convert_file(conn)
    conn.execute("UPDATE library_file SET resolution = '854x480', video_info_json = ? WHERE id = ?", (json.dumps({"width": 854, "height": 480, "video_kbps": 1500, "hdr": "sdr", "bit_depth": 8}), file_id))
    conn.commit()
    assert rc.apply_recommendation(conn, file_id, "Greg", cfg=CFG) == "approved"
    job = conn.execute("SELECT * FROM job WHERE ref_id = ? AND kind = 'library'", (file_id,)).fetchone()
    assert json.loads(job["plan"])["op"] == "copy" and "change" not in json.loads(job["plan"])


def test_a_held_file_refuses_track_changes_and_a_converting_profile(conn):
    from synchub.library import profiles
    file_id = convert_file(conn)
    conn.execute("UPDATE library_file SET resolution = '854x480', video_info_json = ? WHERE id = ?", (json.dumps({"width": 854, "height": 480, "video_kbps": 1500, "hdr": "sdr", "bit_depth": 8}), file_id))
    conn.commit()
    with pytest.raises(ValueError, match="held from conversion"):
        rc.apply_recommendation(conn, file_id, "Greg", cfg=CFG, keep_streams=[2])
    hevc = next(p for p in profiles.all_profiles(conn) if p["video_codec"] != "keep")
    with pytest.raises(ValueError, match="re-encoded"):
        rc.apply_recommendation(conn, file_id, "Greg", cfg=CFG, profile_id=hevc["id"])
    assert conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'library'").fetchone()[0] == 0


def test_a_conversion_request_for_a_held_record_is_still_a_copy_at_the_job_layer(conn):
    """Even if something approved a remux before the file became held, queueing it makes no conversion."""
    file_id = convert_file(conn)
    from synchub.library import store
    store.set_approval(conn, file_id, v.APPROVAL_APPROVED, "Greg", action="remux_copy", accept_track_changes=True)
    conn.execute("UPDATE library_file SET resolution = '854x480', video_info_json = ? WHERE id = ?", (json.dumps({"width": 854, "height": 480, "video_kbps": 1500, "hdr": "sdr", "bit_depth": 8}), file_id))
    conn.commit()
    migrate.queue(conn, file_id, CFG, "Greg")
    assert json.loads(conn.execute("SELECT plan FROM job WHERE kind = 'library'").fetchone()[0])["op"] == "copy"


def test_versions_and_downloads_skip_a_held_file(conn):
    file_id = fixture_file(conn, rel="Heat (1995)/Heat (1995).mkv", size=40_000_000_000, kbps=40000, codec="hevc", resolution="3840x2160")
    low_score(conn, file_id)
    out = versions.recommendations(conn)
    assert not out[versions.VARIANT] and not out[versions.DV_REMUX]
    with pytest.raises(versions.VersionError, match="held from conversion"):
        versions.approve(conn, versions.VARIANT, file_id, "Greg")


# ---------------------------------------------------------------- check first, then convert
@pytest.fixture
def check_first(monkeypatch):
    monkeypatch.setattr(quality, "check_first", lambda conn=None: True)


def approve_conversion(conn, file_id):
    rc.apply_recommendation(conn, file_id, "Greg", cfg=CFG)
    conn.commit()


def test_an_approved_conversion_waits_for_the_check_and_a_good_file_is_then_queued(conn, check_first):
    file_id = convert_file(conn)
    approve_conversion(conn, file_id)
    assert conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'library'").fetchone()[0] == 0
    assert quality.waiting_to_convert(conn, file_id)
    check = conn.execute("SELECT id FROM quality_check WHERE library_file_id = ?", (file_id,)).fetchone()[0]
    assert quality.get(conn, check)["source"] == "convert"
    finish_job(conn, check)
    quality.release_waiting(conn, CFG)
    assert not quality.waiting_to_convert(conn, file_id)
    job = conn.execute("SELECT plan FROM job WHERE kind = 'library'").fetchone()
    assert job and json.loads(job[0])["op"] == "convert"


def test_a_low_scoring_file_is_held_and_gets_no_conversion_job(conn, check_first):
    file_id = convert_file(conn)
    approve_conversion(conn, file_id)
    check = conn.execute("SELECT id FROM quality_check WHERE library_file_id = ?", (file_id,)).fetchone()[0]
    metrics = clean_metrics()
    metrics["slices"][0]["errors"] = 40
    finish_job(conn, check, metrics)
    assert quality.release_waiting(conn, CFG) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'library'").fetchone()[0] == 0
    rec = rec_of(conn, file_id)
    assert rec["approval_status"] == "pending" and quality.is_held(conn, file_id)
    assert dict((r["id"], g.group) for r, g in rc.classify(conn, CFG))[file_id] == rc.G_HELD


def test_a_check_that_fails_sends_the_file_back_instead_of_converting_it(conn, check_first):
    file_id = convert_file(conn)
    approve_conversion(conn, file_id)
    check = conn.execute("SELECT id FROM quality_check WHERE library_file_id = ?", (file_id,)).fetchone()[0]
    finish_job(conn, check, status="failed", error="boom")
    quality.release_waiting(conn, CFG)
    assert rec_of(conn, file_id)["approval_status"] == "pending" and conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'library'").fetchone()[0] == 0


def test_a_plain_copy_never_waits_for_a_check(conn, check_first):
    file_id = add(conn, root="tfiles", rel="Movies/Heat.1995.1080p.mkv", name="Heat (1995).mkv", size=5_000_000_000)
    approve_conversion(conn, file_id)
    assert conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'library'").fetchone()[0] == 1 and conn.execute("SELECT COUNT(*) FROM quality_check").fetchone()[0] == 0


def test_a_file_that_was_just_checked_converts_at_once(conn, check_first):
    file_id = convert_file(conn)
    conn.execute("UPDATE library_file SET analysis_status = 'completed' WHERE id = ?", (file_id,))
    finish_job(conn, quality.request(conn, file_id))
    approve_conversion(conn, file_id)
    assert conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'library'").fetchone()[0] == 1 and not quality.waiting_to_convert(conn, file_id)


# ---------------------------------------------------------------- the page
def test_the_below_minimum_quality_page_lists_filters_pages_and_organizes(client, conn):
    one = convert_file(conn, rel="Old/Heat.avi")
    conn.execute("UPDATE library_file SET resolution = '854x480', video_info_json = ? WHERE id = ?", (json.dumps({"width": 854, "height": 480, "video_kbps": 1500, "hdr": "sdr", "bit_depth": 8}), one))
    conn.commit()
    page = client.get("/library/quality").get_data(as_text=True)
    assert "Held: low quality" in page and "Heat (1995).mkv" in page and "Rename and move only" in page and "Copy unchanged to the right folder" in page
    assert "Heat (1995).mkv" not in client.get("/library/quality?q=zzz").get_data(as_text=True)
    assert "Heat (1995).mkv" not in client.get("/library/quality?root=media").get_data(as_text=True)
    assert client.get("/library/quality/export").get_data(as_text=True).count("\n") >= 2
    assert client.get("/library/queue/held").status_code == 302
    done = client.post(f"/api/library/record/{one}/approve").get_json()
    assert done["ok"] and json.loads(conn.execute("SELECT plan FROM job WHERE kind = 'library'").fetchone()[0])["op"] == "copy"
    assert client.post("/api/library/quality/accept", json={"file_id": one}).get_json()["ok"]
    assert "Nothing is flagged" in client.get("/library/quality").get_data(as_text=True)


def test_the_library_page_no_longer_lists_a_held_file_in_format_conversion(client, conn):
    one = convert_file(conn)
    assert "Format conversion" in client.get("/library").get_data(as_text=True)
    conn.execute("UPDATE library_file SET resolution = '854x480', video_info_json = ? WHERE id = ?", (json.dumps({"width": 854, "height": 480, "video_kbps": 1500, "hdr": "sdr", "bit_depth": 8}), one))
    conn.commit()
    queue = client.get("/library/queue/convert").get_data(as_text=True)
    assert "Heat (1995).mkv" not in queue
    assert "Held: low quality" in client.get(f"/library/record/{one}").get_data(as_text=True)


# ---------------------------------------------------------------- Your copies
def link_film(conn, title, year, items):
    media_id = conn.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('movie', ?, ?, ?)", (title, year, str(year))).lastrowid
    for i, path in enumerate(items):
        key = f"{title}-{i}"
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, first_seen, last_seen, path) VALUES (?, 'movie', ?, ?, ?, ?)", (key, title, NOW, NOW, path))
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', ?, 'x', 1.0)", (media_id, key))
    conn.commit()
    return media_id


def test_your_copies_finds_every_jellyfin_item_and_any_file_with_the_same_name(conn):
    greg_shares(conn)
    title = "Borat Cultural Learnings of America for Make Benefit Glorious Nation of Kazakhstan"
    media_id = link_film(conn, title, 2006, [f"/media/BORAT/{title} (2006).mkv", f"/media/{title} (2006)/Borat (2006) [1080p].mp4"])
    first = add(conn, root="media", rel=f"Movies/BORAT/{title} (2006).mkv", name="Borat (2006).mkv", size=2_000_000_000)
    second = add(conn, root="media", rel=f"Movies/{title} (2006)/Borat (2006) [1080p].mp4", name="Borat (2006).mp4", size=3_000_000_000)
    out = quality.copies(conn, media_id)
    assert {f["id"] for f in out["files"]} == {first, second} and out["unmapped"] == []


def test_your_copies_finds_a_file_by_name_wherever_it_sits_and_names_a_path_it_could_not_map(conn):
    greg_shares(conn)
    media_id = link_film(conn, "Heat", 1995, ["/nowhere/Heat (1995).mkv"])
    stray = add(conn, root="tfiles", rel="Downloads/Heat (1995) 1080p.mkv", name="Heat (1995).mkv", size=2_000_000_000)
    add(conn, root="tfiles", rel="Downloads/Heated (1995).mkv", name="Heated (1995).mkv", size=2_000_000_000)
    out = quality.copies(conn, media_id)
    assert [f["id"] for f in out["files"]] == [stray] and out["files"][0]["found_by"] == "name" and out["unmapped"] == ["/nowhere/Heat (1995).mkv"]


def test_the_mapping_that_names_a_folder_wins_when_two_use_the_same_jellyfin_path(conn):
    storage.create(conn, {"name": "tfiles", "protocol": "local", "share": "/mnt/tfiles", "mount_point": "/mnt/tfiles", "jellyfin_path": "/media2", "jellyfin_extra_paths": "/media2 = Movies"})
    assert storage.from_app_path(storage.list_all(conn), "/media2/A Ghost Story.mkv") == ("tfiles", "Movies/A Ghost Story.mkv")
    assert "/media2 is used twice" in storage.mapping_warning(conn)
    conn.execute("UPDATE storage_root SET jellyfin_path = NULL")
    assert storage.mapping_warning(conn) == ""


def test_the_newest_check_lookup_uses_the_covering_index(conn):
    """On a real database SQLite chose the status index and read every finished check once per file (36 s for 1,500 files)."""
    for i in range(30):
        low_score(conn, fixture_file(conn, rel=f"F{i} (2000)/F{i} (2000).mkv"))
    plan_rows = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT library_file_id, MAX(id) FROM quality_check WHERE mode = 'quality' AND status = 'done' GROUP BY library_file_id"))
    assert "ix_quality_mode_status_file" in plan_rows


def test_held_ids_can_show_a_recent_list_to_the_timer(conn):
    from synchub.library import cache
    cache.clear()
    first = fixture_file(conn, rel="One (2000)/One (2000).mkv", resolution="854x480", kbps=1500)
    assert quality.held_ids(conn) == {first}
    second = fixture_file(conn, rel="Two (2000)/Two (2000).mkv", resolution="854x480", kbps=1500)
    assert quality.held_ids(conn, max_age=30) == {first}              # the timer does not wait for a new calculation
    assert quality.held_ids(conn) == {first, second}                  # a person always sees the current list
