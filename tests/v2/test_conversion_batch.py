"""GPU encoders, chunked resumable encodes, VMAF scores, the Originals tab and the TV size limits per hour (release 1.3.0)."""
import hashlib
import json
import sys
import time

import pytest

from synchub import db, jobs, originals, workers
from synchub.library import size_policy as sp

from .agent_module import agent
from .test_agent_live import make_long, needs_ffmpeg, probe, queue_compress, rig, wait          # noqa: F401

GIB = 1024 ** 3
HEVC = {"video_codec": "hevc", "crf": 23, "preset": "medium", "ten_bit": False, "audio_kbps": 192, "gpu": True}
CHANGE = {"container": "matroska", "keep_audio": [1], "video": {"action": "transcode", "codec": "hevc"}, "audio_convert": {}}


# ---------------------------------------------------------------- GPU encoders (command building only: CI has no GPU)
def test_a_plan_only_gets_a_gpu_encoder_when_it_allows_it_the_worker_has_one_and_the_picture_is_not_hdr():
    both = ["libx265", "hevc_qsv", "hevc_vaapi"]
    assert agent.choose_encoder(HEVC, both) == "qsv"                                          # auto: nvenc, then qsv, then vaapi
    assert agent.choose_encoder(HEVC, both, "vaapi") == "vaapi" and agent.choose_encoder(HEVC, both, "nvenc") == "cpu"
    assert agent.choose_encoder(HEVC, both, "cpu") == "cpu" and agent.choose_encoder(HEVC, []) == "cpu"
    assert agent.choose_encoder({**HEVC, "gpu": False}, both) == "cpu" and agent.choose_encoder({**HEVC, "hdr": {"kind": "hdr10"}}, both) == "cpu"
    assert agent.choose_encoder({**HEVC, "video_codec": "h264"}, both) == "cpu" and agent.choose_encoder(None, both) == "cpu"


def test_each_gpu_encoder_gets_its_own_options_and_a_size_cap_becomes_an_average_bitrate():
    vaapi = " ".join(agent.build_convert_command("ffmpeg", "/in.mkv", "/out", CHANGE, HEVC, "vaapi", "/dev/dri/renderD129"))
    assert vaapi.index("-vaapi_device /dev/dri/renderD129") < vaapi.index("-i /in.mkv")      # a device option belongs before the input
    assert "-vf format=nv12,hwupload -c:v hevc_vaapi -rc_mode CQP -qp 23 -tag:v hvc1" in vaapi and "libx265" not in vaapi
    capped = {**HEVC, "ten_bit": True, "maxrate_kbps": 10500, "bufsize_kbps": 21000, "max_bytes": 5 * GIB}
    ten = " ".join(agent.build_convert_command("ffmpeg", "/in.mkv", "/out", CHANGE, capped, "vaapi", "/dev/dri/renderD128"))
    assert "format=p010le,hwupload" in ten and "-rc_mode VBR -b:v 10000k -maxrate 10500k -bufsize 21000k" in ten and "-profile:v main10" in ten
    nvenc = " ".join(agent.build_convert_command("ffmpeg", "/in.mkv", "/out", CHANGE, {**HEVC, "preset": "slow"}, "nvenc"))
    assert "-c:v hevc_nvenc -preset p6 -tune hq -rc vbr -cq 23 -b:v 0 -pix_fmt yuv420p -tag:v hvc1" in nvenc
    qsv = " ".join(agent.build_convert_command("ffmpeg", "/in.mkv", "/out", CHANGE, HEVC, "qsv"))
    assert "-init_hw_device qsv=hw@va" in qsv and "hwupload=extra_hw_frames=64" in qsv and "-c:v hevc_qsv -preset medium -global_quality 23" in qsv
    cpu = " ".join(agent.build_convert_command("ffmpeg", "/in.mkv", "/out", CHANGE, HEVC))
    assert "-c:v libx265 -crf 23" in cpu and "hevc_vaapi" not in cpu


def test_a_profile_carries_the_gpu_and_vmaf_choices_and_the_agent_checks_its_new_settings(tmp_path):
    clean = agent.clean_profile({**HEVC, "vmaf_mode": "hold"})
    assert clean["gpu"] is True and clean["vmaf_mode"] == "hold"
    assert agent.clean_profile({**HEVC, "vmaf_mode": "rm -rf"})["vmaf_mode"] == "off" and agent.clean_profile({"video_codec": "keep"}).get("gpu") is None
    base = {"SYNCHUB_URL": "http://x:1", "SYNCHUB_TOKEN": "sw_" + "a" * 20}
    assert agent.Config(None, env=base).validate().video_encoder == "auto" and agent.Config(None, env=base).chunk_minutes == 10
    with pytest.raises(agent.ConfigError):
        agent.Config(None, env={**base, "SYNCHUB_VIDEO_ENCODER": "magic"}).validate()
    assert "originals" in agent.OPS


def test_the_size_policy_passes_the_gpu_and_vmaf_settings_into_the_profile():
    from .test_size_policy import movie
    decision = sp.decide(movie(size_gb=45, video_info={"hdr": "sdr", "bit_depth": 8, "video_kbps": 45000}), [], sp.Settings(on=True, gpu=True, vmaf_mode="hold"))
    assert decision.profile["gpu"] is True and decision.profile["vmaf_mode"] == "hold"
    plan, _ = sp.apply({"op": "copy"}, decision)
    assert plan["profile"]["gpu"] is True and plan["profile"]["vmaf_mode"] == "hold"


# ---------------------------------------------------------------- VMAF helpers
def test_vmaf_windows_spread_over_the_film_and_the_floor_depends_on_the_picture_size():
    windows = agent.vmaf_windows(7200, 5)
    assert len(windows) == 5 and windows[0][0] == 360 and windows[-1][0] + windows[-1][1] == pytest.approx(6840) and all(length == 20 for _, length in windows)
    assert agent.vmaf_windows(30, 5) == [(0.0, 20.0)] or agent.vmaf_windows(30, 5)[0][0] == 0.0
    assert agent.vmaf_windows(0, 5) == [] and len(agent.vmaf_windows(600, 1)) == 1
    assert agent.vmaf_floor(2160) == 95.0 and agent.vmaf_floor(1080) == 93.0
    assert agent.parse_vmaf("... VMAF score: 94.512345\n") == 94.512345 and agent.parse_vmaf("nothing") is None
    command = agent.vmaf_command("ffmpeg", "/c.mkv", "/s.mkv", 60, 20, 4)
    assert command.index("/c.mkv") < command.index("/s.mkv") and "libvmaf=n_threads=4" in command[command.index("-lavfi") + 1]


# ---------------------------------------------------------------- TV limits per hour
def test_the_settings_defaults_make_tv_off_and_per_hour(conn):
    cfg = sp.load(conn)
    assert cfg.tv_on is False and cfg.tv_max_gb_per_hour == 4 and cfg.tv_target_gb_per_hour == 2.5 and cfg.gpu is False and cfg.vmaf_mode == "warn"


def test_the_size_preview_counts_what_the_numbers_would_compress_without_queuing_anything(client, conn):
    from .test_library_web import shares
    from .library_helpers import add
    shares(conn)
    record = add(conn, rel="Shows/Show/S01E01.mkv", name="Show S01E01.mkv", size=int(6 * GIB), media_type="TV Episode")
    info = '{"hdr": "sdr", "bit_depth": 10, "width": 1920, "height": 1080, "video_kbps": 14000}'
    conn.execute("UPDATE library_file SET duration_seconds = 3600, media_type = 'TV Episode', video_codec = 'hevc', resolution = '1920x1080', video_info_json = ? WHERE id = ?", (info, record))
    conn.commit()
    body = {"cap_mbps_2160": "18", "cap_mbps_1080": "8", "cap_mbps_720": "4", "cap_mbps_sd": "2", "size_trigger_factor": "1.5", "safety_gb_2160": "45", "safety_gb_1080": "15", "tv_cap_share": "0.5"}
    off = client.post("/api/library/size_preview", json={**body, "tv_size_policy": "0"}).get_json()
    on = client.post("/api/library/size_preview", json={**body, "tv_size_policy": "1"}).get_json()
    assert off["ok"] and "Nothing" in off["message"] and on["ok"] and "1 episodes" in on["message"]
    assert client.post("/api/library/size_preview", json={**body, "cap_mbps_1080": "x", "tv_size_policy": "1"}).status_code == 400
    saved = client.post("/api/library/settings", json={"size_mode": "bitrate", "tv_size_policy": "1", "tv_cap_share": "0.6", "gpu_encoding": "1", "vmaf_mode": "hold"}).get_json()
    assert saved["ok"]
    cfg = sp.load(conn)
    assert cfg.tv_on and cfg.tv_cap_share == 0.6 and cfg.gpu and cfg.vmaf_mode == "hold"
    assert not client.post("/api/library/settings", json={"vmaf_mode": "sometimes"}).get_json()["ok"]


# ---------------------------------------------------------------- chunked encoding, the GPU fallback and VMAF on a real worker
@needs_ffmpeg
def test_a_long_video_is_encoded_in_chunks_and_an_interruption_loses_at_most_one(rig, monkeypatch):      # noqa: F811
    make_long(rig.roots["tfiles"] / "Old/Heat.mkv", seconds=130)
    profile = {"video_codec": "hevc", "crf": 32, "preset": "ultrafast", "ten_bit": False, "audio_kbps": 128}
    job_id = queue_compress(rig, "Old/Heat.mkv", profile, duration=130.0)
    calls = {"n": 0}
    original = agent.JobRunner.run_ffmpeg

    def flaky(self, command, partial, duration, low, high, label):
        calls["n"] += 1
        if calls["n"] == 3:                                                                 # the third chunk: the worker is stopped as if it were rebooting
            raise agent.Interrupted()
        return original(self, command, partial, duration, low, high, label)
    monkeypatch.setattr(agent.JobRunner, "run_ffmpeg", flaky)
    first = rig.start(SYNCHUB_CHUNK_MINUTES=1)
    wait(lambda: calls["n"] >= 3, timeout=90, what="the interruption")
    rig.stop(first)
    assert rig.job(job_id)["status"] == "running"
    rig.start(SYNCHUB_CHUNK_MINUTES=1)
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=120, what="the resumed encode")
    assert job["status"] == "done", job["error"]
    messages = [line["msg"] for line in rig.joblog(job_id)]
    assert sum(m == "Encoding chunk 1 of 3" for m in messages) == 1 and sum(m == "Encoding chunk 2 of 3" for m in messages) == 1      # finished chunks were not encoded again
    assert sum(m == "Encoding chunk 3 of 3" for m in messages) == 2 and "Joining the encoded parts" in " ".join(messages)
    data = probe(rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv")
    kinds = [s["codec_type"] for s in data["streams"]]
    assert kinds.count("video") == 1 and kinds.count("audio") == 2 and abs(float(data["format"]["duration"]) - 130) < 1.5
    assert job["result"]["chunked"] is True


@needs_ffmpeg
def test_a_gpu_encode_that_fails_is_done_again_on_the_cpu_and_the_job_says_so(rig):                  # noqa: F811
    make_long(rig.roots["tfiles"] / "Old/Heat.mkv", seconds=20)
    profile = {"video_codec": "hevc", "crf": 32, "preset": "ultrafast", "ten_bit": False, "audio_kbps": 128, "gpu": True}
    job_id = queue_compress(rig, "Old/Heat.mkv", profile, duration=20.0)
    instance = rig.start()
    wait(lambda: instance.capabilities, what="the capabilities")
    instance.capabilities["encoders"] = list(instance.capabilities["encoders"]) + ["hevc_vaapi"]       # the worker believes it has a GPU; this PC does not
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=90, what="the encode")
    assert job["status"] == "done", job["error"]
    assert job["result"]["encoder"] == "cpu" and job["result"]["gpu_fallback"] and "vaapi" in job["result"]["gpu_fallback"][0]
    assert probe(rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv")["streams"][0]["codec_name"] in ("hevc", "aac")


def fake_vmaf(monkeypatch, score):
    monkeypatch.setattr(agent, "vmaf_command", lambda *a, **k: [sys.executable, "-c", f"print('VMAF score: {score}')"])


@needs_ffmpeg
def test_a_vmaf_score_is_recorded_and_a_low_one_holds_the_job_until_a_person_approves(rig, monkeypatch):          # noqa: F811
    make_long(rig.roots["tfiles"] / "Old/Heat.mkv", seconds=20)
    fake_vmaf(monkeypatch, 88.5)
    warn = {"video_codec": "hevc", "crf": 32, "preset": "ultrafast", "ten_bit": False, "audio_kbps": 128, "vmaf_mode": "warn"}
    job_id = queue_compress(rig, "Old/Heat.mkv", warn, duration=20.0)
    instance = rig.start()
    wait(lambda: instance.capabilities, what="the capabilities")
    instance.capabilities["vmaf"] = True
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=90, what="the encode")
    assert job["status"] == "done" and job["result"]["vmaf"]["mean"] == 88.5 and job["result"]["vmaf"]["floor"] == 93.0       # warn mode records it and carries on
    (rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv").unlink()
    hold = {**warn, "vmaf_mode": "hold"}
    held_id = queue_compress(rig, "Old/Heat.mkv", hold, duration=20.0)
    held = wait(lambda: (j := rig.job(held_id))["status"] in ("needs_review", "done", "failed") and j, timeout=90, what="the hold")
    assert held["status"] == "needs_review" and "88.5" in held["review_reason"] and not (rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv").exists()
    with rig.db.connection() as conn:
        jobs.decide(conn, held_id, "approve", by="Greg")
    done = wait(lambda: (j := rig.job(held_id))["status"] in ("done", "failed") and j, timeout=90, what="the approved job")
    assert done["status"] == "done" and done["result"]["vmaf"]["mean"] == 88.5 and (rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv").exists()
    messages = [line["msg"] for line in rig.joblog(held_id)]
    assert sum(m.startswith("Quality score (VMAF) 88.5") for m in messages) == 2 and sum(m == "Converting" for m in messages) == 1          # measured at the hold and again on approval, encoded once


# ---------------------------------------------------------------- originals
def keep(rig, name, content=b"original bytes", rel="Movies/Heat (1995)/Heat (1995).avi", replaced="Movies/Heat (1995)/Heat (1995).mkv", days=30):
    folder = rig.work / "originals" / name
    folder.mkdir(parents=True)
    (folder / "Heat (1995).avi").write_bytes(content)
    meta = {"job_id": 1, "root": "media", "rel": rel, "name": "Heat (1995).avi", "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "saved_at": "2026-10-01T00:00:00+00:00",
            "retention_days": days, "expires_at": "2099-01-01T00:00:00+00:00", "replaced_with": replaced}
    (folder / "meta.json").write_text(json.dumps(meta))
    return folder


def stored(rig, folder):
    with rig.db.connection() as conn:
        return next((i for i in workers.get(conn, rig.worker_id)["originals"] if i["id"] == folder), None)


def run_original(rig, folder, action, **kw):
    with rig.db.connection() as conn:
        job_id = originals.queue(conn, rig.worker_id, folder, action, **kw)
    return wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=60, what=f"the {action}")


def test_the_worker_lists_its_originals_and_can_keep_one_longer_delete_one_and_restore_one(rig):     # noqa: F811
    keep(rig, "7-aaaa1111")
    purge_me = keep(rig, "8-bbbb2222", rel="Movies/Other.avi", replaced="Movies/Other.mkv")
    converted = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv"
    converted.parent.mkdir(parents=True)
    converted.write_bytes(b"the converted file")
    rig.start()
    wait(lambda: stored(rig, "7-aaaa1111") and stored(rig, "8-bbbb2222"), what="the list of originals")
    assert stored(rig, "7-aaaa1111")["rel"] == "Movies/Heat (1995)/Heat (1995).avi"
    extended = run_original(rig, "7-aaaa1111", "extend", days=90)
    assert extended["status"] == "done" and json.loads((rig.work / "originals/7-aaaa1111/meta.json").read_text())["retention_days"] == 90
    assert run_original(rig, "8-bbbb2222", "purge")["status"] == "done" and not purge_me.exists()
    restored = run_original(rig, "7-aaaa1111", "restore")
    assert restored["status"] == "done", restored["error"]
    assert (rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).avi").read_bytes() == b"original bytes" and not converted.exists()
    aside = rig.work / "originals/7-aaaa1111-converted"
    assert (aside / "Heat (1995).mkv").read_bytes() == b"the converted file" and json.loads((aside / "meta.json").read_text())["kind"] == "converted"
    assert not (rig.work / "originals/7-aaaa1111").exists() and not list(converted.parent.glob("*synchub-restore*"))
    wait(lambda: stored(rig, "7-aaaa1111-converted") and not stored(rig, "7-aaaa1111"), what="the list to follow")


def test_a_restore_refuses_a_kept_original_that_does_not_match_its_checksum(rig):    # noqa: F811
    folder = keep(rig, "9-cccc3333")
    (folder / "Heat (1995).avi").write_bytes(b"original byteS")                               # same size, different content
    converted = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv"
    converted.parent.mkdir(parents=True)
    converted.write_bytes(b"the converted file")
    rig.start()
    wait(lambda: stored(rig, "9-cccc3333"), what="the list of originals")
    failed = run_original(rig, "9-cccc3333", "restore")
    assert failed["status"] == "needs_review" and failed["error_class"] == "verification_failed"
    assert converted.read_bytes() == b"the converted file" and not (converted.parent / "Heat (1995).avi").exists() and not list(converted.parent.glob("*synchub-restore*"))


def test_the_originals_queue_checks_what_it_is_asked(conn):
    wid = workers.create(conn, "w1")[0]
    listing = [{"id": "5-abcd1234", "kind": "original", "root": "media", "rel": "Movies/A.avi", "name": "A.avi", "size": 10, "saved_at": "2026-10-01T00:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00", "replaced_with": "Movies/A.mkv"}, {"id": "6-x-converted", "kind": "converted", "root": "media", "rel": "Movies/A.mkv", "name": "A.mkv", "size": 5}]
    workers.store_originals(conn, wid, listing + ["junk", {"no": "id"}])
    assert [i["id"] for i in workers.get(conn, wid)["originals"]] == ["5-abcd1234", "6-x-converted"]
    with pytest.raises(originals.OriginalsError):
        originals.queue(conn, wid, "nope", "purge")
    with pytest.raises(originals.OriginalsError):
        originals.queue(conn, wid, "6-x-converted", "restore")                                  # a converted copy cannot be restored
    with pytest.raises(originals.OriginalsError):
        originals.queue(conn, wid, "5-abcd1234", "extend", days=0)
    with pytest.raises(originals.OriginalsError):
        originals.queue(conn, wid, "5-abcd1234", "explode")
    job_id = originals.queue(conn, wid, "5-abcd1234", "restore")
    job = jobs.get(conn, job_id)
    assert job["pinned_worker_id"] == wid and job["plan"] == {"op": "originals", "action": "restore", "original": "5-abcd1234"}
    assert (job["source_root"], job["source_rel"], job["dest_rel"]) == ("media", "Movies/A.mkv", "Movies/A.avi")
    with pytest.raises(originals.OriginalsError):
        originals.queue(conn, wid, "5-abcd1234", "purge")                                      # something is already queued for it
    shown = originals.listing(conn)
    assert shown["bytes"] == 15 and next(i for i in shown["items"] if i["id"] == "5-abcd1234")["pending"]["action"] == "restore" and shown["items"][0]["days_left"] is not None


def test_the_originals_tab_and_its_endpoint(client, conn):
    wid = workers.create(conn, "w1")[0]
    workers.store_originals(conn, wid, [{"id": "5-abcd1234", "kind": "original", "root": "media", "rel": "Movies/A.avi", "name": "A.avi", "size": 2 * GIB,
                                         "expires_at": "2099-01-01T00:00:00+00:00", "replaced_with": "Movies/A.mkv"}])
    conn.commit()
    page = client.get("/originals")
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "A.avi" in text and "Restore" in text and "Keep longer" in text
    ok = client.post(f"/api/worker_originals/{wid}", json={"id": "5-abcd1234", "action": "extend", "days": 10})
    assert ok.status_code == 200 and ok.get_json()["ok"]
    again = client.post(f"/api/worker_originals/{wid}", json={"id": "5-abcd1234", "action": "purge"})
    assert again.status_code == 400 and "already queued" in again.get_json()["message"]
    assert "extend: queued" in client.get("/originals").get_data(as_text=True)
