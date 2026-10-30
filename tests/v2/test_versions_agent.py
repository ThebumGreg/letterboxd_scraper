"""The agent's side of extra versions and Dolby Vision conversion: the ffmpeg arguments, and real workers doing the jobs with real ffmpeg (and dovi_tool/mkvmerge when installed)."""
import json
import os
import shutil
import subprocess

import pytest

from synchub import jobs, settings, versions

from .agent_module import agent
from .test_agent_live import needs_ffmpeg, probe, rig, wait                     # noqa: F401  (rig is a fixture)
from .test_versions import DV7, GIB, film

HAVE_DOVI = bool(shutil.which("ffmpeg") and shutil.which("dovi_tool") and shutil.which("mkvmerge"))
needs_dovi = pytest.mark.skipif(not HAVE_DOVI, reason="dovi_tool and mkvmerge are not installed")


# ---------------------------------------------------------------- the arguments
def test_a_profile_may_limit_the_picture_height_and_junk_is_dropped():
    base = {"video_codec": "hevc", "crf": 23, "preset": "medium", "maxrate_kbps": 8000}
    assert agent.clean_profile({**base, "max_height": 1080})["max_height"] == 1080
    for bad in ("tall", 100, 99999, 0, None):
        assert "max_height" not in agent.clean_profile({**base, "max_height": bad})


def test_the_picture_is_scaled_down_never_up_and_before_a_gpu_sees_it():
    profile = agent.clean_profile({"video_codec": "hevc", "crf": 23, "preset": "medium", "max_height": 1080, "deinterlace": True, "ten_bit": True})
    args = agent.video_arguments({}, profile)
    assert args[:2] == ["-vf", "yadif,scale=-2:'min(1080,ih)':flags=lanczos"] and "libx265" in args
    for kind in ("nvenc", "qsv", "vaapi"):
        _, after = agent.gpu_video_arguments(kind, profile, "/dev/dri/renderD128")
        chain = after[after.index("-vf") + 1]
        assert chain.startswith("yadif,scale=-2:'min(1080,ih)'") and (chain.index("scale") < chain.index("hwupload") if kind != "nvenc" else True)
    assert "-vf" not in agent.video_arguments({}, agent.clean_profile({"video_codec": "hevc", "crf": 23, "preset": "medium"}))


def test_a_converted_audio_track_can_be_mixed_down():
    change = {"keep_audio": [1], "audio_convert": {"1": {"codec": "aac", "channels": 2}}, "keep_subtitle": []}
    _, codecs = agent._track_arguments(change, agent.clean_profile({"video_codec": "keep", "audio_kbps": 160}), 0)
    assert ["-c:a:0", "aac", "-b:a:0", "160k", "-ac:a:0", "2"] == codecs[4:10]
    change["audio_convert"]["1"]["channels"] = 99
    _, codecs = agent._track_arguments(change, None, 0)
    assert "-ac:a:0" not in codecs


def test_the_dolby_vision_commands_pipe_the_stream_and_join_it_with_the_other_tracks():
    assert agent.dovi_extract_command("ffmpeg", "/in.mkv") == ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-i", "/in.mkv", "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "pipe:1"]
    assert agent.dovi_convert_command("dovi_tool", "/o.hevc") == ["dovi_tool", "-m", "2", "convert", "--discard", "-i", "-", "-o", "/o.hevc"]
    assert agent.mkvmerge_command("mkvmerge", "/o.hevc", "24000/1001", "/in.mkv", "/out.mkv") == ["mkvmerge", "-o", "/out.mkv", "--default-duration", "0:24000/1001p", "/o.hevc", "--no-video", "/in.mkv"]


def test_a_file_that_is_not_profile_7_is_refused():
    with pytest.raises(agent.JobFailure, match="not Dolby Vision profile 7"):
        agent.JobRunner.require_profile_7({"hdr": "hdr10"})
    with pytest.raises(agent.JobFailure, match="profile 8"):
        agent.JobRunner.require_profile_7({"hdr": "dv", "dv_profile": 8})
    agent.JobRunner.require_profile_7({"hdr": "dv", "dv_profile": 7})


# ---------------------------------------------------------------- a real worker makes the smaller copy
def real_film(path, width=640, height=360, seconds=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={width}x{height}:rate=24", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "ac3", "-ac", "6", str(path)], check=True)


@needs_ffmpeg
def test_a_worker_makes_a_scaled_extra_version_beside_the_original(rig, conn):                       # noqa: F811
    source = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995) Bluray-2160p.mkv"
    real_film(source)
    settings.set_value("variant_min_gb", "0", conn=conn)
    settings.set_value("variant_height", "240", conn=conn)
    record_id = film(conn, info={"hdr": "sdr", "bit_depth": 8, "width": 640, "height": 360}, size_gb=0.001, audio=[agent_track(1, "ac3", 6)])
    conn.execute("UPDATE library_file SET size_bytes = ?, mtime = ?, duration_seconds = 3.0 WHERE id = ?", (source.stat().st_size, source.stat().st_mtime, record_id))
    conn.commit()
    before = source.read_bytes()
    versions.approve(conn, "variant", record_id, "Greg")
    conn.commit()
    rig.start()
    wait(lambda: (conn.commit() or True) and versions.get(conn, 1)["status"] in ("done", "failed", "blocked"), timeout=120, what="the copy")
    row = versions.get(conn, 1)
    assert row["status"] == "done", row["message"]
    extra = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995) - 1080p.mkv"
    streams = probe(extra)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert video["codec_name"] == "hevc" and int(video["height"]) == 240 and audio["codec_name"] == "aac" and int(audio["channels"]) == 2
    assert source.read_bytes() == before                                                              # the original is untouched
    assert jobs.get(conn, row["job_id"])["status"] == "done"


def agent_track(index, codec, channels):
    from .library_helpers import track
    return track(index, codec=codec, channels=channels)


# ---------------------------------------------------------------- a real worker converts Dolby Vision
def dolby_vision_sample(path, seconds=4):
    """A small film whose picture carries a Dolby Vision RPU (profile 8.1 stands in: dovi_tool cannot make profile 7 from nothing), with one audio track."""
    work = path.parent / "build"
    work.mkdir(parents=True, exist_ok=True)
    run = lambda command: subprocess.run(command, check=True, capture_output=True, cwd=work)                                    # noqa: E731
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=24000/1001:duration={seconds}", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
         "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400:repeat-headers=1:hdr10-opt=1",
         "-c:a", "aac", "-b:a", "64k", "bl.mkv"])
    run(["ffmpeg", "-v", "error", "-y", "-i", "bl.mkv", "-map", "0:v", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "bl.hevc"])
    (work / "gen.json").write_text(json.dumps({"cm_version": "V29", "length": 96, "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                                                                                          "max_content_light_level": 1000, "max_frame_average_light_level": 400}}))
    run(["dovi_tool", "generate", "-j", "gen.json", "--rpu-out", "rpu.bin"])
    run(["dovi_tool", "inject-rpu", "-i", "bl.hevc", "--rpu-in", "rpu.bin", "-o", "dv.hevc"])
    run(["mkvmerge", "-o", str(path), "--default-duration", "0:24000/1001p", "dv.hevc", "--no-video", "bl.mkv"])
    shutil.rmtree(work)


@needs_dovi
def test_a_worker_converts_the_dolby_vision_data_in_place_and_keeps_the_original(rig, conn, monkeypatch):    # noqa: F811
    monkeypatch.setattr(agent.JobRunner, "require_profile_7", staticmethod(lambda facts: None))             # the sample is profile 8.1, the only kind dovi_tool can generate
    source = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995) Bluray-2160p.mkv"
    dolby_vision_sample(source)
    record_id = film(conn, info=DV7, size_gb=0.001)
    conn.execute("UPDATE library_file SET size_bytes = ?, mtime = ?, duration_seconds = 4.0 WHERE id = ?", (source.stat().st_size, source.stat().st_mtime, record_id))
    conn.commit()
    versions.approve(conn, "dv_remux", record_id, "Greg")
    conn.commit()
    rig.start()
    wait(lambda: (conn.commit() or True) and versions.get(conn, 1)["status"] in ("done", "failed", "blocked"), timeout=180, what="the conversion")
    row = versions.get(conn, 1)
    assert row["status"] == "done", row["message"]
    data = probe(source)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    dovi = next(d for d in video.get("side_data_list", []) if "DOVI" in d.get("side_data_type", ""))
    assert dovi["dv_profile"] == 8 and dovi["dv_bl_signal_compatibility_id"] == 1 and sum(1 for s in data["streams"] if s["codec_type"] == "audio") == 1
    originals = list((rig.work / "originals").glob("*/meta.json"))
    assert originals and json.loads(originals[0].read_text())["rel"] == "Movies/Heat (1995)/Heat (1995) Bluray-2160p.mkv"


# ---------------------------------------------------------------- a compatible audio track
def test_an_added_audio_track_is_first_and_the_default():
    change = {"keep_audio": [1, 2], "keep_subtitle": [], "audio_add": {"from": 1, "codec": "eac3", "channels": 6, "kbps": 640, "title": "Compatible (E-AC-3 5.1)"}}
    maps, codecs = agent._track_arguments(change, None, 0)
    assert maps[:2] == ["-map", "0:1"] and maps[2:6] == ["-map", "0:1", "-map", "0:2"]        # the added track, then the two kept ones
    assert codecs[codecs.index("-c:a:0") + 1] == "eac3" and "640k" in codecs and "title=Compatible (E-AC-3 5.1)" in codecs
    assert codecs[-4:] == ["-disposition:a", "0", "-disposition:a:0", "default"]


def test_a_bad_added_audio_track_is_refused():
    for bad in ({"from": 1, "codec": "mp3"}, {"from": "x", "codec": "aac"}, {"from": 1, "codec": "aac", "channels": 12}, {"from": 1, "codec": "eac3", "kbps": 5000}):
        with pytest.raises(agent.JobFailure):
            agent.clean_audio_add({"audio_add": bad})
    assert agent.clean_audio_add({}) is None
    assert agent.clean_audio_add({"audio_add": {"from": 1, "codec": "aac", "title": "x;rm -rf"}})["title"] == "xrm -rf"


@needs_ffmpeg
def test_ffmpeg_makes_the_compatible_track_first_and_keeps_the_original(tmp_path):
    src, out = str(tmp_path / "in.mkv"), str(tmp_path / "out.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2:r=10", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "6", "-c:v", "libx264",
                    "-c:a", "flac", "-t", "2", src], check=True)
    streams = probe(src)["streams"]
    audio_index = next(s["index"] for s in streams if s["codec_type"] == "audio")
    change = {"op": "keep_tracks", "container": "matroska", "keep_audio": [audio_index], "keep_subtitle": [],
              "audio_add": {"from": audio_index, "codec": "eac3", "channels": 6, "kbps": 640, "title": "Compatible (E-AC-3 5.1)"}}
    subprocess.run(agent.build_convert_command("ffmpeg", src, out, change), check=True, capture_output=True)
    audio = [s for s in probe(out)["streams"] if s["codec_type"] == "audio"]
    assert [a["codec_name"] for a in audio] == ["eac3", "flac"] and audio[0]["disposition"]["default"] == 1 and audio[1]["disposition"]["default"] == 0
    assert audio[0]["channels"] == 6 and audio[0]["tags"].get("title") == "Compatible (E-AC-3 5.1)"
