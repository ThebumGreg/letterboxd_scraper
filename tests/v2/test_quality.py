"""Quality checks: the scoring rules, how a check follows its job, the Library pass, the movie page's copies, the buttons and the worker's measuring."""
import json
import shutil
import subprocess

import pytest

from synchub import downloads, jobs, quality, settings, storage, workers

from .agent_module import agent
from .library_helpers import add

NOW = "2026-01-01 00:00:00"


def fixture_file(conn, rel="Heat (1995)/Heat (1995).mkv", size=5_000_000_000, kbps=8000, codec="h264", resolution="1920x1080", **extra):
    """A scanned, analysed film that is long enough to be sampled."""
    info = {"width": int(resolution.split("x")[0]), "height": int(resolution.split("x")[1]), "video_kbps": kbps, "hdr": extra.pop("hdr", "sdr"), "bit_depth": 8}
    file_id = add(conn, root="media", rel=rel, name=rel.rsplit("/", 1)[-1], size=size, video=codec, **extra)
    conn.execute("UPDATE library_file SET duration_seconds = 7200, frame_rate = 23.976, resolution = ?, video_info_json = ?, bitrate_kbps = ? WHERE id = ?",
                 (resolution, json.dumps(info), kbps + 640, file_id))
    conn.commit()
    return file_id


def clean_metrics(**one):
    base = {"seconds": 30, "block": 6.0, "blur": 4.0, "interlaced": 0.02, "freeze_seconds": 0.0, "errors": 0}
    base.update(one)
    return {"slices": [dict(base, start=i * 600.0) for i in range(5)]}


def rec_of(conn, file_id):
    return dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (file_id,)).fetchone())


# ---------------------------------------------------------------- scoring
def test_a_clean_file_scores_good(conn):
    rec = rec_of(conn, fixture_file(conn))
    out = quality.score(clean_metrics(), rec)
    assert out["grade"] == "good" and out["score"] == 100 and out["findings"] == []


def test_decode_errors_make_a_file_bad_even_when_everything_else_is_fine(conn):
    rec = rec_of(conn, fixture_file(conn))
    metrics = clean_metrics()
    metrics["slices"][1]["errors"] = 12
    metrics["slices"][3]["errors"] = 2
    out = quality.score(metrics, rec)
    assert out["score"] < 60 and any(f["key"] == "decode_errors" and f["severity"] == "bad" for f in out["findings"])


def test_too_few_bits_for_the_picture_is_found_and_hevc_gets_its_efficiency(conn):
    starved = rec_of(conn, fixture_file(conn, rel="A (2000)/A (2000).mkv", kbps=1500, codec="h264", resolution="1920x1080"))      # 0.03 bits per pixel
    hevc_same = rec_of(conn, fixture_file(conn, rel="B (2000)/B (2000).mkv", kbps=1500, codec="hevc", resolution="1920x1080"))
    a, b = quality.score(clean_metrics(), starved), quality.score(clean_metrics(), hevc_same)
    assert any(f["key"] == "low_bitrate" for f in a["findings"])
    assert quality.bits_per_pixel(hevc_same) > quality.bits_per_pixel(starved)
    assert b["score"] > a["score"]


def test_block_and_blur_are_advisory_and_never_make_a_file_bad_alone(conn):
    rec = rec_of(conn, fixture_file(conn))
    out = quality.score(clean_metrics(block=45.0, blur=30.0), rec)
    assert {f["key"] for f in out["findings"]} == {"blocky", "blurry"} and out["grade"] != "bad"


def test_frozen_picture_and_interlacing_are_reported(conn):
    rec = rec_of(conn, fixture_file(conn))
    out = quality.score(clean_metrics(freeze_seconds=12.0, interlaced=0.9), rec)
    assert {f["key"] for f in out["findings"]} == {"frozen", "interlaced"}


def test_no_measurements_give_no_score(conn):
    rec = rec_of(conn, fixture_file(conn))
    assert quality.score({"slices": []}, rec)["score"] is None


# ---------------------------------------------------------------- a check and its job
def finish_job(conn, check_id, metrics=None, status="done", error=None):
    check = quality.get(conn, check_id)
    worker_id, _ = workers.create(conn, f"w{check_id}")
    conn.execute("UPDATE job SET status = ?, worker_id = ?, result = ?, error = ? WHERE id = ?",
                 (status, worker_id, json.dumps(metrics if metrics is not None else clean_metrics()), error, check["job_id"]))
    quality.sync(conn, check["job_id"])
    conn.commit()
    return quality.get(conn, check_id)


def test_a_check_is_a_scan_job_for_workers_that_can_measure(conn):
    file_id = fixture_file(conn)
    check_id = quality.request(conn, file_id, by="Greg")
    job = jobs.get(conn, quality.get(conn, check_id)["job_id"])
    assert job["kind"] == "scan" and job["plan"]["op"] == "quality" and job["plan"]["needs"] == ["quality"]
    assert (job["source_root"], job["source_rel"]) == ("media", "Heat (1995)/Heat (1995).mkv")
    old = {"capabilities": {"ops": ["scan", "convert"], "features": ["profiles"]}}
    new = {"capabilities": {"ops": ["scan", "quality"], "features": ["quality"]}}
    assert not jobs.can_run(old, job) and jobs.can_run(new, job)


def test_asking_twice_returns_the_same_waiting_check(conn):
    file_id = fixture_file(conn)
    assert quality.request(conn, file_id) == quality.request(conn, file_id)
    assert conn.execute("SELECT COUNT(*) FROM job WHERE ref_type = 'quality_check'").fetchone()[0] == 1


def test_a_finished_job_stores_the_score_and_the_numbers(conn):
    file_id = fixture_file(conn)
    done = finish_job(conn, quality.request(conn, file_id))
    assert done["status"] == "done" and done["score"] == 100 and done["grade"] == "good" and done["samples"] == 5 and done["metrics"]["slices"][0]["block"] == 6.0
    state = quality.state_for(conn, rec_of(conn, file_id))
    assert state["current"] and not state["low"] and state["label"] == "Good"


def test_a_failed_job_is_recorded_and_the_file_can_be_asked_again(conn):
    file_id = fixture_file(conn)
    done = finish_job(conn, quality.request(conn, file_id), status="failed", error="The share was offline.")
    assert done["status"] == "failed" and "offline" in done["message"]
    assert quality.request(conn, file_id) != done["id"]


def test_a_file_that_changed_while_it_was_checked_is_not_scored(conn):
    file_id = fixture_file(conn)
    check_id = quality.request(conn, file_id)
    conn.execute("UPDATE library_file SET size_bytes = size_bytes + 1 WHERE id = ?", (file_id,))
    done = finish_job(conn, check_id)
    assert done["status"] == "failed" and "changed" in done["message"]


def test_a_low_score_flags_the_file_until_it_is_accepted_or_changes(conn):
    file_id = fixture_file(conn)
    metrics = clean_metrics()
    metrics["slices"][0]["errors"] = 40
    finish_job(conn, quality.request(conn, file_id), metrics)
    rec = rec_of(conn, file_id)
    assert quality.state_for(conn, rec)["low"] and [r["id"] for r in quality.low_quality(conn)] == [file_id] and quality.low_count(conn) == 1
    quality.accept(conn, file_id, by="Greg")
    assert not quality.state_for(conn, rec)["low"] and quality.state_for(conn, rec)["label"] == "Accepted as is" and quality.low_quality(conn) == []
    quality.unaccept(conn, file_id)
    assert quality.low_count(conn) == 1
    quality.accept(conn, file_id)
    conn.execute("UPDATE library_file SET mtime = mtime + 5 WHERE id = ?", (file_id,))
    assert quality.low_quality(conn) == []           # the file changed: the old result no longer describes it, so nothing is flagged until it is checked again


def test_results_from_older_scoring_rules_are_not_current(conn):
    file_id = fixture_file(conn)
    check_id = finish_job(conn, quality.request(conn, file_id))["id"]
    conn.execute("UPDATE quality_check SET check_version = 0 WHERE id = ?", (check_id,))
    state = quality.state_for(conn, rec_of(conn, file_id))
    assert not state["current"] and state["label"].endswith("(old)")
    assert [r["id"] for r in quality.due_files(conn, 5)] == [file_id]


# ---------------------------------------------------------------- new downloads and the Library pass
def test_a_new_import_is_checked_only_when_the_setting_says_so(conn):
    file_id = fixture_file(conn)
    rec = rec_of(conn, file_id)
    settings.set_value("quality_on_download", "off", conn=conn)
    assert quality.request_for_import(conn, rec) is None
    settings.set_value("quality_on_download", "flag", conn=conn)
    check_id = quality.request_for_import(conn, rec)
    assert check_id and quality.get(conn, check_id)["source"] == "download"
    assert jobs.get(conn, quality.get(conn, check_id)["job_id"])["priority"] > 100         # someone is waiting for this one


def test_the_download_workflow_asks_for_a_check(conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "radarr_path": "/movies"})
    file_id = fixture_file(conn)
    row = {"id": 1}
    conn.execute("INSERT INTO download_import (id, service, title, root_name, relative_path, path_key, event, status, created_at, updated_at) VALUES (1, 'radarr', 'Heat', 'media', ?, 'k', 'Download', 'scanning', ?, ?)",
                 ("Heat (1995)/Heat (1995).mkv", NOW, NOW))
    conn.commit()
    downloads.decide(conn, {"id": 1}, rec_of(conn, file_id))
    assert conn.execute("SELECT COUNT(*) FROM quality_check WHERE library_file_id = ? AND source = 'download'", (file_id,)).fetchone()[0] == 1


class Playing:
    def __init__(self, playing):
        self.playing = playing

    def sessions(self):
        return [{"UserName": "Greg", "DeviceName": "TV", "Client": "Jellyfin", "NowPlayingItem": {"Name": "Heat", "Type": "Movie"} if self.playing else None, "PlayState": {}}]


def test_the_library_pass_is_off_by_default_and_waits_while_someone_watches(conn):
    fixture_file(conn, rel="A (2000)/A (2000).mkv")
    fixture_file(conn, rel="B (2001)/B (2001).mkv")
    assert quality.library_step(Playing(False)) == 0                                  # off
    settings.set_value("quality_scan_library", "1", conn=conn)
    conn.commit()
    assert quality.library_step(Playing(True)) == 0                                   # someone is watching
    assert quality.library_step(Playing(False)) == 2
    assert quality.library_step(Playing(False)) == 0                                  # both are already waiting


def test_the_library_pass_never_has_more_than_a_few_checks_in_flight(conn):
    for i in range(6):
        fixture_file(conn, rel=f"F{i} (2000)/F{i} (2000).mkv")
    settings.set_value("quality_scan_library", "1", conn=conn)
    conn.commit()
    assert quality.library_step(Playing(False)) == quality.CLEANUP_IN_FLIGHT
    assert quality.library_step(Playing(False)) == 0


def test_the_pass_skips_files_that_are_missing_or_not_analysed(conn):
    fixture_file(conn, rel="A (2000)/A (2000).mkv")
    gone = fixture_file(conn, rel="B (2001)/B (2001).mkv")
    conn.execute("UPDATE library_file SET missing_since = ? WHERE id = ?", (NOW, gone))
    conn.commit()
    assert len(quality.due_files(conn, 10)) == 1
    with pytest.raises(quality.QualityError):
        quality.request(conn, gone)


# ---------------------------------------------------------------- the movie page
def film(conn, with_jellyfin=True):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "jellyfin_path": "/data/media"})
    media_id = conn.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('movie', 'Heat', 1995, '949')").lastrowid
    if with_jellyfin:
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, name, first_seen, last_seen, path) VALUES ('jf1', 'movie', 'Heat', ?, ?, '/data/media/Heat (1995)/Heat (1995).mkv')", (NOW, NOW))
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', 'jf1', 'x', 1.0)", (media_id,))
    conn.commit()
    return media_id


def test_a_film_with_a_4k_copy_and_no_smaller_one_is_told_so(conn):
    media_id = film(conn)
    fixture_file(conn, rel="Heat (1995)/Heat (1995).mkv", size=40_000_000_000, kbps=40000, codec="hevc", resolution="3840x2160", hdr="hdr10")
    fixture_file(conn, rel="Other (2000)/Other (2000).mkv")                              # another film: not part of this one
    out = quality.copies(conn, media_id)
    assert [f["name"] for f in out["files"]] == ["Heat (1995).mkv"] and out["main"]["height"] == 2160
    assert out["remote"] == [] and out["missing_remote"] and out["full_label"] == "2160p HDR10"


def test_a_1080p_extra_version_next_to_the_4k_file_is_the_remote_copy(conn):
    media_id = film(conn)
    fixture_file(conn, rel="Heat (1995)/Heat (1995).mkv", size=40_000_000_000, kbps=40000, codec="hevc", resolution="3840x2160")
    fixture_file(conn, rel="Heat (1995)/Heat (1995) - 1080p.mkv", size=4_000_000_000, kbps=6000, codec="hevc", resolution="1920x1080")
    out = quality.copies(conn, media_id)
    assert out["main"]["name"] == "Heat (1995).mkv" and [f["name"] for f in out["remote"]] == ["Heat (1995) - 1080p.mkv"] and not out["missing_remote"]


def test_a_film_that_jellyfin_does_not_know_has_no_copies(conn):
    media_id = film(conn, with_jellyfin=False)
    fixture_file(conn)
    assert quality.copies(conn, media_id)["files"] == []


def test_the_movie_page_shows_the_card_and_the_buttons_work(client, conn):
    media_id = film(conn)
    file_id = fixture_file(conn, rel="Heat (1995)/Heat (1995).mkv", size=40_000_000_000, kbps=40000, codec="hevc", resolution="3840x2160")
    page = client.get(f"/movie/{media_id}").get_data(as_text=True)
    assert "Your copies" in page and "Full quality copy: 2160p" in page and "No smaller copy for remote viewing" in page and f'data-quality-check="{file_id}"' in page
    ok = client.post("/api/library/quality/check", json={"file_id": file_id}).get_json()
    assert ok["ok"] and conn.execute("SELECT COUNT(*) FROM quality_check").fetchone()[0] == 1
    assert not client.post("/api/library/quality/check", json={"file_id": 99999}).get_json()["ok"]
    assert not client.post("/api/library/quality/check", json={}).get_json()["ok"]


def test_the_low_quality_page_lists_flagged_files_and_saves_settings(client, conn):
    file_id = fixture_file(conn)
    metrics = clean_metrics()
    metrics["slices"][0]["errors"] = 40
    finish_job(conn, quality.request(conn, file_id), metrics)
    page = client.get("/library/quality").get_data(as_text=True)
    assert "Heat (1995).mkv" in page and "decode errors" in page and "Accept as is" in page
    assert "flagged as low quality" in client.get("/").get_data(as_text=True)
    assert client.post("/api/library/quality/accept", json={"file_id": file_id}).get_json()["ok"]
    assert "Nothing is flagged" in client.get("/library/quality").get_data(as_text=True)
    saved = client.post("/api/library/quality/settings", json={"quality_samples": "6", "quality_scan_library": "1", "quality_on_download": "off"}).get_json()
    assert saved["ok"] and quality.samples() == 6 and quality.library_pass() and not quality.on_download()
    assert not client.post("/api/library/quality/settings", json={"quality_samples": "99"}).get_json()["ok"]


def test_the_library_record_page_has_a_quality_panel(client, conn):
    file_id = fixture_file(conn)
    page = client.get(f"/library/record/{file_id}").get_data(as_text=True)
    assert "Picture quality" in page and "Check quality" in page


# ---------------------------------------------------------------- the worker's measuring
def test_slices_are_spread_over_the_film_and_skip_the_ends():
    windows = agent.quality_windows(7200, 10, 30, seed=5)
    assert len(windows) == 10 and windows[0][0] == 180.0 and windows[-1][0] <= 7200 - 180 - 30 and windows == sorted(windows)
    assert agent.quality_windows(20, 10, 30) == [(0.0, 20.0)]
    assert agent.quality_windows(0, 10, 30) == []


def test_the_log_of_a_slice_is_turned_into_numbers():
    log = """[Parsed_idet_2 @ 0x1] Multi frame detection: TFF:     0 BFF:    10 Progressive:   134 Undetermined:     0
[Parsed_blockdetect_0 @ 0x2] block mean: 13.0520412
[Parsed_blurdetect_1 @ 0x3] blur mean: 9.0130429
[freezedetect @ 0x4] lavfi.freezedetect.freeze_duration: 3.5
[h264 @ 0x5] error while decoding MB 68 39, bytestream -55
[h264 @ 0x5] Invalid NAL unit size (-29747 > 29746).
frame=  144 fps=0.0 q=-0.0 size=N/A time=00:00:06.00 bitrate=N/A speed=  38x
"""
    out = agent.parse_quality(log, 6)
    assert out == {"seconds": 6.0, "block": 13.052, "blur": 9.013, "interlaced": round(10 / 144, 3), "freeze_seconds": 3.5, "errors": 2}


def test_filters_that_ffmpeg_lacks_are_left_out_of_the_command():
    command = agent.quality_command("ffmpeg", "/m/film.mkv", 600.0, 30.0, ["idet"], niceness=False)
    assert command[command.index("-vf") + 1] == "idet" and "blockdetect" not in " ".join(command)
    assert "-vf" not in agent.quality_command("ffmpeg", "/m/film.mkv", 0, 5, [], niceness=False)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_damaged_file_is_measured_as_damaged(tmp_path):
    good, bad = tmp_path / "good.mp4", tmp_path / "bad.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=4", "-c:v", "libx264", "-crf", "20", str(good)], check=True)
    data = bytearray(good.read_bytes())
    for i in range(len(data) // 3, len(data) // 3 + 600):
        data[i] ^= 0xFF
    bad.write_bytes(bytes(data))
    out = {}
    for name, path in (("good", good), ("bad", bad)):
        run = subprocess.run(agent.quality_command("ffmpeg", str(path), 0, 4, agent.QUALITY_FILTERS, niceness=False), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out[name] = agent.parse_quality(run.stdout, 4)
    assert out["good"]["errors"] == 0 and out["bad"]["errors"] > 0


# ---------------------------------------------------------------- smoother scoring and the minimum resolution
def test_a_bitrate_just_under_the_line_costs_almost_nothing(conn):
    # 3440 kbps of H.264 at 1920x1080 and 23.976 fps is 0.0692 bits per pixel: under the old 0.07 cliff, now a fraction of a point
    near = rec_of(conn, fixture_file(conn, rel="A (2000)/A (2000).mkv", kbps=3440, codec="h264", resolution="1920x1080"))
    assert 0.068 < quality.bits_per_pixel(near) < 0.07
    out = quality.score(clean_metrics(), near)
    assert out["findings"] == [] and out["score"] > 99
    thin = rec_of(conn, fixture_file(conn, rel="B (2000)/B (2000).mkv", kbps=2200, codec="h264", resolution="1920x1080"))      # about 0.044
    (mild,) = quality.score(clean_metrics(), thin)["findings"]
    assert mild["key"] == "low_bitrate" and mild["severity"] == "warn" and "may show blocks" in mild["text"]
    starved = rec_of(conn, fixture_file(conn, rel="C (2000)/C (2000).mkv", kbps=900, codec="h264", resolution="1920x1080"))      # about 0.018
    (bad,) = quality.score(clean_metrics(), starved)["findings"]
    assert bad["severity"] == "bad" and "will look blocky" in bad["text"]
    assert quality._curve(0.0695, quality.BPP_CURVE) < quality._curve(0.05, quality.BPP_CURVE) < quality._curve(0.03, quality.BPP_CURVE)


def test_mild_findings_are_information_not_warnings(conn):
    rec = rec_of(conn, fixture_file(conn))
    out = quality.score(clean_metrics(blur=10.5), rec)
    (blur,) = out["findings"]
    assert blur["key"] == "blurry" and blur["severity"] == "info" and "Slightly soft" in blur["text"] and out["grade"] == "good"


def test_a_picture_under_the_minimum_resolution_is_flagged_whatever_the_check_says(conn):
    sd = fixture_file(conn, rel="Old (1980)/Old (1980).mkv", kbps=1500, codec="h264", resolution="720x480")
    rec = rec_of(conn, sd)
    state = quality.state_for(conn, rec)
    assert state["low"] and state["small"] and state["label"] == "Low quality" and "720x480" in state["extra"][0]["text"] and "1080p" in state["extra"][0]["text"]
    rows = quality.low_quality(conn)
    assert [r["id"] for r in rows] == [sd] and rows[0]["findings"][0]["key"] == "below_minimum" and quality.low_count(conn) == 1


def test_widescreen_black_bars_and_the_minimum_setting(conn):
    scope = fixture_file(conn, rel="Scope (2000)/Scope (2000).mkv", kbps=8000, resolution="1920x800")
    hd = fixture_file(conn, rel="HD (2000)/HD (2000).mkv", kbps=8000, resolution="1280x720")
    assert quality.below_minimum(rec_of(conn, scope), 1080) is None and quality.below_minimum(rec_of(conn, hd), 1080) == (1280, 720)
    settings.set_value("quality_min_height", "720", conn=conn)
    conn.commit()
    assert quality.low_quality(conn) == []
    settings.set_value("quality_min_height", "0", conn=conn)
    conn.commit()
    assert quality.min_height() == 0 and quality.low_quality(conn) == []


def test_accepting_a_small_file_stops_the_flag_and_a_change_brings_it_back(conn):
    sd = fixture_file(conn, rel="Old (1980)/Old (1980).mkv", resolution="720x480")
    quality.accept(conn, sd, by="Greg")
    assert quality.low_quality(conn) == [] and quality.state_for(conn, rec_of(conn, sd))["label"] == "Accepted as is"
    conn.execute("UPDATE library_file SET size_bytes = size_bytes + 1 WHERE id = ?", (sd,))
    assert quality.low_count(conn) == 1


def test_older_scores_are_worked_out_again_from_the_stored_numbers(conn):
    file_id = fixture_file(conn)
    metrics = clean_metrics(blur=10.5)
    check_id = finish_job(conn, quality.request(conn, file_id), metrics)["id"]
    conn.execute("UPDATE quality_check SET check_version = 1, score = 55, grade = 'poor', findings_json = '[]' WHERE id = ?", (check_id,))
    assert quality.rescore_stale(conn) == 1
    again = quality.get(conn, check_id)
    assert again["check_version"] == quality.CHECK_VERSION and again["score"] > 55 and again["findings"][0]["key"] == "blurry"
    assert quality.rescore_stale(conn) == 0


# ---------------------------------------------------------------- request a better copy
from synchub import better_copy                                      # noqa: E402
from synchub.clients import ApiError                                 # noqa: E402


def release(title, resolution=1080, guid=None, indexer=3, size=8_000_000_000, rejections=(), allowed=True, seeders=20):
    return {"guid": guid or title, "indexerId": indexer, "title": title, "size": size, "seeders": seeders, "indexer": "Tracker", "protocol": "torrent", "ageHours": 5,
            "quality": {"quality": {"name": f"Bluray-{resolution}p", "resolution": resolution}}, "rejections": list(rejections), "downloadAllowed": allowed}


class FakeRadarr:
    def __init__(self, releases=(), history=None, movies=None, fail_block=False):
        self.movie_list = movies if movies is not None else [{"id": 7, "title": "Heat", "path": "/movies/Heat (1995)"}]
        self.history = history if history is not None else [{"id": 11, "eventType": "grabbed", "sourceTitle": "Heat.1995.DVDRip", "date": "2020-01-01"},
                                                            {"id": 12, "eventType": "downloadFolderImported", "sourceTitle": "Heat.1995.DVDRip", "date": "2020-01-02"}]
        self.release_list, self.fail_block, self.calls = list(releases), fail_block, []

    def movies(self):
        return self.movie_list

    def movie_history(self, movie_id):
        self.calls.append(("history", movie_id))
        return self.history

    def mark_history_failed(self, history_id):
        self.calls.append(("block", history_id))
        if self.fail_block:
            raise ApiError("Radarr MarkHistoryFailed failed: HTTP 500")

    def search_movie(self, movie_id):
        self.calls.append(("search", movie_id))

    def releases(self, movie_id):
        self.calls.append(("releases", movie_id))
        return self.release_list

    def grab_release(self, guid, indexer_id):
        self.calls.append(("grab", guid, indexer_id))


def sd_film(conn, radarr_path="/movies"):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", **({"radarr_path": radarr_path} if radarr_path else {})})
    return fixture_file(conn, rel="Heat (1995)/Heat (1995).mkv", kbps=1500, resolution="720x480")


def test_auto_request_blocks_the_grabbed_release_then_searches(conn):
    file_id = sd_film(conn)
    radarr = FakeRadarr()
    out = better_copy.request_auto(file_id, by="Greg", radarr=radarr)
    assert [c[0] for c in radarr.calls] == ["history", "block", "search"] and ("block", 11) in radarr.calls
    assert out["status"] == "requested" and "Heat.1995.DVDRip" in out["message"] and "cutoff" in out["message"]
    row = conn.execute("SELECT * FROM quality_request").fetchone()
    assert (row["status"], row["mode"], row["blocklisted"], row["bad_release"], row["service_item_id"], row["requested_by"]) == ("requested", "auto", 1, "Heat.1995.DVDRip", "7", "Greg")


def test_a_second_request_does_not_block_the_same_release_again(conn):
    file_id = sd_film(conn)
    radarr = FakeRadarr()
    better_copy.request_auto(file_id, radarr=radarr)
    better_copy.request_auto(file_id, radarr=radarr)
    assert [c for c in radarr.calls if c[0] == "block"] == [("block", 11)] and [c for c in radarr.calls if c[0] == "search"] == [("search", 7)] * 2


def test_no_grab_in_the_history_still_searches_and_says_so(conn):
    file_id = sd_film(conn)
    radarr = FakeRadarr(history=[])
    out = better_copy.request_auto(file_id, radarr=radarr)
    assert ("search", 7) in radarr.calls and not any(c[0] == "block" for c in radarr.calls) and "no grab" in out["message"]


def test_a_failed_block_is_reported_but_the_search_goes_ahead(conn):
    file_id = sd_film(conn)
    radarr = FakeRadarr(fail_block=True)
    out = better_copy.request_auto(file_id, radarr=radarr)
    assert "could not be blocked" in out["message"] and ("search", 7) in radarr.calls
    assert conn.execute("SELECT blocklisted FROM quality_request").fetchone()[0] == 0


def test_requests_that_cannot_be_made_say_why_and_are_logged(conn):
    file_id = sd_film(conn, radarr_path=None)
    with pytest.raises(better_copy.BetterCopyError, match="Radarr path"):
        better_copy.request_auto(file_id, radarr=FakeRadarr())
    conn.execute("UPDATE storage_root SET radarr_path = '/movies'")
    conn.commit()
    with pytest.raises(better_copy.BetterCopyError, match="no movie"):
        better_copy.request_auto(file_id, radarr=FakeRadarr(movies=[{"id": 9, "title": "Other", "path": "/movies/Other (2000)"}]))
    with pytest.raises(better_copy.BetterCopyError, match="not in the Library"):
        better_copy.preview(99999, radarr=FakeRadarr())
    assert conn.execute("SELECT COUNT(*) FROM quality_request WHERE status = 'failed'").fetchone()[0] == 1


def test_the_release_list_keeps_only_real_upgrades_best_first_and_changes_nothing(conn):
    file_id = sd_film(conn)
    radarr = FakeRadarr(releases=[release("Heat.1995.DVDRip", 480), release("Heat.1995.720p", 720), release("Heat.1995.1080p.A", 1080, seeders=5), release("Heat.1995.1080p.B", 1080, seeders=50),
                                  release("Heat.1995.2160p", 2160, guid="g4k"), release("Heat.1995.1080p.Banned", 1080, rejections=["Release is blocklisted"]),
                                  release("Heat.1995.1080p.Nope", 1080, allowed=False)])
    out = better_copy.preview(file_id, radarr=radarr)
    assert [c["title"] for c in out["candidates"]] == ["Heat.1995.2160p", "Heat.1995.1080p.B", "Heat.1995.1080p.A"]
    assert out["wanted_height"] == 1080 and out["bad_release"] == "Heat.1995.DVDRip" and out["can_block"]
    assert not any(c[0] in ("block", "search", "grab") for c in radarr.calls)


def test_grabbing_a_chosen_release_blocks_the_old_one_first(conn):
    file_id = sd_film(conn)
    radarr = FakeRadarr(releases=[release("Heat.1995.1080p.B", 1080, guid="gB", indexer=4)])
    out = better_copy.grab(file_id, "gB", 4, by="Greg", radarr=radarr)
    assert radarr.calls.index(("block", 11)) < radarr.calls.index(("grab", "gB", 4)) and out["status"] == "grabbed"
    assert conn.execute("SELECT chosen_release, mode FROM quality_request").fetchone()[:] == ("Heat.1995.1080p.B", "pick")


def test_only_a_release_on_offer_now_can_be_grabbed(conn):
    file_id = sd_film(conn)
    radarr = FakeRadarr(releases=[release("Heat.1995.720p", 720, guid="g720"), release("Heat.1995.1080p", 1080, guid="g1")])
    for guid in ("made-up", "g720"):
        with pytest.raises(better_copy.BetterCopyError, match="not on offer"):
            better_copy.grab(file_id, guid, 3, radarr=radarr)
    assert not any(c[0] in ("block", "grab") for c in radarr.calls)


def test_tv_episodes_are_not_requested_yet(conn):
    file_id = sd_film(conn)
    conn.execute("UPDATE library_file SET media_type = 'TV Episode' WHERE id = ?", (file_id,))
    conn.commit()
    with pytest.raises(better_copy.BetterCopyError, match="TV"):
        better_copy.request_auto(file_id, radarr=FakeRadarr())


def test_the_buttons_reach_radarr_and_the_page_shows_the_request(client, conn, monkeypatch):
    file_id = sd_film(conn)
    radarr = FakeRadarr(releases=[release("Heat.1995.1080p.B", 1080, guid="gB", indexer=4)])
    monkeypatch.setattr(better_copy, "_client", lambda radarr_=None: radarr)
    listed = client.post("/api/library/quality/releases", json={"file_id": file_id}).get_json()
    assert listed["ok"] and listed["candidates"][0]["guid"] == "gB"
    asked = client.post("/api/library/quality/better", json={"file_id": file_id}).get_json()
    assert asked["ok"] and "Blocked" in asked["message"]
    grabbed = client.post("/api/library/quality/grab", json={"file_id": file_id, "guid": "gB", "indexer_id": 4}).get_json()
    assert grabbed["ok"] and ("grab", "gB", 4) in radarr.calls
    assert not client.post("/api/library/quality/grab", json={"file_id": file_id}).get_json()["ok"]
    page = client.get("/library/quality").get_data(as_text=True)
    assert "Request better copy" in page and "Show releases" in page and "Below your minimum resolution" in page
    assert "Better copy grabbed" in client.get(f"/library/record/{file_id}").get_data(as_text=True)


def test_the_radarr_client_sends_the_calls_radarr_documents(monkeypatch):
    from synchub import http
    from synchub.clients.arr import RadarrClient
    seen = []

    class Answer:
        status_code = 200
        text = ""

        def json(self):
            return [] if seen[-1][2] == "GET" else {}

    def fake(service, event, method, url, **kw):
        seen.append((event, url, method, kw.get("params"), kw.get("json")))
        return Answer()

    monkeypatch.setattr(http, "request", fake)
    client = RadarrClient("http://radarr:7878", "key")
    client.movie_history(7)
    client.mark_history_failed(11)
    client.search_movie(7)
    client.releases(7)
    client.grab_release("g1", 4)
    assert seen == [
        ("GetMovieHistory", "http://radarr:7878/api/v3/history/movie", "GET", {"movieId": 7}, None),
        ("MarkHistoryFailed", "http://radarr:7878/api/v3/history/failed/11", "POST", None, {}),
        ("SearchMovie", "http://radarr:7878/api/v3/command", "POST", None, {"name": "MoviesSearch", "movieIds": [7]}),
        ("GetReleases", "http://radarr:7878/api/v3/release", "GET", {"movieId": 7}, None),
        ("GrabRelease", "http://radarr:7878/api/v3/release", "POST", None, {"guid": "g1", "indexerId": 4}),
    ]
