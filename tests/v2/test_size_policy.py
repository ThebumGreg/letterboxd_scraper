"""The movie size policy: when a film is compressed, to what, and what is left to a person."""
import json
import sys

import pytest

from synchub import jobs, settings, storage
from synchub.library import migrate, size_policy as sp
from synchub.library import vocab as v

from .agent_module import agent
from .library_helpers import CFG, add, plan, track

GIB = 1024 ** 3
media_analysis = sys.modules[agent.media_analysis.__name__] if getattr(agent, "media_analysis", None) else None


def movie(size_gb=40, minutes=120, audio=None, video_info=None, height=1080, width=1920, fps=24.0, **kw):
    audio = [track(1, codec="truehd", bitrate_kbps=4000)] if audio is None else audio
    rec = {"size_bytes": int(size_gb * GIB), "duration_seconds": minutes * 60, "media_type": "Movie/Unknown", "streams_json": json.dumps(audio), "video_codec": "h264",
           "resolution": f"{width}x{height}", "frame_rate": fps, "bitrate_kbps": size_gb * GIB * 8 / (minutes * 60) / 1000,
           "video_info_json": json.dumps(video_info) if video_info is not None else None}
    rec.update(kw)
    return rec


ON = sp.Settings(on=True)


def test_nothing_happens_while_the_policy_is_off_or_the_film_is_small_enough():
    assert sp.decide(movie(), cfg=sp.Settings()).action == sp.NONE
    assert sp.decide(movie(size_gb=28), cfg=ON).action == sp.NONE                  # between the target and the limit: left alone
    assert sp.decide(movie(size_gb=10), cfg=ON).action == sp.NONE


def test_tv_is_left_alone_unless_a_cap_is_set():
    episode = movie(size_gb=45, media_type="TV Episode")
    assert sp.decide(episode, cfg=ON).action == sp.NONE
    assert sp.decide(episode, cfg=sp.Settings(on=True, tv_max_gb=10, tv_target_gb=8)).action == sp.COMPRESS


def test_removing_tracks_that_brings_it_under_the_limit_is_tried_first():
    audio = [track(1, codec="truehd", bitrate_kbps=4000), track(2, language="ita", codec="truehd", bitrate_kbps=4000), track(3, language="fra", codec="truehd", bitrate_kbps=4000)]
    rec = movie(size_gb=32, audio=audio)                                           # two foreign TrueHD tracks are about 7 GB over two hours
    decision = sp.decide(rec, [plan(remove_audio=[(2, "ita"), (3, "fra")], keep_audio=(1,))], ON)
    assert decision.action == sp.NONE and "under the 30 GB limit" in decision.notes[0]
    assert sp.decide(rec, [], ON).action == sp.COMPRESS


def test_a_big_film_is_compressed_to_hevc_with_a_bitrate_cap_under_the_target():
    decision = sp.decide(movie(size_gb=45, video_info={"hdr": "sdr", "bit_depth": 8, "video_kbps": 45000}), [], ON)
    assert decision.action == sp.COMPRESS
    n, p = decision.numbers, decision.profile
    assert n["target_gb"] == 25 and 25000 < n["target_video_kbps"] < 28000                        # (25 GiB * 0.99 - 4 Mbps of TrueHD) over two hours
    assert p["video_codec"] == "hevc" and p["crf"] == 22 and p["ten_bit"] and p["maxrate_kbps"] == round(n["target_video_kbps"] * 1.05) and p["bufsize_kbps"] == 2 * p["maxrate_kbps"]
    assert p["max_bytes"] == 30 * GIB and "hdr" not in p and decision.summary.startswith("Compress 45.0 GB")


def test_the_target_never_asks_for_more_than_most_of_what_the_source_has():
    decision = sp.decide(movie(size_gb=31, video_info={"hdr": "sdr", "video_kbps": 20000}, minutes=60), [], ON)
    assert decision.numbers["target_video_kbps"] <= 18000


def test_a_target_that_would_starve_the_picture_goes_to_a_person_with_the_numbers():
    decision = sp.decide(movie(size_gb=60, minutes=240, height=2160, width=3840, fps=60.0), [], ON)   # four hours of 60 fps 4K into 25 GB
    assert decision.action == sp.REVIEW and "bits per pixel" in decision.reasons[0] and decision.numbers["bpp"] < decision.numbers["bpp_floor"] == 0.05
    assert decision.profile["video_codec"] == "hevc"                                              # approving accepts it


def test_audio_that_leaves_no_room_blocks_compression():
    rec = movie(size_gb=45, audio=[track(i, codec="truehd", bitrate_kbps=100000) for i in range(1, 4)])
    assert sp.decide(rec, [], ON).action == sp.BLOCKED


def test_hdr10_and_hlg_are_kept():
    info = {"hdr": "hdr10", "bit_depth": 10, "transfer": "smpte2084", "primaries": "bt2020", "matrix": "bt2020nc", "master_display": "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)", "max_cll": "1000,400"}
    decision = sp.decide(movie(size_gb=45, video_info=info), [], ON)
    assert decision.action == sp.COMPRESS and decision.profile["hdr"] == {"kind": "hdr10", "transfer": "smpte2084", "primaries": "bt2020", "matrix": "bt2020nc",
                                                                         "master_display": "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)", "max_cll": "1000,400"}
    assert any("HDR10 is kept" in n for n in decision.notes)
    hlg = sp.decide(movie(size_gb=45, video_info={"hdr": "hlg", "bit_depth": 10, "transfer": "arib-std-b67"}), [], ON)
    assert hlg.profile["hdr"]["kind"] == "hlg"


@pytest.mark.parametrize("dv_profile, setting, action", [("5", "review", sp.BLOCKED), ("5", "hdr10", sp.COMPRESS), ("8", "hdr10", sp.COMPRESS), ("7", "review", sp.REVIEW), ("4", "hdr10", sp.REVIEW)])
def test_dolby_vision_depends_on_its_profile(dv_profile, setting, action):
    info = {"hdr": "dv", "dv_profile": dv_profile, "bit_depth": 10, "transfer": "smpte2084"}
    cfg = sp.Settings(on=True, dv_profile_5=setting if dv_profile == "5" else "review", dv_profile_7_8=setting if dv_profile in ("7", "8") else "hdr10")
    decision = sp.decide(movie(size_gb=45, video_info=info), [], cfg)
    assert decision.action == action
    if dv_profile in ("7", "8") and action == sp.COMPRESS:
        assert any("Dolby Vision layer is lost" in n for n in decision.notes)


def test_interlaced_video_is_deinterlaced_and_already_hevc_is_warned_about():
    decision = sp.decide(movie(size_gb=45, video_info={"hdr": "sdr", "interlaced": True}, video_codec="hevc"), [], ON)
    assert decision.profile["deinterlace"] is True and any("generation loss" in n for n in decision.notes)


def test_settings_come_from_the_settings_table(conn):
    assert sp.load(conn).on is False
    for key, value in (("size_policy", "1"), ("movie_max_gb", "20"), ("movie_target_gb", "15"), ("size_crf", "19"), ("dolby_vision_profile_5", "hdr10")):
        settings.set_value(key, value, conn=conn)
    loaded = sp.load(conn)
    assert (loaded.on, loaded.movie_max_gb, loaded.movie_target_gb, loaded.crf, loaded.dv_profile_5) == (True, 20.0, 15.0, 19, "hdr10")


def test_applying_a_decision_makes_an_hevc_matroska_job_plan():
    decision = sp.decide(movie(size_gb=45), [], ON)
    out, dest = sp.apply({"op": "copy", "revision": 1}, decision, "Movies/Heat (1995)/Heat (1995).mp4")
    assert out["op"] == "convert" and out["change"]["video"] == {"action": "transcode", "codec": "hevc"} and out["change"]["container"] == "matroska"
    assert dest == "Movies/Heat (1995)/Heat (1995).mkv" and out["needs"] == ["profiles"] and out["profile"]["maxrate_kbps"] and out["size_policy"]["target_gb"] == 25


def test_a_profile_the_person_chose_keeps_its_quality_and_gets_only_the_cap():
    decision = sp.decide(movie(size_gb=45), [], ON)
    chosen = {"name": "HEVC high", "video_codec": "hevc", "crf": 18, "preset": "slow", "ten_bit": True, "audio_kbps": 192}
    out, _ = sp.apply({"op": "convert", "profile": chosen}, decision, None)
    assert out["profile"]["crf"] == 18 and out["profile"]["preset"] == "slow" and out["profile"]["maxrate_kbps"] == decision.profile["maxrate_kbps"]


# ---------------------------------------------------------------- queued Library jobs
def test_an_approved_big_film_is_queued_as_a_compression_when_the_policy_is_on(conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media"})
    settings.set_value("size_policy", "1", conn=conn)
    record_id = add(conn, rel="Movies/Heat.mkv", name="Heat (1995).mkv", size=45 * GIB, action="copy_rename")
    conn.execute("UPDATE library_file SET duration_seconds = 7200, media_type = 'Movie/Unknown' WHERE id = ?", (record_id,))
    from synchub.library import recommend as rc
    rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG)
    conn.commit()
    job = jobs.get(conn, conn.execute("SELECT id FROM job WHERE ref_type = 'library_file' ORDER BY id DESC").fetchone()[0])
    assert job["plan"]["op"] == "convert" and job["plan"]["profile"]["video_codec"] == "hevc" and job["plan"]["profile"]["max_bytes"] == 30 * GIB
    assert job["plan"]["needs"] == ["profiles"] and job["dest_rel"].endswith(".mkv")


def test_with_the_policy_off_the_same_film_is_just_copied(conn):
    record_id = add(conn, rel="Movies/Heat.mkv", name="Heat (1995).mkv", size=45 * GIB, action="copy_rename")
    from synchub.library import recommend as rc
    rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG)
    conn.commit()
    job = jobs.get(conn, conn.execute("SELECT id FROM job WHERE ref_type = 'library_file' ORDER BY id DESC").fetchone()[0])
    assert job["plan"]["op"] == "copy" and "profile" not in job["plan"]


# ---------------------------------------------------------------- the agent's side
def test_the_agent_accepts_the_size_cap_and_hdr_facts_and_nothing_else():
    cleaned = agent.clean_profile({"video_codec": "hevc", "crf": 22, "preset": "medium", "ten_bit": True, "maxrate_kbps": 22000, "bufsize_kbps": 44000, "max_bytes": 30 * GIB,
                                   "deinterlace": True, "hdr": {"kind": "hdr10", "master_display": "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)", "max_cll": "1000,400", "evil": "x"}})
    assert cleaned["maxrate_kbps"] == 22000 and cleaned["bufsize_kbps"] == 44000 and cleaned["max_bytes"] == 30 * GIB and cleaned["deinterlace"]
    assert cleaned["hdr"] == {"kind": "hdr10", "master_display": "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)", "max_cll": "1000,400"}
    hostile = agent.clean_profile({"video_codec": "hevc", "crf": 22, "preset": "medium", "hdr": {"kind": "hdr10", "master_display": "a:b;rm -rf /"}, "maxrate_kbps": "x"})
    assert "maxrate_kbps" not in hostile and "master_display" not in hostile["hdr"]
    assert "maxrate_kbps" not in agent.clean_profile({"video_codec": "h264", "crf": 20, "preset": "medium", "maxrate_kbps": 5000})


def test_the_ffmpeg_command_carries_the_cap_and_the_hdr_tags():
    profile = {"video_codec": "hevc", "crf": 22, "preset": "medium", "ten_bit": True, "maxrate_kbps": 22000, "bufsize_kbps": 44000,
               "hdr": {"kind": "hdr10", "master_display": "G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)", "max_cll": "1000,400"}}
    command = agent.build_convert_command("ffmpeg", "/in.mkv", "/out.partial", {"video": {"action": "transcode"}}, profile)
    text = " ".join(command)
    params = command[command.index("-x265-params") + 1]
    assert "-pix_fmt yuv420p10le" in text and "vbv-maxrate=22000" in params and "vbv-bufsize=44000" in params
    assert "transfer=smpte2084" in params and "master-display=G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)" in params and "max-cll=1000,400" in params and "colorprim=bt2020" in params
    assert "-color_trc smpte2084" in text and "-colorspace bt2020nc" in text
    plain = " ".join(agent.build_convert_command("ffmpeg", "/in.mkv", "/out.partial", {"video": {"action": "transcode"}}, {"video_codec": "hevc", "crf": 22, "preset": "medium", "deinterlace": True}))
    assert "-vf yadif" in plain and "vbv-maxrate" not in plain and "colorprim" not in plain


def stream(**kw):
    base = {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084", "color_primaries": "bt2020",
            "color_space": "bt2020nc", "field_order": "progressive", "bit_rate": "40000000"}
    base.update(kw)
    return base


def test_video_facts_read_hdr_dolby_vision_and_bitrate_from_ffprobe():
    ma = agent.media_analysis
    facts = ma.video_facts(stream(side_data_list=[
        {"side_data_type": "Mastering display metadata", "red_x": "34000/50000", "red_y": "16000/50000", "green_x": "13250/50000", "green_y": "34500/50000", "blue_x": "7500/50000",
         "blue_y": "3000/50000", "white_point_x": "15635/50000", "white_point_y": "16450/50000", "max_luminance": "10000000/10000", "min_luminance": "50/10000"},
        {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400}]))
    assert facts["hdr"] == "hdr10" and facts["bit_depth"] == 10 and facts["video_kbps"] == 40000.0 and facts["interlaced"] is False
    assert facts["master_display"] == "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)" and facts["max_cll"] == "1000,400"
    dv = ma.video_facts(stream(side_data_list=[{"side_data_type": "DOVI configuration record", "dv_profile": 8, "dv_bl_signal_compatibility_id": 1}]))
    assert dv["hdr"] == "dv" and dv["dv_profile"] == 8
    sdr = ma.video_facts(stream(color_transfer="bt709", pix_fmt="yuv420p", field_order="tt", bit_rate=None))
    assert sdr["hdr"] == "sdr" and sdr["bit_depth"] == 8 and sdr["interlaced"] is True and sdr["video_kbps"] is None
    assert ma.video_facts(stream(color_transfer="arib-std-b67"))["hdr"] == "hlg"
