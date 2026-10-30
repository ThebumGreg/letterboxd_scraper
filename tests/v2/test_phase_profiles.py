"""Profile rules, the bitrate rule, audio modes and the planner that uses them."""
import dataclasses
import json

import pytest

from synchub import settings
from synchub.library import audio, profiles, rules, size_policy as sp

GIB = 1024 ** 3


def film(size_gb=60, width=3840, height=2160, video_kbps=60000, duration=7200, streams=None, **extra):
    info = {"hdr": "sdr", "bit_depth": 10, "width": width, "height": height, "video_kbps": video_kbps}
    return {"media_type": "Movie/Unknown", "size_bytes": int(size_gb * GIB), "duration_seconds": duration, "video_info_json": json.dumps(info), "resolution": f"{width}x{height}",
            "frame_rate": 24.0, "streams_json": json.dumps(streams or []), "video_codec": "hevc", **extra}


@pytest.fixture
def bitrate(conn):
    settings.set_value("size_mode", "bitrate", conn=conn)
    settings.set_value("size_policy", "1", conn=conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------- rules
def test_a_profile_follows_the_options_until_it_overrides_a_rule(conn):
    settings.set_value("cap_mbps_1080", "9", conn=conn)
    conn.commit()
    assert profiles.rules(conn, profiles.get(conn, 1))["cap_1080"] == 9.0
    saver = profiles.get(conn, 8)
    assert profiles.rules(conn, saver)["cap_1080"] == 6.0 and profiles.rules(conn, saver)["audio_mode"] == "compatible_only"
    assert profiles.rules(conn, profiles.get(conn, 3))["size_policy"] is False                  # Move only never inherits compression


def test_rules_are_checked_and_unknown_ones_dropped():
    assert rules.clean({"cap_1080": "9", "bogus": 1, "audio_mode": "keep", "extra_copy": "1", "crf": ""}) == {"cap_1080": 9.0, "audio_mode": "keep", "extra_copy": True}
    for bad in ({"audio_mode": "loud"}, {"cap_1080": "9000"}, {"cap_1080": "x"}, {"extra_label": "bad/label"}):
        with pytest.raises(rules.RulesError):
            rules.clean(bad)


def test_a_profile_saves_and_reads_back_its_rules(conn):
    pid = profiles.save(conn, {"name": "Mine", "video_codec": "keep", "rules": {"cap_2160": 20, "extra_copy": True}}, by="t")
    assert profiles.get(conn, pid)["rules"] == {"cap_2160": 20.0, "extra_copy": True}
    with pytest.raises(profiles.ProfileError):
        profiles.save(conn, {"name": "Bad", "video_codec": "keep", "rules": {"cap_2160": 0}})


def test_effective_prefers_the_approval_then_the_scan_then_the_default(conn):
    assert profiles.effective(conn, {})["id"] == 1
    assert profiles.effective(conn, {"scan_profile_id": 8})["id"] == 8
    assert profiles.effective(conn, {"scan_profile_id": 8, "approved_profile_id": 9})["id"] == 9
    assert profiles.effective(conn, {"scan_profile_id": 8}, profile_id=7)["id"] == 7


# ---------------------------------------------------------------- the bitrate rule
def test_a_4k_film_far_over_the_ceiling_is_compressed_to_it(bitrate):
    decision = sp.decide(film(video_kbps=60000), [], sp.load(bitrate))
    assert decision.action == sp.COMPRESS
    assert 17500 < decision.profile["maxrate_kbps"] <= round(18000 * sp.MAXRATE_HEADROOM)       # 18 Mbps plus the headroom, less the share kept for audio
    assert 14 < decision.numbers["target_gb"] < 17.5                              # 18 Mbps for two hours


def test_a_film_near_the_ceiling_is_left_alone_even_if_big(bitrate):
    decision = sp.decide(film(size_gb=30, video_kbps=22000), [], sp.load(bitrate))      # 22 Mbps is under 1.5 x 18
    assert decision.action == sp.NONE and "Mbps" in " ".join(decision.notes)


def test_the_safety_size_catches_a_huge_file_with_a_low_bitrate(bitrate):
    assert sp.decide(film(size_gb=50, video_kbps=26000), [], sp.load(bitrate)).action == sp.COMPRESS      # under the trigger, over 45 GB


def test_1080p_and_tv_have_their_own_ceilings(bitrate):
    hd = film(size_gb=20, width=1920, height=1080, video_kbps=16000)
    assert sp.decide(hd, [], sp.load(bitrate)).action == sp.COMPRESS                  # 16 > 1.5 x 8
    episode = {**film(size_gb=3, width=1920, height=1080, video_kbps=8000, duration=2700), "media_type": "TV Episode"}
    settings.set_value("tv_size_policy", "1", conn=bitrate)
    bitrate.commit()
    assert sp.decide(episode, [], sp.load(bitrate)).action == sp.COMPRESS            # TV ceiling is half: 4 Mbps, trigger 6


def test_a_profile_changes_the_ceiling(bitrate):
    hd = film(size_gb=12, width=1920, height=1080, video_kbps=10000)
    assert sp.decide(hd, [], sp.load(bitrate, profiles.get(bitrate, 8))).action == sp.COMPRESS       # 10 > 1.5 x 6
    assert sp.decide(hd, [], sp.load(bitrate)).action == sp.NONE                                    # global ceiling 8: trigger 12


def test_dolby_vision_7_is_remuxed_not_compressed_when_the_profile_says_so(bitrate):
    rec = film(video_kbps=70000)
    info = json.loads(rec["video_info_json"])
    info.update(hdr="dv", dv_profile=7, transfer="smpte2084")
    rec["video_info_json"] = json.dumps(info)
    cfg = dataclasses.replace(sp.load(bitrate, profiles.get(bitrate, 7)), on=True)
    decision = sp.decide(rec, [], cfg)
    assert decision.action == sp.NONE and "8.1" in " ".join(decision.notes)


# ---------------------------------------------------------------- audio
TRUEHD = {"index": 1, "type": "audio", "codec": "truehd", "lang": "eng", "channels": 8, "default": True}
AC3 = {"index": 2, "type": "audio", "codec": "ac3", "lang": "eng", "channels": 6}
SUB = {"index": 3, "type": "subtitle", "codec": "subrip", "lang": "eng"}


def test_a_compatible_track_is_added_first_and_the_original_kept():
    change, note = audio.apply({"streams_json": json.dumps([TRUEHD, SUB])}, None, "add_compatible")
    assert change["audio_add"] == {"from": 1, "codec": "eac3", "channels": 6, "kbps": 640, "title": "Compatible (E-AC-3 5.1)"} and change["keep_audio"] == [1]
    assert "keeps the original TRUEHD" in note


def test_compatible_only_converts_the_main_track():
    change, note = audio.apply({"streams_json": json.dumps([TRUEHD])}, None, "compatible_only")
    assert change["audio_convert"] == {"1": {"codec": "eac3", "channels": 6, "kbps": 640}} and change["default_audio"] == 1 and "audio_add" not in change


def test_an_already_compatible_track_and_the_keep_mode_change_nothing():
    assert audio.apply({"streams_json": json.dumps([AC3])}, None, "add_compatible")[0] is None
    assert audio.apply({"streams_json": json.dumps([TRUEHD])}, None, "keep") == (None, None)


def test_stereo_lossless_gets_an_aac_stereo_track():
    flac = {"index": 1, "type": "audio", "codec": "flac", "lang": "eng", "channels": 2}
    assert audio.apply({"streams_json": json.dumps([flac])}, None, "add_compatible")[0]["audio_add"]["codec"] == "aac"


def test_the_main_track_is_the_english_one_that_is_kept():
    french = {"index": 1, "type": "audio", "codec": "dts", "lang": "fra", "channels": 6}
    english = {"index": 2, "type": "audio", "codec": "truehd", "lang": "eng", "channels": 8}
    change, _ = audio.apply({"streams_json": json.dumps([french, english])}, None, "add_compatible")
    assert change["audio_add"]["from"] == 2 and change["keep_audio"] == [1, 2]


# ---------------------------------------------------------------- names
def tv_record(**extra):
    return {"media_type": "TV Episode", "show_title": "Cold Case", "season_number": 2, "recommended_name": "Cold Case - S02E05.mkv", "file_name": "cold.case.s02e05.720p.mkv", "relative_path": "x/y.mkv",
            "root_name": "tfiles", **extra}


def test_jellyfin_style_drops_the_dash_in_tv_names():
    from synchub.library import layout
    from synchub.library import vocab as v
    jf, cur = v.LibraryConfig(naming_style="jellyfin"), v.LibraryConfig(naming_style="current")
    assert layout.relative_display(tv_record(), jf) == "TV Shows/Cold Case/Season 02/Cold Case S02E05.mkv"
    assert layout.relative_display(tv_record(), cur) == "TV Shows/Cold Case/Season 02/Cold Case - S02E05.mkv"
    assert layout.jellyfin_tv_name("Show - S01E01-E02.mkv") == "Show S01E01-E02.mkv" and layout.jellyfin_tv_name("Heat (1995).mkv") == "Heat (1995).mkv"


def test_a_profile_that_does_not_clean_names_keeps_the_files_own_name():
    from synchub.library import layout
    from synchub.library import vocab as v
    keep = v.LibraryConfig(naming_style="jellyfin", clean_names=False)
    assert layout.relative_display(tv_record(), keep).endswith("Season 02/cold.case.s02e05.720p.mkv")
    assert layout.relative_display(tv_record(approved_name="Cold Case S02E05.mkv"), keep).endswith("Cold Case S02E05.mkv")        # a name the person typed always wins


def test_an_existing_library_file_in_the_old_style_is_offered_a_rename(conn):
    from .library_helpers import add
    from synchub.library import recommend as rc
    from synchub.library import vocab as v
    settings.set_value("naming_style", "jellyfin", conn=conn)
    conn.commit()
    rec_id = add(conn, root="media", rel="TV Shows/Cold Case/Season 02/Cold Case - S02E05.mkv", name="Cold Case - S02E05.mkv", media_type="TV Episode")
    conn.execute("UPDATE library_file SET show_title = 'Cold Case', season_number = 2 WHERE id = ?", (rec_id,))
    conn.commit()
    from synchub.library import scan, store
    cfg = scan.config(conn)
    assert cfg.naming_style == "jellyfin"
    rec = store.get_record(conn, rec_id)
    r = rc.recommend(rec, cfg)
    assert r.kind == "rename" and "Cold Case S02E05.mkv" in r.headline
