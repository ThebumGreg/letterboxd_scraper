"""Conversion profiles, rename only, a cancel that stays cancelled, deleting sources later, telling Radarr/Sonarr, and the Dashboard stages of a job."""
import json
import os

import pytest

from synchub import jobs, pipeline_events, settings, states
from synchub.library import cleanup, migrate, profiles, recommend as rc, store
from synchub.library import vocab as v

from .library_helpers import CFG, add, plan
from .test_agent_live import make_avi, needs_ffmpeg, probe, rig, sha, wait          # noqa: F401  (rig is a fixture)
from .test_library_findings import rec_of
from .test_library_migrate import approve, job_of

KEEP_ALL = {"op": "keep_tracks", "container": "matroska"}


def seeded(conn, **kw):
    record_id = add(conn, **kw)
    return record_id, store.get_record(conn, record_id)


# ---------------------------------------------------------------- profiles
def test_the_built_in_profiles_exist_and_cannot_be_changed(conn):
    names = [p["name"] for p in profiles.all_profiles(conn)]
    assert names == ["Standard", "Keep all subtitles", "Move only", "HEVC smaller", "HEVC balanced", "HEVC high quality"]
    assert profiles.default(conn)["name"] == "Standard"
    with pytest.raises(profiles.ProfileError, match="cannot be changed"):
        profiles.save(conn, {"name": "Standard x"}, "Greg", 1)
    with pytest.raises(profiles.ProfileError, match="cannot be deleted"):
        profiles.delete(conn, 1)


def test_a_custom_profile_is_checked_saved_and_used_as_a_default(conn):
    pid = profiles.save(conn, {"name": "Phone", "video_codec": "hevc", "crf": "28", "preset": "fast", "ten_bit": True, "audio_kbps": "128", "subtitles": "keep_all"}, "Greg")
    p = profiles.get(conn, pid)
    assert (p["video_codec"], p["crf"], p["preset"], p["ten_bit"], p["audio_kbps"], p["subtitles"], p["builtin"]) == ("hevc", 28, "fast", True, 128, "keep_all", False)
    assert profiles.for_plan(p) == {"name": "Phone", "video_codec": "hevc", "crf": 28, "preset": "fast", "ten_bit": True, "audio_kbps": 128}
    for bad, why in (({"name": ""}, "name"), ({"name": "x", "video_codec": "av1"}, "HEVC or H.264"), ({"name": "x", "video_codec": "hevc", "crf": "50"}, "between 14 and 35"),
                     ({"name": "x", "video_codec": "hevc", "preset": "warp"}, "speed"), ({"name": "x", "audio_kbps": "8"}, "between 64"), ({"name": "phone"}, "already a profile")):
        with pytest.raises(profiles.ProfileError, match=why):
            profiles.save(conn, bad, "Greg")
    profiles.save(conn, {"name": "Phone", "video_codec": "h264", "crf": "24"}, "Greg", pid)
    assert profiles.get(conn, pid)["video_codec"] == "h264" and profiles.get(conn, pid)["ten_bit"] is False
    settings.set_value("library_default_profile", str(pid), conn=conn)
    with pytest.raises(profiles.ProfileError, match="default profile"):
        profiles.delete(conn, pid)
    settings.set_value("library_default_profile", "1", conn=conn)
    profiles.delete(conn, pid)
    assert profiles.get(conn, pid) is None


def test_move_only_forces_the_other_fields_to_do_nothing(conn):
    pid = profiles.save(conn, {"name": "Untouched", "video_codec": "hevc", "crf": "20", "move_only": True}, "Greg")
    p = profiles.get(conn, pid)
    assert p["move_only"] and p["video_codec"] == "keep" and p["subtitles"] == "keep_all" and p["crf"] is None


# ---------------------------------------------------------------- what a profile does to a plan
def approved_record(conn, profile_id=None, mode=None, **kw):
    record_id = add(conn, **kw)
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "Greg", action=v.ACTION_COPY_RENAME if not kw.get("proposals") else None, accept_track_changes=bool(kw.get("proposals")),
                       profile_id=profile_id, mode=mode)
    conn.commit()
    return record_id


TRACKS = dict(audio=None, subtitles=None)


def subs_record(conn, **kw):
    from .library_helpers import track
    audio = [track(1), track(2, "ita", default=False)]
    subs = [track(3, "eng", "subrip", "subtitle", default=False), track(4, "fra", "subrip", "subtitle", default=False)]
    proposal = plan(remove_audio=[(2, "ita")], remove_subtitle=[(4, "fra")], keep_audio=(1,), keep_subtitle=(3,), default_audio=1)
    return approved_record(conn, rel="Movies/Heat.1995.mkv", name="Heat (1995).mkv", action="copy_rename", audio=audio, subtitles=subs, proposals=[proposal], **kw)


def plan_of(conn, record_id, profile_id=None):
    rec = store.get_record(conn, record_id)
    return migrate.plan_for(rec, CFG, profiles.get(conn, profile_id or 1))


def test_standard_removes_other_languages_and_keep_all_subtitles_puts_them_back(conn):
    record_id = subs_record(conn)
    _, rel, standard = plan_of(conn, record_id, 1)
    assert standard["op"] == "convert" and standard["change"]["keep_subtitle"] == [3] and "profile" not in standard
    _, _, keep = plan_of(conn, record_id, 2)
    assert keep["op"] == "convert" and keep["change"]["keep_subtitle"] == [3, 4] and keep["change"]["remove_subtitle"] == [] and keep["change"]["remove_audio"]


def test_keep_all_subtitles_alone_makes_the_plan_a_plain_copy(conn):
    from .library_helpers import track
    proposal = plan(remove_subtitle=[(4, "fra")], keep_audio=(1,), keep_subtitle=(3,))
    record_id = approved_record(conn, rel="Movies/Heat.mkv", name="Heat (1995).mkv", action="copy_rename", subtitles=[track(3, "eng", "subrip", "subtitle", default=False), track(4, "fra", "subrip", "subtitle", default=False)],
                                proposals=[proposal])
    assert plan_of(conn, record_id, 1)[2]["op"] == "convert"
    assert plan_of(conn, record_id, 2)[2]["op"] == "copy"


def test_move_only_copies_the_file_exactly_even_when_a_conversion_was_suggested(conn):
    record_id = approved_record(conn, rel="Old/Heat.avi", name="Heat (1995).mkv", container="avi", video="mpeg4", action="remux_copy", proposals=[plan(convert={"remux": True}, keep_audio=(1,))])
    root, rel, p = plan_of(conn, record_id, 3)
    assert (root, rel, p["op"]) == ("media", "Movies/Heat (1995)/Heat (1995).avi", "copy") and "change" not in p


def test_an_hevc_profile_converts_the_video_and_needs_an_agent_that_knows_profiles(conn):
    record_id = approved_record(conn, rel="Movies/Heat.mkv", name="Heat (1995).mkv", action="copy_rename", video="h264")
    _, rel, p = plan_of(conn, record_id, 5)
    assert p["op"] == "convert" and rel == "Movies/Heat (1995)/Heat (1995).mkv" and p["change"]["video"] == {"action": "transcode", "from": "h264", "codec": "hevc"}
    assert p["profile"] == {"name": "HEVC balanced", "video_codec": "hevc", "crf": 23, "preset": "medium", "ten_bit": True, "audio_kbps": 192} and p["needs"] == ["profiles"]
    assert p["change"]["container"] == "matroska"


def test_a_file_that_is_already_hevc_is_not_encoded_again_and_a_legacy_container_becomes_mkv(conn):
    done = approved_record(conn, rel="Movies/A.mkv", name="A (2000).mkv", action="copy_rename", video="hevc")
    assert plan_of(conn, done, 5)[2]["op"] == "copy"
    old = approved_record(conn, rel="Old/B.avi", name="B (2001).mkv", action="copy_rename", video="mpeg4", container="avi")
    root, rel, p = plan_of(conn, old, 5)
    assert p["op"] == "convert" and rel.endswith("B (2001).mkv") and p["change"]["video"]["action"] == "transcode" and p["change"]["container"] == "matroska"


def test_a_job_that_needs_profiles_only_goes_to_an_agent_that_lists_them():
    job = {"plan": {"op": "convert", "needs": ["profiles", "libx265"]}}
    old = {"capabilities": {"ops": ["copy", "convert"], "encoders": ["libx265"]}}
    new = {"capabilities": {"ops": ["copy", "convert"], "features": ["profiles"], "encoders": ["libx265"]}}
    no_encoder = {"capabilities": {"ops": ["copy", "convert"], "features": ["profiles"], "encoders": []}}
    assert not jobs.can_run(old, job) and jobs.can_run(new, dict(job)) and not jobs.can_run(no_encoder, job)
    assert jobs.can_run({"capabilities": {}}, job)                                         # an agent that lists nothing is trusted, as before


def test_the_queued_job_carries_the_chosen_profile_and_the_default_is_used_otherwise(conn):
    a = approved_record(conn, rel="Movies/A.mkv", name="A (2000).mkv", action="copy_rename", video="h264", profile_id=4)
    assert migrate.queue(conn, a, CFG) and jobs.get(conn, job_of(conn, a))["plan"]["profile"]["name"] == "HEVC smaller"
    settings.set_value("library_default_profile", "6", conn=conn)
    b = approved_record(conn, rel="Movies/B.mkv", name="B (2001).mkv", action="copy_rename", video="h264")
    migrate.queue(conn, b, CFG)
    assert jobs.get(conn, job_of(conn, b))["plan"]["profile"]["name"] == "HEVC high quality"


# ---------------------------------------------------------------- cancel stays cancelled
def test_a_cancelled_job_stays_cancelled_until_it_is_queued_again(conn):
    record_id = approved_record(conn, rel="Movies/A.mkv", name="A (2000).mkv", action="copy_rename")
    job_id = migrate.queue(conn, record_id, CFG)
    jobs.cancel(conn, job_id, "Greg")
    migrate.sync(conn, job_id)
    rec = rec_of(conn, record_id)
    assert rec["migration_status"] == "pending" and rec["migration_cancelled_at"] and "stays cancelled" in rec["migration_last_error"]
    assert migrate.queue_approved(conn, CFG) == (0, []) and migrate.queue(conn, record_id, CFG) is None
    assert migrate.queue_summary(conn)["cancelled"] == 1 and migrate.queue_summary(conn)["unqueued"] == 0
    assert [a["id"] for a in migrate.attention(conn)] == [record_id]
    again = migrate.requeue(conn, record_id, CFG, "Greg")
    assert again != job_id and rec_of(conn, record_id)["migration_cancelled_at"] is None and migrate.attention(conn) == []


def test_a_new_decision_or_a_retry_clears_the_cancelled_mark(conn):
    record_id = approved_record(conn, rel="Movies/A.mkv", name="A (2000).mkv", action="copy_rename")
    job_id = migrate.queue(conn, record_id, CFG)
    jobs.cancel(conn, job_id, "Greg")
    migrate.sync(conn, job_id)
    jobs.retry(conn, job_id, "Greg")                                                       # the Workers page retry
    migrate.sync(conn, job_id)
    assert rec_of(conn, record_id)["migration_cancelled_at"] is None and rec_of(conn, record_id)["migration_status"] == "pending"
    jobs.cancel(conn, job_id, "Greg")
    migrate.sync(conn, job_id)
    store.set_approval(conn, record_id, v.APPROVAL_PENDING, "Greg")
    assert rec_of(conn, record_id)["migration_cancelled_at"] is None


def test_a_failed_file_is_not_queued_again_by_the_bulk_button(conn):
    record_id = approved_record(conn, rel="Movies/A.mkv", name="A (2000).mkv", action="copy_rename")
    job_id = migrate.queue(conn, record_id, CFG)
    conn.execute("UPDATE job SET status = 'failed', error = 'disk', finished_at = datetime('now') WHERE id = ?", (job_id,))
    migrate.sync(conn, job_id)
    assert rec_of(conn, record_id)["migration_status"] == "failed" and migrate.queue_approved(conn, CFG) == (0, [])
    assert migrate.requeue(conn, record_id, CFG) != job_id


# ---------------------------------------------------------------- rename only
def test_a_file_on_the_destination_share_at_the_wrong_path_is_suggested_a_rename(conn):
    record_id = add(conn, root="media", rel="Movies/Heat.1995.1080p.mkv", name="Heat (1995).mkv")
    r = rc.recommend(store.get_record(conn, record_id), CFG)
    assert (r.group, r.kind, r.mode, r.can_approve) == ("suggested", "rename", "rename_only", True) and "Nothing is copied" in r.steps[0]
    assert rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG) == "approved"
    rec = rec_of(conn, record_id)
    assert rec["approved_mode"] == "rename_only"
    job = jobs.get(conn, job_of(conn, record_id))
    assert job["plan"]["op"] == "rename" and (job["source_root"], job["dest_root"], job["dest_rel"]) == ("media", "media", "Movies/Heat (1995)/Heat (1995).mkv") and job["need_bytes"] == 0


def test_any_share_can_be_renamed_in_place_and_it_stays_on_that_share(conn):
    record_id = add(conn, root="tfiles", rel="Downloads/Heat.mkv", name="Heat (1995).mkv")
    assert rc.recommend(store.get_record(conn, record_id), CFG).mode is None                # the usual suggestion is still to bring it into the library
    rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG, mode="rename_only")
    job = jobs.get(conn, job_of(conn, record_id))
    assert (job["plan"]["op"], job["source_root"], job["dest_root"], job["dest_rel"]) == ("rename", "tfiles", "tfiles", "Movies/Heat (1995)/Heat (1995).mkv")


def test_a_tv_episode_is_renamed_into_its_season_folder(conn):
    record_id = add(conn, root="media", rel="TV Shows/Show/Show.S01E02.mkv", name="Show - S01E02.mkv", media_type="TV Episode", show="Show", season=1)
    rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG)
    assert jobs.get(conn, job_of(conn, record_id))["dest_rel"] == "TV Shows/Show/Season 01/Show - S01E02.mkv"


def test_renaming_is_refused_for_a_file_already_in_place_and_a_rename_keeps_the_extension(conn):
    placed = add(conn, root="media", rel="Movies/Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")
    with pytest.raises(ValueError, match="Already in the library"):
        rc.apply_recommendation(conn, placed, "Greg", cfg=CFG, mode="rename_only")
    old = add(conn, root="media", rel="Old/Heat.avi", name="Heat (1995).mkv", container="avi", video="mpeg4", action="remux_copy", proposals=[plan(convert={"remux": True}, keep_audio=(1,))])
    rc.apply_recommendation(conn, old, "Greg", cfg=CFG, mode="rename_only")
    assert jobs.get(conn, job_of(conn, old))["dest_rel"] == "Movies/Heat (1995)/Heat (1995).avi"


def test_choosing_copy_overrides_the_rename_the_recommendation_suggested(conn):
    record_id = add(conn, root="media", rel="Movies/Heat.mkv", name="Heat (1995).mkv")
    rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG, mode="copy", profile_id=3)
    job = jobs.get(conn, job_of(conn, record_id))
    assert job["plan"]["op"] == "copy" and rec_of(conn, record_id)["approved_mode"] is None and rec_of(conn, record_id)["approved_profile_id"] == 3


@needs_ffmpeg
def test_a_worker_renames_the_file_with_its_subtitles_and_the_record_follows(rig, conn):                      # noqa: F811
    source = rig.roots["media"] / "Movies/Heat.1995.mkv"
    make_avi(source)
    (source.parent / "Heat.1995.en.srt").write_text("1\n")
    (source.parent / "Heat.1995.1080p.srt").write_text("not ours\n")
    stat = source.stat()
    record_id = add(conn, root="media", rel="Movies/Heat.1995.mkv", name="Heat (1995).mkv", size=stat.st_size, mtime=stat.st_mtime)
    approve(conn, record_id)
    rig.start()
    wait(lambda: (conn.commit() or True) and rec_of(conn, record_id)["migration_status"] == "completed", timeout=60, what="the rename")
    folder = rig.roots["media"] / "Movies/Heat (1995)"
    assert (folder / "Heat (1995).mkv").exists() and (folder / "Heat (1995).en.srt").exists() and not source.exists()
    assert (source.parent / "Heat.1995.1080p.srt").exists()                                       # another file's subtitle is not taken
    rec = rec_of(conn, record_id)
    assert rec["relative_path"] == "Movies/Heat (1995)/Heat (1995).mkv" and rec["file_name"] == "Heat (1995).mkv" and rec["migrated_how"] == "rename"
    assert rec["path_key"] == v.record_key("media", "Movies/Heat (1995)/Heat (1995).mkv") and rec["source_delete_status"] is None
    assert rc.recommend(rec, CFG).group == "done"


@needs_ffmpeg
def test_a_rename_refuses_to_overwrite_and_a_changed_file_is_not_renamed(rig, conn):                       # noqa: F811
    source = rig.roots["media"] / "Movies/Heat.mkv"
    make_avi(source)
    stat = source.stat()
    taken = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"someone else")
    record_id = add(conn, root="media", rel="Movies/Heat.mkv", name="Heat (1995).mkv", size=stat.st_size, mtime=stat.st_mtime)
    approve(conn, record_id)
    rig.start()
    job_id = job_of(conn, record_id)
    job = wait(lambda: (conn.commit() or True) and jobs.get(conn, job_id)["status"] in ("failed", "needs_review") and jobs.get(conn, job_id), timeout=60, what="the refusal")
    assert job["error_class"] == "dest_exists" and source.exists() and taken.read_bytes() == b"someone else"


# ---------------------------------------------------------------- HEVC on a real worker
@needs_ffmpeg
def test_a_worker_converts_with_an_hevc_profile(rig, conn):                                              # noqa: F811
    source = rig.roots["tfiles"] / "Old/Heat.avi"
    make_avi(source, audio_tracks=1)
    record_id = add(conn, rel="Old/Heat.avi", name="Heat (1995).mkv", container="avi", video="mpeg4", action="remux_copy", size=source.stat().st_size,
                    proposals=[plan(convert={"remux": True, "video": "transcode"}, keep_audio=(1,))])
    conn.execute("UPDATE library_file SET duration_seconds = 2.0 WHERE id = ?", (record_id,))
    rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG, profile_id=5)
    conn.commit()
    rig.start()
    wait(lambda: (conn.commit() or True) and rec_of(conn, record_id)["migration_status"] in ("completed", "failed", "manual_review"), timeout=120, what="the conversion")
    rec = rec_of(conn, record_id)
    assert rec["migration_status"] == "completed", rec["migration_last_error"]
    streams = probe(rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv")["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert video["codec_name"] == "hevc" and video["pix_fmt"] == "yuv420p10le"


# ---------------------------------------------------------------- the source is deleted later
def finished_copy(conn, days="7", rel="Downloads/Heat.mkv", name="Heat (1995).mkv", **kw):
    """A copy that a worker finished and verified (done by hand), with the source deletion set to `days`."""
    settings.set_value("source_delete_after_days", days, conn=conn)
    record_id = approved_record(conn, root="tfiles", rel=rel, name=name, action="copy_rename", size=1000, **kw)
    job_id = migrate.queue(conn, record_id, CFG)
    conn.execute("UPDATE job SET status = 'running', worker_id = NULL WHERE id = ?", (job_id,))
    jobs_done(conn, job_id)
    return record_id, job_id


def jobs_done(conn, job_id, result=None):
    conn.execute("UPDATE job SET status = 'done', stage = 'done', result = ?, finished_at = datetime('now') WHERE id = ?", (json.dumps(result or {"op": "copy", "bytes": 1000, "sha256": "ab" * 32, "verified": "sha256"}), job_id))
    migrate.sync(conn, job_id)


def test_a_verified_copy_arms_the_source_deletion_for_the_chosen_number_of_days(conn):
    record_id, _ = finished_copy(conn, "14")
    rec = rec_of(conn, record_id)
    assert rec["migration_status"] == "completed" and rec["migrated_dest_size"] == 1000 and rec["migrated_dest_sha256"] == "ab" * 32 and rec["migrated_how"] == "copy"
    assert rec["source_delete_status"] == "waiting" and rec["source_delete_due_at"] > rec["migration_finished_at"] and "14 days" in rec["source_delete_message"]


def test_nothing_is_armed_while_deletion_is_off_or_for_a_rename_or_an_in_place_replacement(conn):
    off, _ = finished_copy(conn, "0")
    assert rec_of(conn, off)["source_delete_status"] is None and cleanup.sweep(conn) == 0
    settings.set_value("source_delete_after_days", "7", conn=conn)
    renamed = approved_record(conn, root="media", rel="Movies/B.mkv", name="B (2001).mkv", action="copy_rename", mode="rename_only")
    job_id = migrate.queue(conn, renamed, CFG)
    jobs_done(conn, job_id, {"op": "rename", "bytes": 5, "verified": "size"})
    assert rec_of(conn, renamed)["source_delete_status"] is None


def test_the_deletion_job_is_queued_only_when_the_time_is_up_and_the_setting_is_still_on(conn):
    record_id, _ = finished_copy(conn, "7")
    assert cleanup.sweep(conn) == 0
    conn.execute("UPDATE library_file SET source_delete_due_at = datetime('now', '-1 minute') WHERE id = ?", (record_id,))
    settings.set_value("source_delete_after_days", "0", conn=conn)
    assert cleanup.sweep(conn) == 0 and rec_of(conn, record_id)["source_delete_status"] == "waiting"      # turning it Off stops every pending deletion
    settings.set_value("source_delete_after_days", "30", conn=conn)
    assert cleanup.sweep(conn) == 1 and cleanup.sweep(conn) == 0
    job = conn.execute("SELECT * FROM job WHERE ref_type = 'library_source'").fetchone()
    p = json.loads(job["plan"])
    assert (job["kind"], job["source_root"], job["source_rel"], job["dest_root"], job["dest_rel"]) == ("library", "tfiles", "Downloads/Heat.mkv", "media", "Movies/Heat (1995)/Heat (1995).mkv")
    assert p["op"] == "delete_source" and p["dest_size"] == 1000 and p["dest_sha256"] == "ab" * 32 and p["source_size"] == 1000
    assert rec_of(conn, record_id)["source_delete_status"] == "queued"


def test_the_record_follows_the_deletion_job_and_a_changed_source_is_never_swept(conn):
    record_id, _ = finished_copy(conn, "7")
    conn.execute("UPDATE library_file SET source_delete_due_at = datetime('now', '-1 minute') WHERE id = ?", (record_id,))
    cleanup.sweep(conn)
    job_id = conn.execute("SELECT id FROM job WHERE ref_type = 'library_source'").fetchone()[0]
    conn.execute("UPDATE job SET status = 'running' WHERE id = ?", (job_id,))
    assert cleanup.sync(conn, job_id) is False and rec_of(conn, record_id)["source_delete_status"] == "queued"
    conn.execute("UPDATE job SET status = 'done', result = '{\"deleted\": true}' WHERE id = ?", (job_id,))
    assert cleanup.sync(conn, job_id) is True
    rec = rec_of(conn, record_id)
    assert rec["source_delete_status"] == "deleted" and rec["source_deleted_at"] and rec["migration_status"] == "completed"       # the deletion job never rewrites the migration
    other, _ = finished_copy(conn, "7", rel="Downloads/Third.mkv", name="Third (1997).mkv")                                                          # a file that changed since is not swept
    conn.execute("UPDATE library_file SET source_delete_due_at = datetime('now', '-1 minute'), source_revision = source_revision + 1 WHERE id = ?", (other,))
    assert cleanup.sweep(conn) == 0


def test_a_person_can_keep_a_source_and_start_the_timer_again(conn):
    record_id, _ = finished_copy(conn, "7")
    cleanup.hold(conn, record_id, "Greg")
    conn.execute("UPDATE library_file SET source_delete_due_at = datetime('now', '-1 day') WHERE id = ?", (record_id,))
    assert rec_of(conn, record_id)["source_delete_status"] == "held" and cleanup.sweep(conn) == 0
    cleanup.release(conn, record_id, "Greg")
    assert rec_of(conn, record_id)["source_delete_status"] == "waiting" and rec_of(conn, record_id)["source_delete_due_at"] > "2000"
    settings.set_value("source_delete_after_days", "0", conn=conn)
    cleanup.hold(conn, record_id, "Greg")
    with pytest.raises(ValueError, match="Off"):
        cleanup.release(conn, record_id, "Greg")


@needs_ffmpeg
def test_a_worker_deletes_the_source_after_checking_the_library_copy(rig, conn):                          # noqa: F811
    settings.set_value("source_delete_after_days", "7", conn=conn)
    source = rig.roots["tfiles"] / "Downloads/Heat.mkv"
    make_avi(source)
    stat = source.stat()
    record_id = add(conn, root="tfiles", rel="Downloads/Heat.mkv", name="Heat (1995).mkv", size=stat.st_size, mtime=stat.st_mtime)
    approve(conn, record_id)
    rig.start()
    wait(lambda: (conn.commit() or True) and rec_of(conn, record_id)["migration_status"] == "completed", timeout=60, what="the copy")
    dest = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv"
    assert dest.exists() and source.exists() and rec_of(conn, record_id)["source_delete_status"] == "waiting"
    conn.execute("UPDATE library_file SET source_delete_due_at = datetime('now', '-1 minute') WHERE id = ?", (record_id,))
    conn.commit()
    wait(lambda: (conn.commit() or True) and rec_of(conn, record_id)["source_delete_status"] == "deleted", timeout=60, what="the deletion")
    assert not source.exists() and dest.exists() and not (source.parent).exists()                  # the emptied folder goes too, the share stays
    assert rec_of(conn, record_id)["migration_status"] == "completed"


@needs_ffmpeg
def test_a_worker_keeps_the_source_when_the_library_copy_is_damaged_or_the_source_changed(rig, conn):    # noqa: F811
    settings.set_value("source_delete_after_days", "7", conn=conn)
    source = rig.roots["tfiles"] / "Downloads/Heat.mkv"
    make_avi(source)
    stat = source.stat()
    record_id = add(conn, root="tfiles", rel="Downloads/Heat.mkv", name="Heat (1995).mkv", size=stat.st_size, mtime=stat.st_mtime)
    approve(conn, record_id)
    rig.start()
    wait(lambda: (conn.commit() or True) and rec_of(conn, record_id)["migration_status"] == "completed", timeout=60, what="the copy")
    dest = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv"
    with open(dest, "r+b") as handle:
        handle.write(b"XXXX")                                                                       # the library copy is damaged, same size
    conn.execute("UPDATE library_file SET source_delete_due_at = datetime('now', '-1 minute') WHERE id = ?", (record_id,))
    conn.commit()
    job = wait(lambda: (conn.commit() or True) and (r := conn.execute("SELECT * FROM job WHERE ref_type = 'library_source'").fetchone()) and r["error_class"] and r, timeout=60, what="the refusal")
    assert job["error_class"] == "verification_failed" and "source was kept" in job["error"] and source.exists()


# ---------------------------------------------------------------- Radarr and Sonarr
class FakeRadarr:
    def __init__(self, path):
        self.calls, self.movie = [], {"id": 7, "title": "Heat", "path": path, "monitored": True}

    def movies(self):
        return [self.movie]

    def update_movie(self, movie_id, body, move_files=False):
        self.calls.append(("update", movie_id, body["path"], body["monitored"], move_files))

    def delete_movie(self, movie_id, delete_files=False):
        self.calls.append(("delete", movie_id, delete_files))

    def rescan_movie(self, movie_id):
        self.calls.append(("rescan", movie_id))


class FakeSonarr:
    def __init__(self, path):
        self.calls, self.show = [], {"id": 3, "title": "Show", "path": path}

    def series(self):
        return [self.show]

    def episodes(self, series_id):
        return [{"id": 31, "seasonNumber": 1, "episodeNumber": 2}, {"id": 32, "seasonNumber": 1, "episodeNumber": 3}]

    def update_series(self, series_id, body, move_files=None):
        self.calls.append(("update", series_id, body["path"], move_files))

    def monitor_episodes(self, ids, monitored=True):
        self.calls.append(("monitor", ids, monitored))

    def rescan_series(self, series_id):
        self.calls.append(("rescan", series_id))


def ctx(**kw):
    base = {"service": "radarr", "how": "copy", "mode": "update_location", "title": "Heat", "source": "/downloads/Heat/Heat.mkv", "dest": "/movies/Heat (1995)/Heat (1995).mkv", "episode": None, "others_in_old_folder": 0}
    return {**base, **kw}


def test_radarr_is_told_the_new_folder_without_moving_files_and_then_rescans():
    radarr = FakeRadarr("/downloads/Heat")
    assert cleanup.run_arr(ctx(), radarr=radarr)[0] == "done"
    assert radarr.calls == [("update", 7, "/movies/Heat (1995)", True, False), ("rescan", 7)]


def test_radarr_can_stop_monitoring_or_forget_the_movie_without_deleting_files():
    radarr = FakeRadarr("/downloads/Heat")
    assert cleanup.run_arr(ctx(mode="unmonitor"), radarr=radarr)[0] == "done" and radarr.calls == [("update", 7, "/downloads/Heat", False, False)]
    radarr = FakeRadarr("/downloads/Heat")
    status, message = cleanup.run_arr(ctx(mode="remove"), radarr=radarr)
    assert status == "done" and radarr.calls == [("delete", 7, False)] and "No file was deleted" in message


def test_a_rename_always_passes_on_the_new_location_whatever_the_mode():
    radarr = FakeRadarr("/movies/Heat")
    cleanup.run_arr(ctx(how="rename", mode="remove", source="/movies/Heat/Heat.mkv"), radarr=radarr)
    assert radarr.calls == [("update", 7, "/movies/Heat (1995)", True, False), ("rescan", 7)]


def test_arr_steps_are_skipped_with_a_reason_when_they_cannot_be_done():
    assert cleanup.run_arr(ctx(dest=None), radarr=FakeRadarr("/x"))[0] == "skipped"
    status, message = cleanup.run_arr(ctx(), radarr=FakeRadarr("/elsewhere"))
    assert status == "skipped" and "does not have a movie" in message

    class Down(FakeRadarr):
        def movies(self):
            raise RuntimeError("Radarr is down")
    assert cleanup.run_arr(ctx(), radarr=Down("/x")) == ("failed", "Radarr is down")


def test_sonarr_moves_the_series_folder_only_when_no_other_episode_is_left_behind():
    base = dict(service="sonarr", source="/downloads/Show/Season 01/Show - S01E02.mkv", dest="/tv/Show/Season 01/Show - S01E02.mkv", episode=(1, 2))
    sonarr = FakeSonarr("/downloads/Show")
    assert cleanup.run_arr(ctx(**base), sonarr=sonarr)[0] == "done" and sonarr.calls == [("update", 3, "/tv/Show", False), ("rescan", 3)]
    sonarr = FakeSonarr("/downloads/Show")
    status, message = cleanup.run_arr(ctx(others_in_old_folder=2, **base), sonarr=sonarr)
    assert status == "skipped" and "2 other episode(s)" in message and sonarr.calls == []
    sonarr = FakeSonarr("/tv/Show")                                                               # the folder is already right: just rescan
    cleanup.run_arr(ctx(**base), sonarr=sonarr)
    assert sonarr.calls == [("rescan", 3)]


def test_sonarr_unmonitors_one_episode_and_says_what_remove_means_for_tv():
    base = dict(service="sonarr", source="/downloads/Show/Show - S01E02.mkv", dest=None, episode=(1, 2), mode="remove")
    sonarr = FakeSonarr("/downloads/Show")
    status, message = cleanup.run_arr(ctx(**base), sonarr=sonarr)
    assert status == "done" and sonarr.calls == [("monitor", [31], False)] and "without deleting its file" in message


def test_the_arr_step_is_due_right_after_the_copy_or_waits_for_the_source_deletion(conn):
    settings.set_value("arr_after_migration", "update_location", conn=conn)
    now, due = finished_copy(conn, "7")
    assert rec_of(conn, now)["arr_status"] == "due"
    settings.set_value("arr_apply_when", "with_source_delete", conn=conn)
    later, job_id = finished_copy(conn, "7", rel="Downloads/Other.mkv", name="Other (1996).mkv")
    assert rec_of(conn, later)["arr_status"] == "waiting"
    conn.execute("UPDATE library_file SET source_delete_due_at = datetime('now', '-1 minute') WHERE id = ?", (later,))
    cleanup.sweep(conn)
    delete_job = conn.execute("SELECT id FROM job WHERE ref_type = 'library_source' AND ref_id = ?", (later,)).fetchone()[0]
    conn.execute("UPDATE job SET status = 'done', result = '{}' WHERE id = ?", (delete_job,))
    cleanup.sync(conn, delete_job)
    assert rec_of(conn, later)["arr_status"] == "due"
    settings.set_value("source_delete_after_days", "0", conn=conn)
    never = approved_record(conn, root="tfiles", rel="Downloads/N.mkv", name="N (2002).mkv", action="copy_rename")
    jobs_done(conn, migrate.queue(conn, never, CFG))
    assert rec_of(conn, never)["arr_status"] == "skipped" and "source deletion is off" in rec_of(conn, never)["arr_message"]


# ---------------------------------------------------------------- the Dashboard stages
def dashboard_movie(conn, title="Heat", year=1995):
    movie_id = conn.execute("INSERT INTO media (kind, title, year) VALUES ('movie', ?, ?)", (title, year)).lastrowid
    states.compute(conn)
    return movie_id


def state_of(conn, movie_id):
    row = conn.execute("SELECT state_code FROM item_state WHERE target_type = 'movie' AND target_id = ?", (movie_id,)).fetchone()
    return row[0] if row else None


def test_a_titles_dashboard_state_follows_its_job_stage_by_stage(conn):
    movie_id = dashboard_movie(conn)
    record_id = approved_record(conn, rel="Old/Heat.avi", name="Heat (1995).mkv", container="avi", video="mpeg4", action="remux_copy", proposals=[plan(convert={"remux": True}, keep_audio=(1,))])
    job_id = migrate.queue(conn, record_id, CFG)
    assert pipeline_events.update_for_job(conn, job_id) == set() and state_of(conn, movie_id) == "missing"           # still waiting for a worker
    seen = []
    for stage, expected in (("claimed", "transferring"), ("copy_in", "transferring"), ("analyze", "working"), ("encode", "encoding"), ("copy_out", "transferring_library"), ("verify", "transferring_library")):
        conn.execute("UPDATE job SET status = 'running', stage = ?, progress = 0.5 WHERE id = ?", (stage, job_id))
        pipeline_events.update_for_job(conn, job_id)
        seen.append(state_of(conn, movie_id))
        assert seen[-1] == expected, stage
    labels = {r["code"]: r["label"] for r in conn.execute("SELECT code, label FROM state_def")}
    assert [labels[c] for c in ("transferring", "working", "encoding", "transferring_library", "in_library")] == ["Transferring to Worker", "Working", "Converting", "Transferring to Library", "In Library"]
    conn.execute("UPDATE job SET status = 'done', stage = 'done' WHERE id = ?", (job_id,))
    assert pipeline_events.update_for_job(conn, job_id) and state_of(conn, movie_id) == "missing"           # the event closes; Jellyfin's sync says In Library


def test_a_job_on_a_title_already_in_the_library_shows_its_stage_then_goes_back(conn):
    movie_id = dashboard_movie(conn)
    conn.execute("UPDATE item_state SET state_code = 'in_library' WHERE target_type = 'movie' AND target_id = ?", (movie_id,))
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name) VALUES ('j1', 'movie', 'Heat')") if False else None
    record_id = approved_record(conn, rel="Movies/Heat.mkv", name="Heat (1995).mkv", action="copy_rename", video="h264", profile_id=5)
    job_id = migrate.queue(conn, record_id, CFG)
    conn.execute("UPDATE job SET status = 'running', stage = 'encode' WHERE id = ?", (job_id,))
    pipeline_events.update_for_job(conn, job_id)
    assert state_of(conn, movie_id) == "encoding"
    event = conn.execute("SELECT detail FROM processing_event WHERE provider = 'synchub_job'").fetchone()
    assert event["detail"] == pipeline_events.WAS_IN_LIBRARY


def test_a_stage_is_closed_when_its_job_stops_running_and_unmatched_titles_show_nothing(conn):
    movie_id = dashboard_movie(conn)
    record_id = approved_record(conn, rel="Movies/Heat.mkv", name="Heat (1995).mkv", action="copy_rename")
    job_id = migrate.queue(conn, record_id, CFG)
    conn.execute("UPDATE job SET status = 'running', stage = 'copy_in' WHERE id = ?", (job_id,))
    pipeline_events.update_for_job(conn, job_id)
    assert state_of(conn, movie_id) == "transferring"
    conn.execute("UPDATE job SET status = 'queued', stage = NULL WHERE id = ?", (job_id,))          # the worker went silent and the job went back
    assert pipeline_events.close_stale(conn) and state_of(conn, movie_id) == "missing"
    stranger = approved_record(conn, rel="Movies/Nobody.mkv", name="Nobody Known (1999).mkv", action="copy_rename")
    other = migrate.queue(conn, stranger, CFG)
    conn.execute("UPDATE job SET status = 'running', stage = 'copy_in' WHERE id = ?", (other,))
    assert pipeline_events.update_for_job(conn, other) == set()


def test_a_deletion_or_scan_job_never_shows_on_the_dashboard(conn):
    dashboard_movie(conn)
    job_id = jobs.create(conn, "scan", title="Scan", plan={"op": "scan"})
    conn.execute("UPDATE job SET status = 'running', stage = 'scan' WHERE id = ?", (job_id,))
    assert pipeline_events.update_for_job(conn, job_id) == set()


def test_the_job_lifecycle_has_one_label_per_step():
    labels = {(s, st): jobs.lifecycle({"status": s, "stage": st})[1] for s, st in (("queued", None), ("running", "claimed"), ("running", "copy_in"), ("running", "analyze"), ("running", "encode"),
                                                                              ("running", "copy_out"), ("done", "done"), ("needs_review", "plan"), ("failed", "encode"), ("cancelled", None))}
    assert list(labels.values()) == ["Queued", "Assigned to a worker", "Transferring to worker", "Working", "Converting", "Transferring to library", "Completed", "Needs you", "Failed", "Cancelled"]


# ---------------------------------------------------------------- the worker's command
def test_the_ffmpeg_command_follows_the_profile_and_refuses_values_it_does_not_know():
    from .agent_module import agent
    change = {"op": "convert", "container": "matroska", "keep_audio": [1], "video": {"action": "transcode", "codec": "hevc"}, "audio_convert": {"1": {"codec": "aac"}}}
    hevc = {"video_codec": "hevc", "crf": 23, "preset": "slow", "ten_bit": True, "audio_kbps": 256}
    command = " ".join(agent.build_convert_command("ffmpeg", "/in.avi", "/out.partial", change, hevc))
    assert "-c:v libx265 -crf 23 -preset slow -pix_fmt yuv420p10le -tag:v hvc1" in command and "-b:a:0 256k" in command
    assert "-c:v libx264 -crf 20 -preset medium -pix_fmt yuv420p" in " ".join(agent.build_convert_command("ffmpeg", "/in.avi", "/o", {**change, "video": {"action": "transcode"}})) and "-b:a:0 192k" in command.replace("256k", "192k")
    eight = " ".join(agent.build_convert_command("ffmpeg", "/in.avi", "/o", change, {**hevc, "ten_bit": False}))
    assert "yuv420p " in eight + " " and "yuv420p10le" not in eight
    plain = " ".join(agent.build_convert_command("ffmpeg", "/in.avi", "/o", {**change, "video": {"action": "copy"}}, hevc))
    assert "-c:v copy" in plain and "libx265" not in plain                                          # a profile never re-encodes video the plan keeps
    for bad in ({**hevc, "crf": 99}, {**hevc, "preset": "x; rm -rf /"}, {**hevc, "video_codec": "av1"}, {**hevc, "audio_kbps": 5}, {**hevc, "crf": "abc"}):
        with pytest.raises(agent.JobFailure) as failure:
            agent.build_convert_command("ffmpeg", "/in.avi", "/o", change, bad)
        assert failure.value.error_class == "bad_job" and failure.value.retryable is False
    assert agent.clean_profile(None) is None and agent.clean_profile({"video_codec": "keep"})["audio_kbps"] == 192


# ---------------------------------------------------------------- the pages
def web_shares(conn):
    from .test_library_web import shares
    shares(conn)


def test_profiles_are_made_and_deleted_through_the_page_and_the_settings_check_the_default(client, conn):
    from .test_library_web import post
    web_shares(conn)
    r = post(client, "/api/library/profiles", {"name": "Phone", "video_codec": "hevc", "crf": "28", "preset": "fast", "ten_bit": True, "audio_kbps": "128", "subtitles": "keep_all"})
    assert r.get_json()["ok"] and profiles.get(conn, 7)["name"] == "Phone"
    assert not post(client, "/api/library/profiles", {"name": "Bad", "video_codec": "hevc", "crf": "99"}).get_json()["ok"]
    assert not post(client, "/api/library/profiles", {"id": 1, "name": "Standard 2"}).get_json()["ok"]
    assert post(client, "/api/library/settings", {"library_default_profile": "7", "source_delete_after_days": "14", "arr_after_migration": "unmonitor", "arr_apply_when": "with_source_delete"}).get_json()["ok"]
    assert (settings.get("library_default_profile"), settings.get("source_delete_after_days"), settings.get("arr_after_migration"), settings.get("arr_apply_when")) == ("7", "14", "unmonitor", "with_source_delete")
    for bad in ({"library_default_profile": "99"}, {"source_delete_after_days": "5"}, {"arr_after_migration": "delete"}, {"arr_apply_when": "now"}):
        assert not post(client, "/api/library/settings", bad).get_json()["ok"]
    assert not post(client, "/api/library/profiles/7/delete").get_json()["ok"]                       # it is the default
    assert post(client, "/api/library/settings", {"library_default_profile": "1"}).get_json()["ok"]
    assert post(client, "/api/library/profiles/7/delete").get_json()["ok"] and profiles.get(conn, 7) is None


def test_the_library_page_lists_profiles_settings_and_files_that_stopped(client, conn):
    from .test_library_web import post
    web_shares(conn)
    record_id = approved_record(conn, rel="Movies/A.mkv", name="A (2000).mkv", action="copy_rename")
    job_id = migrate.queue(conn, record_id, CFG)
    jobs.cancel(conn, job_id, "Greg")
    migrate.sync(conn, job_id)
    conn.commit()
    page = client.get("/library").get_data(as_text=True)
    assert "Conversion profiles" in page and "HEVC balanced" in page and "built in" in page and 'id="setSourceDelete"' in page and 'id="setArr"' in page
    assert "1 cancelled" in page and f'data-requeue="{record_id}"' in page
    r = post(client, f"/api/library/record/{record_id}/requeue")
    assert r.get_json() == {"ok": True, "message": "Queued again."} and rec_of(conn, record_id)["migration_cancelled_at"] is None


def test_the_record_page_offers_the_handling_choice_and_shows_what_happens_after(client, conn):
    from .test_library_web import post
    web_shares(conn)
    record_id = add(conn, root="media", rel="Movies/Heat.mkv", name="Heat (1995).mkv")
    page = client.get(f"/library/record/{record_id}").get_data(as_text=True)
    assert "How should it be handled?" in page and "Rename in place" in page and "Movies/Heat (1995)/Heat (1995).mkv" in page and 'id="profilePick"' in page and 'id="hRename" value="rename" checked' in page
    assert post(client, f"/api/library/record/{record_id}/approve", {"mode": "copy", "profile_id": "4"}).get_json()["ok"]
    assert rec_of(conn, record_id)["approved_profile_id"] == 4
    other, _ = finished_copy(conn, "7", rel="Downloads/Z.mkv", name="Zed (2001).mkv")
    conn.commit()
    page = client.get(f"/library/record/{other}").get_data(as_text=True)
    assert "After the migration" in page and "Keep the source" in page
    assert post(client, f"/api/library/record/{other}/keep-source").get_json()["ok"] and rec_of(conn, other)["source_delete_status"] == "held"
    assert post(client, f"/api/library/record/{other}/release-source").get_json()["ok"] and rec_of(conn, other)["source_delete_status"] == "waiting"


def test_the_jobs_list_shows_the_lifecycle_label_not_the_raw_status(client, conn):
    web_shares(conn)
    job_id = jobs.create(conn, "library", title="Convert: Heat", plan={"op": "convert"})
    conn.execute("UPDATE job SET status = 'running', stage = 'encode', progress = 0.4 WHERE id = ?", (job_id,))
    conn.commit()
    page = client.get("/workers").get_data(as_text=True)
    assert "Converting" in page


# ---------------------------------------------------------------- showing and redoing a decision
def test_an_approved_record_lists_what_the_job_removes_for_each_track(conn):
    from synchub.web.library import describe_record
    record_id = subs_record(conn)
    rec = store.get_record(conn, record_id)
    tracks = {s["index"]: s["state"] for s in describe_record(rec, rc.recommend(rec, CFG), CFG, profiles.get(conn, 1))["streams"]}
    assert tracks[2] == "removed" and tracks[4] == "removed" and tracks[1] == "kept" and tracks[3] == "kept"
    tracks = {s["index"]: s["state"] for s in describe_record(rec, rc.recommend(rec, CFG), CFG, profiles.get(conn, 2))["streams"]}
    assert tracks[4] == "kept" and tracks[2] == "removed"             # keep every subtitle puts the French one back
    assert describe_record(rec, rc.recommend(rec, CFG), CFG, profiles.get(conn, 2))["streams"][0]["remove"] is False


def test_redo_takes_a_file_back_to_pending_and_ignores_the_old_job(conn):
    record_id = subs_record(conn)
    job_id = migrate.queue(conn, record_id, CFG)
    migrate.redo(conn, record_id, "Greg")
    rec = rec_of(conn, record_id)
    assert rec["approval_status"] == "pending" and rec["approved_track_changes_json"] is None and rec["migration_status"] == "pending"
    assert jobs.get(conn, job_id)["status"] == "cancelled" and migrate.active_job(conn, record_id) is None
    assert migrate.queue(conn, record_id, CFG) is None                  # not approved any more
    store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "Greg", accept_track_changes=True, profile_id=2)
    assert migrate.queue(conn, record_id, CFG) is not None


def test_redo_of_a_finished_copy_needs_the_copy_removed_and_never_without_the_source(conn):
    record_id = subs_record(conn)
    job_id = migrate.queue(conn, record_id, CFG)
    jobs_done(conn, job_id, {"verified": "size", "bytes": 5})
    migrate.sync(conn, job_id)
    assert rec_of(conn, record_id)["migration_status"] == "completed"
    with pytest.raises(ValueError, match="remove that copy first"):
        migrate.redo(conn, record_id, "Greg")
    conn.execute("UPDATE library_file SET source_delete_status = 'deleted' WHERE id = ?", (record_id,))
    with pytest.raises(ValueError, match="nothing left"):
        migrate.redo(conn, record_id, "Greg", dest_removed=True)
    conn.execute("UPDATE library_file SET source_delete_status = NULL WHERE id = ?", (record_id,))
    migrate.redo(conn, record_id, "Greg", dest_removed=True)
    assert rec_of(conn, record_id)["migration_status"] == "pending" and migrate.sync_active(conn) == 0
    assert rec_of(conn, record_id)["migration_status"] == "pending"     # the old finished job does not bring it back


def test_the_record_page_shows_removed_tracks_after_approval_and_can_redo(client, conn):
    from .test_library_web import post
    web_shares(conn)
    record_id = subs_record(conn)
    conn.commit()
    page = client.get(f"/library/record/{record_id}").get_data(as_text=True)
    assert page.count('badge bg-danger">removed') == 2 and "Do it again with other settings" in page and 'data-remove=' not in page
    r = post(client, f"/api/library/record/{record_id}/redo", {})
    assert r.get_json()["ok"] and rec_of(conn, record_id)["approval_status"] == "pending"
    page = client.get(f"/library/record/{record_id}").get_data(as_text=True)
    assert 'data-remove="2"' in page and "Do it again with other settings" not in page
