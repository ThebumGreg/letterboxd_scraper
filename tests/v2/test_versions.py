"""Extra versions of films and Dolby Vision 7 to 8.1: what is recommended, what approving queues, and how the row follows its job."""
import json

import pytest

from synchub import jobs, settings, versions
from synchub.library import recommend as rc

from .library_helpers import CFG, add, track
from .test_library_findings import rec_of

GIB = 1024 ** 3
REL = "Movies/Heat (1995)/Heat (1995) Bluray-2160p.mkv"
FOUR_K = {"hdr": "hdr10", "bit_depth": 10, "width": 3840, "height": 2160, "transfer": "smpte2084"}
DV7 = {"hdr": "dv", "dv_profile": 7, "dv_compat": 6, "bit_depth": 10, "width": 3840, "height": 2160, "transfer": "smpte2084"}


def film(conn, rel=REL, info=FOUR_K, size_gb=60, name="Heat (1995).mkv", audio=None, subtitles=(), media_type="Movie/Unknown", **kw):
    """A scanned film on the destination share, with the picture facts a worker records."""
    audio = audio if audio is not None else [track(1, codec="truehd", channels=8), track(2, codec="ac3", channels=6, language="fra")]
    record_id = add(conn, root="media", rel=rel, name=name, size=int(size_gb * GIB), audio=audio, subtitles=subtitles, media_type=media_type, **kw)
    conn.execute("UPDATE library_file SET video_info_json = ?, resolution = ?, duration_seconds = 7200, video_codec = 'hevc' WHERE id = ?",
                 (json.dumps(info), f"{info['width']}x{info['height']}", record_id))
    conn.commit()
    return record_id


def kinds(conn):
    found = versions.recommendations(conn)
    return {k: [e["id"] for e in found[k]] for k in ("variant", "dv_remux", "declined")}


def finish_job(conn, job_id, status="done", result=None, error=None):
    conn.execute("UPDATE job SET status = ?, result = ?, error = ?, finished_at = datetime('now') WHERE id = ?", (status, json.dumps(result or {}), error, job_id))
    return versions.sync(conn, job_id)


# ---------------------------------------------------------------- what is recommended
def test_a_big_4k_film_is_recommended_a_smaller_copy_and_nothing_else_is(conn):
    big = film(conn)
    film(conn, rel="Movies/Small (2000)/Small (2000).mkv", size_gb=5)                                       # under the size limit
    film(conn, rel="Movies/Hd (2001)/Hd (2001).mkv", info={**FOUR_K, "width": 1920, "height": 1080})        # already 1080p
    film(conn, rel="TV Shows/Show/Season 01/S01E01.mkv", media_type="TV Episode", show="Show", season=1)     # films only
    film(conn, rel="Movies/Heat (1995)/Heat (1995) - 1080p.mkv", name="Heat (1995).mkv")                     # an extra version is not copied again
    assert kinds(conn) == {"variant": [big], "dv_remux": [], "declined": []}


def test_the_size_limit_and_the_height_come_from_the_settings(conn):
    big = film(conn, size_gb=20)
    assert kinds(conn)["variant"] == []
    settings.set_value("variant_min_gb", "10", conn=conn)
    assert kinds(conn)["variant"] == [big]
    settings.set_value("variant_height", "2160", conn=conn)
    assert kinds(conn)["variant"] == []                                                                     # the film is not taller than the copy would be
    settings.set_value("versions_on", "0", conn=conn)
    settings.set_value("variant_height", "1080", conn=conn)
    assert kinds(conn)["variant"] == []


def test_a_film_that_is_not_in_a_folder_named_like_it_cannot_get_a_copy(conn):
    film(conn, rel="Movies/Heat.mkv")
    (entry,) = versions.recommendations(conn)["variant"]
    assert entry["can_approve"] is False and "folder" in entry["why_not"]


def test_dolby_vision_profile_5_is_never_offered_a_copy_and_profile_7_is_offered_the_conversion(conn):
    five = film(conn, info={**DV7, "dv_profile": 5}, rel="Movies/Five (2001)/Five (2001).mkv")
    seven = film(conn, info=DV7, rel="Movies/Seven (2002)/Seven (2002).mkv")
    found = versions.recommendations(conn)
    assert [e["id"] for e in found["dv_remux"]] == [seven]
    assert {e["id"]: e["can_approve"] for e in found["variant"]} == {five: False, seven: True}
    settings.set_value("dv7_remux", "0", conn=conn)
    assert kinds(conn)["dv_remux"] == []


def test_a_file_named_like_another_version_is_left_alone_by_the_library(conn):
    film(conn, rel="Movies/Heat (1995)/Heat (1995) - 1080p.mkv", name="Heat (1995).mkv", size_gb=5)
    rec = rec_of(conn, 1)
    assert rc.recommend(rec, CFG).kind == "version" and rc.recommend(rec, CFG).group == rc.G_DONE


# ---------------------------------------------------------------- approving
def test_approving_a_copy_queues_a_job_beside_the_original_with_a_scaled_hevc_profile(conn):
    record_id = film(conn, subtitles=[track(3, codec="subrip", kind="subtitle", language="eng"), track(4, codec="hdmv_pgs_subtitle", kind="subtitle", language="eng")])
    job_id = versions.approve(conn, "variant", record_id, "Greg")
    job = jobs.get(conn, job_id)
    assert (job["kind"], job["ref_type"], job["source_root"], job["source_rel"]) == ("library", "library_action", "media", REL)
    assert job["dest_rel"] == "Movies/Heat (1995)/Heat (1995) - 1080p.mkv"
    plan = job["plan"]
    assert plan["op"] == "convert" and "replace" not in plan and set(plan["needs"]) == {"profiles", "scale"} and plan["variant"] == {"label": "1080p", "height": 1080}
    assert plan["profile"]["video_codec"] == "hevc" and plan["profile"]["max_height"] == 1080 and plan["profile"]["maxrate_kbps"] == 8000 and plan["profile"]["ten_bit"] is True
    change = plan["change"]
    assert change["video"]["action"] == "transcode" and change["keep_audio"] == [1] and change["default_audio"] == 1 and change["keep_subtitle"] == [3]      # English audio only, text subtitles only
    assert change["audio_convert"] == {"1": {"codec": "aac", "channels": 2}}
    row = versions.get(conn, 1)
    assert (row["kind"], row["status"], row["job_id"], row["label"]) == ("variant", "queued", job_id, "1080p")
    assert kinds(conn)["variant"] == []                                                                     # it is on its way: not recommended again


def test_the_copy_keeps_surround_sound_when_stereo_is_off_and_converts_only_what_phones_cannot_play(conn):
    settings.set_value("variant_stereo", "0", conn=conn)
    record_id = film(conn, audio=[track(1, codec="dts", channels=8), track(2, codec="ac3", channels=6, language="fra")])
    plan = jobs.get(conn, versions.approve(conn, "variant", record_id, "Greg"))["plan"]
    assert plan["change"]["keep_audio"] == [1] and plan["change"]["audio_convert"] == {"1": {"codec": "aac", "channels": 6}}


def test_a_person_can_choose_another_height_for_one_film(conn):
    record_id = film(conn)
    plan = jobs.get(conn, versions.approve(conn, "variant", record_id, "Greg", height=720))["plan"]
    assert plan["profile"]["max_height"] == 720 and plan["variant"]["height"] == 720
    with pytest.raises(versions.VersionError):
        versions.approve(conn, "variant", film(conn, rel="Movies/B (2000)/B (2000).mkv"), "Greg", height=100)


def test_approving_the_conversion_replaces_the_file_in_place_and_needs_a_worker_with_the_tools(conn):
    record_id = film(conn, info=DV7)
    job = jobs.get(conn, versions.approve(conn, "dv_remux", record_id, "Greg", with_copy=False))
    plan = job["plan"]
    assert job["dest_rel"] == REL and plan["replace"] is True and plan["needs"] == ["dovi"] and plan["change"]["video"] == {"action": "copy", "dv": "p8_1"}
    assert plan["source_size"] == 60 * GIB and plan["originals_retention_days"] == 30 and job["policy"] == {"originals_retention_days": 30}


def test_a_conversion_of_an_mp4_ends_in_mkv(conn):
    record_id = film(conn, info=DV7, rel="Movies/Heat (1995)/Heat (1995).mp4")
    job = jobs.get(conn, versions.approve(conn, "dv_remux", record_id, "Greg"))
    assert job["dest_rel"] == "Movies/Heat (1995)/Heat (1995).mkv" and job["plan"]["change"]["target_extension"] == ".mkv"


def test_the_same_film_cannot_be_queued_twice_or_by_two_kinds_at_once(conn):
    record_id = film(conn, info=DV7)
    versions.approve(conn, "variant", record_id, "Greg")
    with pytest.raises(versions.VersionError, match="already queued"):
        versions.approve(conn, "variant", record_id, "Greg")
    with pytest.raises(versions.VersionError, match="Another job"):
        versions.approve(conn, "dv_remux", record_id, "Greg")


def test_a_film_that_does_not_qualify_is_refused_with_the_reason(conn):
    record_id = film(conn, info={**DV7, "dv_profile": 5})
    with pytest.raises(versions.VersionError, match="profile 5"):
        versions.approve(conn, "variant", record_id, "Greg")
    with pytest.raises(versions.VersionError, match="does not apply|not something"):
        versions.approve(conn, "dv_remux", record_id, "Greg")


def test_bulk_approval_queues_what_it_can_and_says_what_it_could_not(conn):
    good, bad = film(conn), film(conn, rel="Movies/Heat.mkv")
    done, skipped = versions.approve_many(conn, "variant", [{"id": good}, {"id": bad}, {"id": 999}], "Greg")
    assert done == 1 and [i for i, _ in skipped] == [bad, 999]
    other = film(conn, rel="Movies/Ronin (1998)/Ronin (1998).mkv")
    assert versions.approve_recommended(conn, "variant", "Greg") == (1, [])
    assert jobs.get(conn, versions.get(conn, 2)["job_id"])["source_rel"].endswith("Ronin (1998).mkv") and other


# ---------------------------------------------------------------- declining
def test_a_declined_film_stays_off_the_list_until_it_is_brought_back(conn):
    record_id = film(conn)
    versions.skip(conn, "variant", record_id, "Greg")
    assert kinds(conn) == {"variant": [], "dv_remux": [], "declined": [record_id]}
    versions.restore(conn, "variant", record_id)
    assert kinds(conn)["variant"] == [record_id]


def test_a_film_being_worked_on_cannot_be_declined(conn):
    record_id = film(conn)
    versions.approve(conn, "variant", record_id, "Greg")
    with pytest.raises(versions.VersionError):
        versions.skip(conn, "variant", record_id, "Greg")


# ---------------------------------------------------------------- following the job
def test_the_row_follows_its_job_to_done_and_says_so_once(conn):
    record_id = film(conn)
    job_id = versions.approve(conn, "variant", record_id, "Greg")
    conn.execute("UPDATE job SET status = 'running' WHERE id = ?", (job_id,))
    versions.sync(conn, job_id)
    assert versions.get(conn, 1)["status"] == "converting"
    assert finish_job(conn, job_id, result={"bytes": 6 * GIB}) is True
    row = versions.get(conn, 1)
    assert row["status"] == "done" and "6.0 GB" in row["message"] and row["finished_at"]
    assert versions.sync(conn, job_id) is False
    with pytest.raises(versions.VersionError, match="already done"):
        versions.approve(conn, "variant", record_id, "Greg")


def test_a_failed_job_shows_its_reason_and_the_film_comes_back_with_a_retry_note(conn):
    record_id = film(conn)
    job_id = versions.approve(conn, "variant", record_id, "Greg")
    assert finish_job(conn, job_id, "failed", error="ffmpeg stopped") is False
    assert versions.get(conn, 1)["status"] == "failed" and "ffmpeg stopped" in versions.get(conn, 1)["message"]
    (entry,) = versions.recommendations(conn)["variant"]
    assert "ffmpeg stopped" in entry["retry"]
    versions.approve(conn, "variant", record_id, "Greg")                                                    # try again
    assert versions.get(conn, 1)["status"] == "queued" and versions.get(conn, 1)["job_id"] != job_id


def test_a_cancelled_job_counts_as_failed_so_it_can_be_tried_again(conn):
    job_id = versions.approve(conn, "dv_remux", film(conn, info=DV7), "Greg")
    jobs.cancel(conn, job_id, "Greg")
    versions.sync(conn, job_id)
    assert versions.get(conn, 1)["status"] == "failed" and "Cancelled" in versions.get(conn, 1)["message"]


def test_a_job_of_another_kind_is_ignored_by_sync(conn):
    other = jobs.create(conn, "library", title="x", plan={"op": "copy"})
    assert versions.sync(conn, other) is False


# ---------------------------------------------------------------- the page
def test_the_versions_tab_renders_and_its_buttons_queue_and_decline(client, conn):
    big = film(conn)
    seven = film(conn, info=DV7, rel="Movies/Seven (2002)/Seven (2002).mkv")
    page = client.get("/versions")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Smaller extra copy" in html and "Dolby Vision 7 to 8.1" in html and "Heat (1995)" in html
    sent = client.post("/api/library/versions/approve", json={"kind": "variant", "items": [{"id": big, "height": 720}]})
    assert sent.get_json()["ok"] and sent.get_json()["queued"] == 1
    assert client.post("/api/library/versions/skip", json={"kind": "dv_remux", "id": seven}).get_json()["ok"]
    assert client.post("/api/library/versions/approve", json={"kind": "nope", "items": [{"id": 1}]}).status_code == 400
    assert client.post("/api/library/versions/approve", json={"kind": "variant", "items": []}).status_code == 400
    page = client.get("/versions").get_data(as_text=True)
    assert "Declined (1)" in page and "720p" in page


def test_the_settings_are_checked_and_saved(client, conn):
    assert client.post("/api/library/versions/settings", json={"variant_height": "10"}).status_code == 400
    assert client.post("/api/library/versions/settings", json={"variant_label": "a/b"}).status_code == 400
    ok = client.post("/api/library/versions/settings", json={"variant_height": "720", "variant_label": "720p", "variant_stereo": "0", "dv7_remux": "0"})
    assert ok.get_json()["ok"] and settings.get("variant_height") == "720" and settings.get("dv7_remux") == "0"


# ---------------------------------------------------------------- one job, two outputs
def test_a_dolby_vision_job_carries_the_smaller_copy_as_a_second_output(conn):
    record_id = film(conn, info=DV7, size_gb=60)
    job_id = versions.approve(conn, "dv_remux", record_id, "Greg")
    conn.commit()
    plan = jobs.get(conn, job_id)["plan"]
    assert plan["extra"]["rel"].endswith("Heat (1995) - 1080p.mkv") and plan["extra"]["height"] == 1080 and plan["extra"]["profile"]["max_height"] == 1080
    assert {"dovi", "scale", "two_outputs"} <= set(plan["needs"])
    rows = {(r["kind"], r["label"]): r for r in conn.execute("SELECT * FROM library_action")}
    assert rows[("variant", "1080p")]["job_id"] == job_id == rows[("dv_remux", "")]["job_id"]
    assert kinds(conn)["variant"] == []                                                         # the copy is not offered a second time


def test_the_smaller_copy_can_be_left_out_of_the_conversion(conn):
    record_id = film(conn, info=DV7, size_gb=60)
    job_id = versions.approve(conn, "dv_remux", record_id, "Greg", with_copy=False)
    assert "extra" not in jobs.get(conn, job_id)["plan"] and conn.execute("SELECT COUNT(*) FROM library_action WHERE kind = 'variant'").fetchone()[0] == 0


def test_both_rows_follow_the_job_and_a_missing_copy_is_reported_not_hidden(conn):
    record_id = film(conn, info=DV7, size_gb=60)
    job_id = versions.approve(conn, "dv_remux", record_id, "Greg")
    conn.commit()
    finish_job(conn, job_id, result={"kept_original": True, "extra": {"bytes": 5 * GIB, "rel": "x"}})
    rows = {r["kind"]: dict(r) for r in conn.execute("SELECT * FROM library_action")}
    assert rows["dv_remux"]["status"] == "done" and rows["variant"]["status"] == "done" and "5.0 GB" in rows["variant"]["message"]
    other = film(conn, rel="Movies/Alien (1979)/Alien (1979).mkv", name="Alien (1979).mkv", info=DV7, size_gb=60)
    job2 = versions.approve(conn, "dv_remux", other, "Greg")
    conn.commit()
    finish_job(conn, job2, result={"kept_original": True, "extra": {"error": "not enough room"}})
    rows = {(r["path_key"], r["kind"]): dict(r) for r in conn.execute("SELECT * FROM library_action")}
    second = [v for k, v in rows.items() if "alien" in k[0].lower()]
    assert {r["kind"]: r["status"] for r in second} == {"dv_remux": "done", "variant": "failed"} and "not enough room" in next(r for r in second if r["kind"] == "variant")["message"]
