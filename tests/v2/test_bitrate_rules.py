"""Compression by video bitrate: the re-encode-above and aim-for numbers per picture size, the bitrate-target encode, and the new defaults."""
import json

from synchub import settings
from synchub.library import profiles, rules, size_policy as sp

from .agent_module import agent
from .test_size_policy import movie
from tests.v2.media_helpers import all_tabs


def cfg(**kw):
    return sp.Settings(on=True, mode="bitrate", trigger_mbps={"2160": 50.0, "1080": 25.0, "720": 0.0, "sd": 0.0}, caps_mbps={"2160": 35.0, "1080": 12.0, "720": 4.0, "sd": 2.0},
                       safety_gb={"2160": 0.0, "1080": 0.0}, rate_mode="target", **kw)


def uhd(mbps, **kw):
    info = {"hdr": "hdr10", "video_kbps": mbps * 1000, "width": 3840, "height": 2160, "transfer": "smpte2084", "bit_depth": 10}
    return movie(size_gb=mbps * 0.9 + 4, height=2160, width=3840, video_info=info, **kw)


def test_a_4k_film_is_only_touched_above_the_trigger_and_aimed_at_the_lower_bitrate():
    assert sp.decide(uhd(45), cfg=cfg()).action == sp.NONE
    decision = sp.decide(uhd(60), cfg=cfg())
    assert decision.action == sp.COMPRESS
    assert 34000 <= decision.profile["maxrate_kbps"] / 1.05 <= 35000 and decision.profile["rate_mode"] == "target"       # aimed just under 35 Mbps once audio is counted


def test_a_1080p_film_is_touched_only_when_it_is_remux_sized_and_720p_is_left_alone():
    def hd(mbps, height=1080, width=1920):
        return movie(size_gb=mbps * 0.9 + 4, height=height, width=width, video_info={"hdr": "sdr", "video_kbps": mbps * 1000, "width": width, "height": height})
    assert sp.decide(hd(20), cfg=cfg()).action == sp.NONE
    assert sp.decide(hd(30), cfg=cfg()).action == sp.COMPRESS
    assert sp.decide(hd(30, 720, 1280), cfg=cfg()).action == sp.NONE and "not compressed" in " ".join(sp.decide(hd(30, 720, 1280), cfg=cfg()).notes)


def test_the_quality_mode_keeps_the_older_constant_quality_encode():
    decision = sp.decide(uhd(60), cfg=sp.Settings(**{**cfg().__dict__, "rate_mode": "quality"}))
    assert "rate_mode" not in decision.profile


def test_an_episode_follows_the_same_rule_scaled_by_its_share():
    episode = uhd(30, media_type="TV Episode")
    assert sp.decide(episode, cfg=cfg()).action == sp.NONE                                      # TV is off by default
    assert sp.decide(episode, cfg=cfg(tv_on=True)).action == sp.COMPRESS                        # 30 Mbps is over half of the 50 Mbps trigger


def test_the_new_defaults_are_loaded_and_a_profile_can_override_the_trigger(conn):
    settings.set_value("size_mode", "bitrate", conn=conn)
    conn.commit()
    loaded = sp.load(conn)
    assert (loaded.trigger_mbps["2160"], loaded.caps_mbps["2160"], loaded.trigger_mbps["1080"], loaded.caps_mbps["1080"], loaded.trigger_mbps["720"]) == (50.0, 35.0, 25.0, 12.0, 0.0)
    assert loaded.rate_mode == "target" and loaded.dv_profile_7_8 == "remux" and loaded.safety_gb == {"2160": 0.0, "1080": 0.0}
    assert sp.load(conn, {"rules": {"trigger_2160": 40}}).trigger_mbps["2160"] == 40
    space = next(p for p in profiles.all_profiles(conn) if p["name"] == "Space saver")
    assert sp.load(conn, space).trigger_mbps["2160"] == 21                                      # unchanged behaviour: 1.5 x its own 14 Mbps


def test_profiles_show_what_makes_them_different(conn):
    by_name = {p["name"]: p for p in profiles.all_profiles(conn)}
    assert "Compresses above the bitrate limit" in by_name["Space saver"]["chips"]
    assert "Keeps all audio as it is" in by_name["Archive"]["chips"] and by_name["Archive"]["chips"] != by_name["Direct play"]["chips"]
    assert by_name["Move only"]["chips"] == ["Nothing changed"]


def test_the_encoder_aims_at_the_bitrate_in_target_mode_and_uses_crf_otherwise():
    base = {"video_codec": "hevc", "crf": 20, "preset": "medium", "maxrate_kbps": 36750, "bufsize_kbps": 73500}
    target = agent.video_arguments({}, agent.clean_profile({**base, "rate_mode": "target"}))
    assert target[target.index("-b:v") + 1] == "35000k" and "-crf" not in target
    quality = agent.video_arguments({}, agent.clean_profile(base))
    assert "-crf" in quality and "-b:v" not in quality
    assert "rate_mode" not in agent.clean_profile({**base, "rate_mode": "target", "maxrate_kbps": None})


def test_the_settings_page_saves_the_new_fields(app, client):
    saved = client.post("/api/library/settings", json={"size_mode": "bitrate", "trigger_mbps_2160": "55", "cap_mbps_2160": "36", "rate_mode": "quality", "variant_min_gb": "0"})
    assert saved.get_json()["ok"] is True
    loaded = sp.load()
    assert (loaded.trigger_mbps["2160"], loaded.caps_mbps["2160"], loaded.rate_mode) == (55.0, 36.0, "quality")
    assert client.post("/api/library/settings", json={"rate_mode": "fast"}).get_json()["ok"] is False
    page = all_tabs(client, "/library")
    assert 'id="setTrig2160" value="55"' in page and "When to compress" in page and "How to compress" in page
