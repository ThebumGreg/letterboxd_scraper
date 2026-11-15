"""Compression test: a few short clips are encoded with the planned profile and scored with VMAF before any real conversion."""
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from synchub import compression_test, jobs, settings
from tests.v2.test_quality import fixture_file

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
import synchub_agent as agent                                  # noqa: E402

GIB = 1024 ** 3


@pytest.fixture
def big(conn):
    """A 1080p film at 31 Mbps (30 GB), well over the size rule's trigger, so a compression is planned."""
    settings.set_value("size_policy", "1", conn=conn)
    file_id = fixture_file(conn, size=30 * GIB, kbps=31000)
    conn.commit()
    return file_id


def finish(conn, test_id, result, status="done"):
    row = compression_test.get(conn, test_id)
    conn.execute("UPDATE job SET status = ?, result = ? WHERE id = ?", (status, json.dumps(result), row["job_id"]))
    compression_test.sync(conn, row["job_id"])
    conn.commit()
    return compression_test.get(conn, test_id)


def clip(start, vmaf, source=40000, encoded=12000):
    return {"start": start, "length": 15.0, "source_kbps": source, "encoded_kbps": encoded, "vmaf": vmaf}


def result(vmaf=(95.0, 94.0, 96.0, 93.5), **extra):
    scores = list(vmaf)
    out = {"op": "compress_test", "encoder": "cpu", "hdr": "sdr", "height": 1080, "floor": 93.0, "mean": round(sum(scores) / len(scores), 2), "min": min(scores), "skipped": None,
           "clips": [clip(600.0 * (i + 1), s) for i, s in enumerate(scores)], "elapsed": 180.0}
    out.update(extra)
    return out


# ---------------------------------------------------------------- the rules
def test_verdict_looks_at_the_average_and_the_worst_clip():
    assert compression_test.verdict_for(94.0, 91.0, 93.0, 8) == "pass"
    assert compression_test.verdict_for(94.0, 84.0, 93.0, 8) == "borderline"     # one clip is 9 under the floor
    assert compression_test.verdict_for(94.0, 85.0, 93.0, 8) == "pass"           # exactly 8 under still passes
    assert compression_test.verdict_for(92.9, 92.0, 93.0, 8) == "fail"
    assert compression_test.verdict_for(None, None, 93.0) is None


def test_the_estimate_uses_the_clips_bitrate_for_a_targeted_encode_and_the_ratio_otherwise():
    clips = [clip(0, 95, source=40000, encoded=12000), clip(1, 95, source=20000, encoded=8000)]
    numbers = {"duration": 7200, "audio_kbps": 640, "subtitle_kbps": 0}
    target = compression_test.estimate_gb(clips, numbers, {"rate_mode": "target"})
    assert target == round((10000 + 640) * 1000 / 8 * 7200 / GIB, 1)            # the two clips average 10 Mbps
    quality = compression_test.estimate_gb(clips, numbers, {}, video_kbps=30000)
    assert quality == round((30000 * 10000 / 30000 + 640) * 1000 / 8 * 7200 / GIB, 1)    # clips shrank to a third: so does the film
    assert compression_test.estimate_gb(clips, numbers, {}, video_kbps=None) is None and compression_test.estimate_gb([], numbers, {}) is None


def test_the_clips_are_spread_over_the_film_and_the_counts_are_kept_in_range():
    windows = agent.compress_test_windows(7200.0, 4, 15, seed=3)
    starts = [s for s, _ in windows]
    assert len(windows) == 4 and starts == sorted(starts) and starts[0] >= 180 and starts[-1] <= 7200 - 180 - 15 and {length for _, length in windows} == {15.0}
    assert len(agent.compress_test_windows(7200.0, 99, 15)) == 8 and agent.compress_test_windows(7200.0, 4, 500)[0][1] == 30.0
    assert agent.compress_test_windows(0, 4, 15) == []


def test_the_score_command_reads_the_clip_from_zero_and_the_film_at_the_same_window():
    cmd = agent.vmaf_clip_command("ffmpeg", "/w/clip.mkv", "/m/Heat.mkv", 812.0, 15.0, 4)
    first, second = cmd.index("-i"), cmd.index("-i", cmd.index("-i") + 1)
    assert "-ss" not in cmd[:first] and cmd[first + 1] == "/w/clip.mkv"
    assert cmd[second - 4:second] == ["-ss", "812.000", "-t", "15.000"] and cmd[second + 1] == "/m/Heat.mkv" and "libvmaf=n_threads=4" in cmd[cmd.index("-lavfi") + 1]


# ---------------------------------------------------------------- asking for a test
def test_a_request_makes_a_job_only_a_worker_with_the_feature_takes(conn, big):
    test_id = compression_test.request(conn, big, by="Greg")
    row = compression_test.get(conn, test_id)
    job = jobs.get(conn, row["job_id"])
    plan = job["plan"]
    assert job["kind"] == "scan" and job["title"].startswith("Compression test:") and job["ref_type"] == "compression_test"
    assert plan["op"] == "compress_test" and "compress_test" in plan["needs"] and "profiles" in plan["needs"] and plan["clips"] == 4 and plan["seconds"] == 15
    assert plan["profile"]["video_codec"] == "hevc" and plan["change"]["video"]["action"] == "transcode"
    assert not jobs.can_run({"capabilities": {"ops": ["scan", "quality", "preview"], "features": ["profiles", "preview", "quality"]}}, job)
    assert jobs.can_run({"capabilities": {"ops": ["scan", "compress_test"], "features": ["profiles", "compress_test"]}}, job)
    assert compression_test.request(conn, big) == test_id                     # a second press joins the one waiting


def test_the_clip_count_and_length_come_from_the_settings(conn, big):
    settings.set_value("ctest_clips", "6", conn=conn)
    settings.set_value("ctest_seconds", "20", conn=conn)
    conn.commit()
    plan = jobs.get(conn, compression_test.get(conn, compression_test.request(conn, big))["job_id"])["plan"]
    assert plan["clips"] == 6 and plan["seconds"] == 20


def test_a_file_the_size_rules_leave_alone_gets_a_sentence_not_a_job(conn):
    settings.set_value("size_policy", "1", conn=conn)
    small = fixture_file(conn, rel="Small (2000)/Small (2000).mkv", size=2 * GIB, kbps=2500)
    with pytest.raises(compression_test.CompressionTestError, match="nothing to test"):
        compression_test.request(conn, small)
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0
    with pytest.raises(compression_test.CompressionTestError):
        compression_test.request(conn, 99999)


# ---------------------------------------------------------------- the result
def test_a_pass_is_stored_with_the_estimate_and_the_clips(conn, big):
    test_id = compression_test.request(conn, big)
    row = finish(conn, test_id, result())
    assert row["status"] == "done" and row["verdict"] == "pass" and row["mean_vmaf"] == 94.62 and row["min_vmaf"] == 93.5 and row["floor"] == 93.0
    assert row["est_gb"] and row["from_gb"] == 30.0
    shown = compression_test.describe(conn, dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (big,)).fetchone()))
    assert shown["verdict_label"] == "Pass" and shown["current"] and shown["parts"][0]["at"] == "10:00" and shown["worst_at"] == "40:00"


def test_one_bad_clip_makes_it_borderline_even_when_the_average_passes(conn, big):
    row = finish(conn, compression_test.request(conn, big), result((99.0, 99.0, 99.0, 80.0)))
    assert row["mean_vmaf"] == 94.25 and row["verdict"] == "borderline"


def test_an_average_under_the_floor_fails(conn, big):
    row = finish(conn, compression_test.request(conn, big), result((70.0, 80.0, 75.0, 72.0)))
    assert row["verdict"] == "fail" and "Fail" in row["message"]


def test_hdr_is_not_scored_but_still_gives_a_size_estimate(conn, big):
    clips = [{**clip(600.0 * (i + 1), None)} for i in range(4)]
    row = finish(conn, compression_test.request(conn, big), {"op": "compress_test", "encoder": "cpu", "hdr": "hdr10", "height": 2160, "floor": 95.0, "mean": None, "min": None,
                                                              "skipped": "VMAF is not meaningful for HDR pictures, so only the size is estimated", "clips": clips})
    assert row["status"] == "done" and row["verdict"] is None and row["est_gb"] and "HDR" in row["message"]


def test_a_result_goes_stale_when_the_file_or_the_profile_changes(conn, big):
    finish(conn, compression_test.request(conn, big), result())
    rec = lambda: dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (big,)).fetchone())   # noqa: E731
    assert compression_test.describe(conn, rec())["current"]
    conn.execute("UPDATE library_file SET mtime = mtime + 1 WHERE id = ?", (big,))
    assert not compression_test.describe(conn, rec())["current"]
    conn.execute("UPDATE library_file SET mtime = mtime - 1 WHERE id = ?", (big,))
    settings.set_value("size_preset", "slow", conn=conn)
    settings.set_value("size_crf", "20", conn=conn)
    assert compression_test.describe(conn, rec())["current"] is not None


def test_a_file_that_changed_during_the_test_fails_it(conn, big):
    test_id = compression_test.request(conn, big)
    conn.execute("UPDATE library_file SET size_bytes = size_bytes + 5 WHERE id = ?", (big,))
    row = finish(conn, test_id, result())
    assert row["status"] == "failed" and "changed" in row["message"]


def test_a_failed_or_cancelled_job_is_followed(conn, big):
    test_id = compression_test.request(conn, big)
    job_id = compression_test.get(conn, test_id)["job_id"]
    conn.execute("UPDATE job SET status = 'running' WHERE id = ?", (job_id,))
    compression_test.sync_active(conn)
    assert compression_test.get(conn, test_id)["status"] == "running"
    conn.execute("UPDATE job SET status = 'failed', error = 'ffmpeg stopped' WHERE id = ?", (job_id,))
    compression_test.sync_active(conn)
    assert compression_test.get(conn, test_id)["status"] == "failed" and "ffmpeg stopped" in compression_test.get(conn, test_id)["message"]


def test_the_job_belongs_to_its_file_on_the_jobs_pages(conn, big):
    from synchub.library import links
    job_id = compression_test.get(conn, compression_test.request(conn, big))["job_id"]
    assert links.record_for_job(conn, jobs.get(conn, job_id)) == big
    assert job_id in [j["id"] for j in links.jobs_for_record(conn, big)]


# ---------------------------------------------------------------- the worker
def fake_runner(tmp_path, film, plan, vmaf=True, hdr="sdr"):
    work = tmp_path / "work"
    work.mkdir()
    progress = []

    class Runner:
        job = {"plan": plan, "source": {"root": "media", "rel": film.name}}
        cfg = types.SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe", work_dir=str(work), vaapi_device="/dev/dri/renderD128", video_encoder="auto")
        agent = types.SimpleNamespace(capabilities={"vmaf": vmaf, "encoders": []}, gpu_take=lambda: False, gpu_give=lambda: None)
        folder = str(work)
        log = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
        reporter = types.SimpleNamespace(check=lambda: None)

        def folder_in(self, root, rel=""):
            return str(film)

        def media_settings(self, plan):
            return agent.media_analysis.Settings(ffprobe="ffprobe", ffmpeg="ffmpeg", integrity_mode="tail", tail_seconds=30, retry_delay=1, check=lambda: None, log=lambda *a, **k: None)

        def guard(self):
            pass

        def report_progress(self, *a, **k):
            progress.append(a)

        def with_source_colours(self, settings, src, profile):
            return profile

        def video_packet_bytes(self, settings, path, start, seconds):
            return agent.JobRunner.video_packet_bytes(self, settings, path, start, seconds)

        def video_packet_stats(self, settings, path, start, seconds):
            return agent.JobRunner.video_packet_stats(self, settings, path, start, seconds)

        def run_clip(self, command, duration, low, high, label):
            return agent.JobRunner.run_clip(self, command, duration, low, high, label)

        def _remove(self, path):
            Path(path).unlink(missing_ok=True)

        def _stop_ffmpeg(self, proc):
            proc.kill()

    return Runner(), work, progress


@pytest.mark.skipif(not shutil.which("ffmpeg") or "libx265" not in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout,
                    reason="needs ffmpeg with libx265")
def test_the_worker_encodes_clips_with_the_profile_and_reports_numbers_only(tmp_path):
    film = tmp_path / "Heat (1995).mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=40", "-c:v", "libx264", "-b:v", "3M", str(film)], check=True)
    plan = {"op": "compress_test", "clips": 2, "seconds": 5, "seed": 1, "source_size": film.stat().st_size, "duration": 40.0,
            "profile": {"video_codec": "hevc", "crf": 28, "preset": "ultrafast", "ten_bit": False, "audio_kbps": 192, "name": "Test"}, "change": {"video": {"action": "transcode"}}}
    runner, work, progress = fake_runner(tmp_path, film, plan, vmaf=False)
    out = agent.JobRunner.do_compress_test(runner)
    assert out["op"] == "compress_test" and len(out["clips"]) == 2 and out["encoder"] == "cpu" and out["mean"] is None and "libvmaf" in out["skipped"]
    assert all(c["encoded_kbps"] > 0 and c["source_kbps"] > 0 and c["vmaf"] is None and c["length"] == 5.0 for c in out["clips"])
    assert progress[-1][1] == 1.0 and [f.name for f in work.iterdir()] == ["ffmpeg.err"]     # every clip was removed; the log is the job folder's own


@pytest.mark.skipif(not shutil.which("ffmpeg") or "libx265" not in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout,
                    reason="needs ffmpeg with libx265")
def test_a_gpu_clip_that_stops_early_is_encoded_again_on_the_cpu(tmp_path, monkeypatch):
    film = tmp_path / "Heat (1995).mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=40", "-c:v", "libx264", "-b:v", "3M", str(film)], check=True)
    plan = {"op": "compress_test", "clips": 2, "seconds": 5, "seed": 1, "source_size": film.stat().st_size, "duration": 40.0,
            "profile": {"video_codec": "hevc", "crf": 28, "preset": "ultrafast", "ten_bit": False, "audio_kbps": 192, "name": "Test", "gpu": True}, "change": {"video": {"action": "transcode"}}}
    runner, work, _ = fake_runner(tmp_path, film, plan, vmaf=False)
    runner.agent = types.SimpleNamespace(capabilities={"vmaf": False, "encoders": ["hevc_vaapi"]}, gpu_take=lambda: True, gpu_give=lambda: None)
    real = agent.build_chunk_command

    def stops_early(ffmpeg, src, out, change, profile, encoder, device, start, length=None):
        return real(ffmpeg, src, out, change, profile, "cpu", device, start, 0.5 if encoder != "cpu" else length)    # the "GPU" writes half a second and exits cleanly
    monkeypatch.setattr(agent, "build_chunk_command", stops_early)
    out = agent.JobRunner.do_compress_test(runner)
    assert out["encoder"] == "cpu" and out["fallback"] and "frames came out" in out["fallback"][0]
    assert all(c["frames"] >= c["source_frames"] * 0.9 and c["encoded_kbps"] > 100 for c in out["clips"])


def test_a_test_without_compression_settings_fails_without_retrying(tmp_path):
    film = tmp_path / "x.mkv"
    film.write_bytes(b"x")
    runner, _, _ = fake_runner(tmp_path, film, {"op": "compress_test", "profile": None})
    with pytest.raises(agent.JobFailure) as err:
        agent.JobRunner.do_compress_test(runner)
    assert err.value.error_class == "bad_job" and not err.value.retryable


# ---------------------------------------------------------------- the pages
def test_the_button_queues_a_test_and_the_record_page_shows_the_result(client, conn, big):
    assert not client.post("/api/library/quality/check", json={"file_id": 99999, "mode": "compress"}).get_json()["ok"]
    ok = client.post("/api/library/quality/check", json={"file_id": big, "mode": "compress"}).get_json()
    assert ok["ok"] and conn.execute("SELECT COUNT(*) FROM compression_test").fetchone()[0] == 1
    page = client.get(f"/library/record/{big}").get_data(as_text=True)
    assert "Test compression" in page and "Waiting for a worker" in page
    finish(conn, conn.execute("SELECT id FROM compression_test").fetchone()["id"], result((99.0, 99.0, 99.0, 80.0)))
    page = client.get(f"/library/record/{big}").get_data(as_text=True)
    assert "Borderline" in page and "worst clip 80.0 at 40:00" in page and "Estimated size: about" in page
    assert "this file has not been converted" in page                              # no VMAF from a real conversion yet, and the card says why
    assert "No score: this file has not been converted" in page and "Not scored:" not in page


def test_the_settings_are_saved_and_checked(client, conn):
    assert client.post("/api/library/quality/settings", json={"ctest_clips": "6", "ctest_seconds": "20", "ctest_margin": "5"}).get_json()["ok"]
    assert (compression_test.clip_count(conn), compression_test.clip_seconds(conn), compression_test.margin(conn)) == (6, 20, 5)
    assert not client.post("/api/library/quality/settings", json={"ctest_clips": "99"}).get_json()["ok"]


# ---------------------------------------------------------------- encoder choice and test anyway
def test_the_encoder_can_be_chosen_for_one_test_and_for_one_film(conn, big):
    plan = lambda tid: jobs.get(conn, compression_test.get(conn, tid)["job_id"])["plan"]      # noqa: E731
    first = compression_test.request(conn, big, encoder="cpu")
    assert plan(first)["encoder"] == "cpu" and compression_test.get(conn, first)["encoder_wanted"] == "cpu"
    conn.execute("UPDATE job SET status = 'cancelled' WHERE id = ?", (compression_test.get(conn, first)["job_id"],))
    compression_test.sync_active(conn)
    assert "encoder" not in plan(compression_test.request(conn, big))                      # the profile decides
    with pytest.raises(compression_test.CompressionTestError):
        compression_test.request(conn, big, encoder="tpu")
    assert "CPU" in compression_test.set_encoder_choice(conn, big, "cpu")
    conn.execute("UPDATE job SET status = 'cancelled' WHERE ref_type = 'compression_test'")
    compression_test.sync_active(conn)
    assert plan(compression_test.request(conn, big))["encoder"] == "cpu"                    # the film's own choice rides along
    assert "profile" in compression_test.set_encoder_choice(conn, big, "")
    with pytest.raises(compression_test.CompressionTestError):
        compression_test.set_encoder_choice(conn, big, "quantum")


def test_the_films_encoder_choice_reaches_the_conversion_plan(conn, big):
    from synchub.library import size_policy
    assert size_policy.with_encoder({}, {"encoder_choice": "cpu"}) == {"encoder": "cpu"}
    assert size_policy.with_encoder({}, {"encoder_choice": None}) == {} and size_policy.with_encoder({}, {"encoder_choice": "weird"}) == {}


def test_a_gpu_fail_says_a_cpu_test_may_do_better(conn, big):
    finish(conn, compression_test.request(conn, big), result((80.0, 82.0, 85.0, 70.0), encoder="vaapi"))
    shown = compression_test.describe(conn, dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (big,)).fetchone()))
    assert shown["verdict"] == "fail" and "a CPU test may score better" in shown["verdict_text"]


def test_test_anyway_plans_a_file_the_rules_leave_alone(conn):
    settings.set_value("size_policy", "1", conn=conn)
    low = fixture_file(conn, rel="Baby Driver (2017)/Baby Driver (2017).mkv", size=14 * GIB, kbps=16900, codec="hevc")
    with pytest.raises(compression_test.CompressionTestError, match="even if the rules leave it alone"):
        compression_test.request(conn, low)
    test_id = compression_test.request(conn, low, force=True)
    plan = jobs.get(conn, compression_test.get(conn, test_id)["job_id"])["plan"]
    assert plan["op"] == "compress_test" and plan["profile"]["video_codec"] == "hevc" and plan["profile"].get("maxrate_kbps")


def test_the_pages_for_encoder_choice(client, conn, big):
    page = client.get(f"/library/record/{big}").get_data(as_text=True)
    assert "Test on the CPU" in page and "even if the rules leave it alone" in page
    assert client.post("/api/library/quality/check", json={"file_id": big, "mode": "compress", "encoder": "cpu"}).get_json()["ok"]
    assert not client.post("/api/library/quality/check", json={"file_id": big, "mode": "compress", "encoder": "tpu"}).get_json()["ok"]
    assert client.post("/api/library/quality/encoder", json={"file_id": big, "choice": "cpu"}).get_json()["ok"]
    assert conn.execute("SELECT encoder_choice FROM library_file WHERE id = ?", (big,)).fetchone()[0] == "cpu"
    assert not client.post("/api/library/quality/encoder", json={"file_id": big, "choice": "x"}).get_json()["ok"]


def test_the_worker_picks_the_encoder_the_plan_asks_for():
    working = ["hevc_vaapi"]
    profile = {"video_codec": "hevc", "gpu": True}
    assert agent.encoder_choice(None, profile, working) == "vaapi"
    assert agent.encoder_choice("cpu", profile, working) == "cpu"
    assert agent.encoder_choice("gpu", {"video_codec": "hevc", "gpu": False}, working) == "vaapi"
    assert agent.encoder_choice("gpu", {"video_codec": "hevc", "gpu": True, "hdr": {"kind": "hdr10"}}, working) == "cpu"       # HDR never goes to the GPU
    assert agent.encoder_choice("gpu", None, working) == "cpu"


# ---------------------------------------------------------------- new downloads
def test_a_nothing_to_do_import_can_be_approved_to_tidy_its_name_and_share(client, conn):
    from synchub import downloads, storage
    from synchub.web.library import _with_previews
    from tests.v2.library_helpers import add
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "radarr_path": "/movies"})
    settings.set_value("download_pipeline", "review", conn=conn)
    conn.commit()
    payload = {"eventType": "Download", "movie": {"id": 7, "title": "Heat", "folderPath": "/movies/Heat (1995)"}, "movieFile": {"path": "/movies/Heat (1995)/Heat.1995.mkv", "size": 2 * GIB}}
    (made,) = downloads.record_event(conn, "radarr", "Download", payload)
    record_id = add(conn, root="media", rel="Heat (1995)/Heat.1995.mkv", name="Heat (1995).mkv", size=2 * GIB, action="copy_rename")
    conn.execute("UPDATE download_import SET status = 'nothing_to_do', record_id = ?, path_key = (SELECT path_key FROM library_file WHERE id = ?) WHERE id = ?", (record_id, record_id, made))
    conn.commit()
    row = _with_previews(conn, downloads.recent(conn, 15))[0]
    assert row["can_tidy"] is True and row["tidy_text"].startswith("Rename in place")
    reply = client.post(f"/api/downloads/{made}/tidy").get_json()
    assert reply["ok"], reply
    assert conn.execute("SELECT approval_status FROM library_file WHERE id = ?", (record_id,)).fetchone()[0] == "approved"
    again = client.post(f"/api/downloads/{made}/tidy").get_json()
    assert _with_previews(conn, downloads.recent(conn, 15))[0]["can_tidy"] is False          # an approved record has nothing left to tidy


# ---------------------------------------------------------------- test before converting
def approve_for_conversion(conn, file_id):
    """What approving a compressing conversion does: ask for the checks. Returns whether it has to wait."""
    from synchub import quality
    return quality.wait_for_check(conn, file_id, "Greg", compress=True)


def finish_checks(conn, file_id):
    """The quality check and the damage check (when asked for) come back clean."""
    from synchub import quality
    from tests.v2.test_quality import clean_metrics, finish_job
    for row in conn.execute("SELECT id FROM quality_check WHERE library_file_id = ? AND status = 'queued'", (file_id,)).fetchall():
        finish_job(conn, row["id"], clean_metrics())


def waiting_test(conn, file_id):
    return conn.execute("SELECT id FROM compression_test WHERE library_file_id = ? ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()["id"]


def test_a_compressing_conversion_waits_for_a_test_and_a_pass_releases_it(conn, big, monkeypatch):
    from synchub import quality
    from synchub.library import migrate
    monkeypatch.setattr(quality, "check_first", lambda conn=None: True)          # both the quality check and the test are asked for
    queued = []
    monkeypatch.setattr(migrate, "queue", lambda conn, fid, cfg, by, checked=False: queued.append(fid) or 1)
    conn.execute("UPDATE library_file SET approval_status = 'approved', approved_action = 'transcode_copy' WHERE id = ?", (big,))
    assert approve_for_conversion(conn, big) is True
    test_id = waiting_test(conn, big)
    assert conn.execute("SELECT compression_test_id FROM convert_wait WHERE library_file_id = ?", (big,)).fetchone()[0] == test_id
    assert conn.execute("SELECT COUNT(*) FROM quality_check WHERE library_file_id = ?", (big,)).fetchone()[0] == 1
    assert quality.release_waiting(conn) == (0, 0) and not queued                    # still checking and testing
    finish_checks(conn, big)
    finish(conn, test_id, result())
    quality.release_waiting(conn)
    assert queued == [big]


def test_a_fail_holds_the_film_and_convert_anyway_lets_the_next_approval_through(conn, big, monkeypatch):
    from synchub import quality
    from synchub.library import migrate
    queued = []
    monkeypatch.setattr(migrate, "queue", lambda conn, fid, cfg, by, checked=False: queued.append(fid) or 1)
    conn.execute("UPDATE library_file SET approval_status = 'approved', approved_action = 'transcode_copy' WHERE id = ?", (big,))
    assert approve_for_conversion(conn, big)
    finish_checks(conn, big)
    finish(conn, waiting_test(conn, big), result((70.0, 80.0, 75.0, 72.0)))
    assert quality.release_waiting(conn) == (0, 1) and not queued
    row = conn.execute("SELECT approval_status, approval_note FROM library_file WHERE id = ?", (big,)).fetchone()
    assert row["approval_status"] == "pending" and "failed" in row["approval_note"]
    conn.execute("UPDATE library_file SET approval_status = 'approved' WHERE id = ?", (big,))
    assert approve_for_conversion(conn, big) is True                                  # a finished test still says Fail: held again, no second test
    assert conn.execute("SELECT COUNT(*) FROM compression_test").fetchone()[0] == 1
    quality.release_waiting(conn)
    assert not queued
    assert "Approve the file again" in compression_test.override(conn, big)
    conn.execute("UPDATE library_file SET approval_status = 'approved' WHERE id = ?", (big,))
    assert approve_for_conversion(conn, big) is False                                 # accepted: the conversion goes ahead without waiting


def test_a_borderline_asks_and_a_changed_profile_asks_again(conn, big):
    from synchub import quality
    conn.execute("UPDATE library_file SET approval_status = 'approved', approved_action = 'transcode_copy' WHERE id = ?", (big,))
    assert approve_for_conversion(conn, big)
    finish_checks(conn, big)
    finish(conn, waiting_test(conn, big), result((99.0, 99.0, 99.0, 80.0)))
    assert quality.release_waiting(conn) == (0, 1)
    assert "borderline" in conn.execute("SELECT approval_note FROM library_file WHERE id = ?", (big,)).fetchone()[0]
    compression_test.override(conn, big)
    settings.set_value("size_preset", "slow", conn=conn)
    settings.set_value("size_crf", "21", conn=conn)
    conn.execute("UPDATE library_file SET approval_status = 'approved' WHERE id = ?", (big,))
    rec = dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (big,)).fetchone())
    assert compression_test.gate(conn, rec) == ("test", None)                         # the accepted settings are not these settings: a new test, not a free pass


def test_the_gate_can_be_switched_off_and_skips_hdr_and_plain_copies(conn, big, monkeypatch):
    from synchub import quality
    monkeypatch.setattr(quality, "check_first", lambda conn=None: True)          # the suite switches the quality gate off by default
    rec = dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (big,)).fetchone())
    assert compression_test.gate(conn, rec) == ("test", None)
    settings.set_value("ctest_before_convert", "0", conn=conn)
    assert compression_test.gate(conn, rec) == ("go", None)
    settings.set_value("ctest_before_convert", "1", conn=conn)
    hdr = dict(rec, video_info_json=json.dumps({"hdr": "hdr10", "video_kbps": 31000}))
    assert compression_test.gate(conn, hdr) == ("go", None)
    assert quality.wait_for_check(conn, big, "Greg", compress=False) is True            # a plain conversion still waits for its quality check, but never for a test
    assert conn.execute("SELECT COUNT(*) FROM compression_test").fetchone()[0] == 0


def test_the_override_page_and_the_setting(client, conn, big):
    assert not client.post("/api/library/quality/override", json={"file_id": big}).get_json()["ok"]            # nothing failed yet
    finish(conn, compression_test.request(conn, big), result((70.0, 80.0, 75.0, 72.0)))
    page = client.get(f"/library/record/{big}").get_data(as_text=True)
    assert "Convert anyway" in page and "Compression test before converting" not in page
    assert client.post("/api/library/quality/override", json={"file_id": big}).get_json()["ok"]
    assert client.post("/api/library/quality/settings", json={"ctest_before_convert": "0"}).get_json()["ok"]
    assert not client.post("/api/library/quality/settings", json={"ctest_before_convert": "maybe"}).get_json()["ok"]


# ---------------------------------------------------------------- credits, HDR scoring, the VMAF to keep (1.9.0)
def test_the_clips_stay_out_of_the_end_credits():
    for seed in range(6):
        windows = agent.compress_test_windows(6760.0, 4, 15, seed=seed)
        assert len(windows) == 4 and max(start + length for start, length in windows) <= 6760 - 480 + 1
    short = agent.compress_test_windows(2400.0, 4, 15)                      # a short film keeps the old margin: 5 minutes would leave too little
    assert max(start + length for start, length in short) > 2400 - 480


def test_a_quiet_clip_is_left_out_of_the_size_estimate():
    """Baby Driver 2026-11-15: the last clip was in the credits (16.8 Mbps source, 0.4 Mbps encoded) and dragged the estimate from 14 GB to 10.9 GB."""
    clips = [clip(180, 90.0, source=24089, encoded=11684), clip(2315, 96.9, source=24667, encoded=11683), clip(4443, 96.2, source=23715, encoded=11750), clip(6565, 97.0, source=16836 // 2, encoded=446)]
    assert compression_test.atypical(clips) == {3}
    numbers = {"duration": 6760, "audio_kbps": 4912, "subtitle_kbps": 109}
    assert compression_test.estimate_gb(clips, numbers, {"rate_mode": "target"}) == compression_test.estimate_gb(clips[:3], numbers, {"rate_mode": "target"}) > 13
    assert compression_test.atypical(clips[:2]) == set()                    # two clips cannot say which one is odd


def test_an_hdr_picture_is_tone_mapped_for_both_sides_before_it_is_scored():
    plain = " ".join(agent.vmaf_clip_command("ffmpeg", "/w/c.mkv", "/m/f.mkv", 10.0, 15.0, 4))
    hdr = " ".join(agent.vmaf_clip_command("ffmpeg", "/w/c.mkv", "/m/f.mkv", 10.0, 15.0, 4, tonemap="smpte2084"))
    assert "tonemap" not in plain and hdr.count("tonemap=tonemap=hable") == 2 and hdr.count("tin=smpte2084") == 2
    assert "tin=arib-std-b67" in " ".join(agent.vmaf_clip_command("ffmpeg", "/w/c.mkv", "/m/f.mkv", 10.0, 15.0, 4, tonemap="arib-std-b67"))


def test_an_hdr_score_is_marked_as_a_rough_guide(conn, big):
    row = finish(conn, compression_test.request(conn, big), result(hdr="hdr10", tonemapped=True, floor=94.0))
    shown = compression_test.describe(conn, dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (big,)).fetchone()))
    assert row["verdict"] == "pass" and shown["tonemapped"]


def test_the_vmaf_to_keep_replaces_the_floor_and_judges_finished_tests_again(client, conn, big):
    test_id = compression_test.request(conn, big)
    row = finish(conn, test_id, result())                                # average 94.6 against the worker's floor of 93
    assert row["verdict"] == "pass" and row["floor"] == 93.0
    assert client.post("/api/library/quality/settings", json={"ctest_floor": "97"}).get_json()["ok"]
    row = compression_test.get(conn, test_id)
    assert row["floor"] == 97.0 and row["verdict"] == "fail"
    assert client.post("/api/library/quality/settings", json={"ctest_floor": ""}).get_json()["ok"]       # blank = automatic again
    assert compression_test.get(conn, test_id)["verdict"] == "pass" and compression_test.floor_override(conn) is None
    assert not client.post("/api/library/quality/settings", json={"ctest_floor": "20"}).get_json()["ok"]


def test_a_test_is_written_to_the_files_history(conn, big):
    from synchub.library import store
    finish(conn, compression_test.request(conn, big), result())
    kinds = [e["event_type"] for e in store.events_for(conn, big)]
    assert "compression_test_queued" in kinds and "compression_test_done" in kinds
