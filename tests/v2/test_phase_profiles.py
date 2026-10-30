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


# ---------------------------------------------------------------- extra copy per profile
def test_the_extra_copy_follows_the_profile_of_the_film(conn):
    from synchub import versions
    assert versions.load(conn, profiles.get(conn, 1)).on == versions.load(conn).on
    pid = profiles.save(conn, {"name": "Phone copy", "video_codec": "keep", "rules": {"extra_copy": True, "extra_label": "720p", "extra_height": 720, "extra_min_gb": 5, "dv_profile_7_8": "remux"}}, by="t")
    cfg = versions.load(conn, profiles.get(conn, pid))
    assert (cfg.on, cfg.label, cfg.height, cfg.min_gb, cfg.dv7) == (True, "720p", 720, 5.0, True)
    off = profiles.save(conn, {"name": "No copy", "video_codec": "keep", "rules": {"extra_copy": False}}, by="t")
    assert versions.load(conn, profiles.get(conn, off)).on is False
    assert versions.load_for(conn, {"scan_profile_id": pid}).label == "720p"


# ---------------------------------------------------------------- the Library queue shows what the rules will do
def test_a_big_4k_film_with_lossless_audio_sits_in_format_conversion_with_the_rule_steps(conn):
    from .library_helpers import add
    from synchub.library import recommend as rc, scan
    settings.set_value("size_mode", "bitrate", conn=conn)
    settings.set_value("size_policy", "1", conn=conn)
    settings.set_value("audio_mode", "add_compatible", conn=conn)
    conn.commit()
    rec_id = add(conn, rel="Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=int(60 * GIB), media_type="Movie/Unknown")
    info = {"hdr": "sdr", "bit_depth": 10, "width": 3840, "height": 2160, "video_kbps": 60000}
    conn.execute("UPDATE library_file SET duration_seconds = 7200, video_codec = 'hevc', resolution = '3840x2160', video_info_json = ?, streams_json = ? WHERE id = ?",
                 (json.dumps(info), json.dumps([TRUEHD]), rec_id))
    conn.commit()
    rows = {rec["id"]: r for rec, r in rc.classify(conn, scan.config(conn))}
    r = rows[rec_id]
    assert r.group == rc.G_CONVERT and r.kind in ("rules", "convert")
    text = " ".join(r.steps)
    assert "Compress" in text and "compatible" in text
    conn.execute("UPDATE library_file SET scan_profile_id = 3 WHERE id = ?", (rec_id,))          # Move only: the rules do not apply
    conn.commit()
    assert {rec["id"]: r for rec, r in rc.classify(conn, scan.config(conn))}[rec_id].group != rc.G_CONVERT


# ---------------------------------------------------------------- port 80 next to 5001
def test_the_entrypoint_adds_the_extra_port_to_gunicorn(tmp_path):
    import os
    import pathlib
    import subprocess
    stub = tmp_path / "bin" / "gunicorn"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\necho \"$@\"\n")
    stub.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    script = pathlib.Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"
    env = {**os.environ, "PATH": f"{stub.parent}:{os.environ['PATH']}", "SYNCHUB_DATA_DIR": str(data), "SYNCHUB_EXTRA_PORT": "80", "PUID": str(os.getuid()), "PGID": str(os.getgid())}
    out = subprocess.run(["sh", str(script), "gunicorn", "--bind", "0.0.0.0:5001", "app"], env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert out == "--bind 0.0.0.0:80 --bind 0.0.0.0:5001 app"
    env.pop("SYNCHUB_EXTRA_PORT")
    assert subprocess.run(["sh", str(script), "gunicorn", "--bind", "0.0.0.0:5001", "app"], env=env, capture_output=True, text=True, check=True).stdout.strip() == "--bind 0.0.0.0:5001 app"


# ---------------------------------------------------------------- worker benchmark
def _worker(conn):
    from synchub import workers
    wid, _token = workers.create(conn, "bench-1")
    conn.execute("UPDATE worker SET capabilities = ? WHERE id = ?", (json.dumps({"features": ["benchmark"], "encoders": ["libx265"]}), wid))
    conn.commit()
    return wid


REPORT = {"agent": "1.4.0", "when": "2026-10-30T10:00:00Z", "seconds": 30, "cpu": "Test CPU", "cores": 8, "gpus": [], "source": "synthetic", "ok": True, "took_seconds": 90,
          "health": [{"name": "ffmpeg", "ok": True, "detail": "ffmpeg 7"}],
          "results": [{"encoder": "cpu", "label": "CPU (libx265)", "ok": True, "speed": 1.2, "fps": 28.8, "vmaf": 96.1, "vmaf_floor": 95.0, "passed": True},
                      {"encoder": "vaapi", "label": "GPU (VAAPI, hevc_vaapi)", "ok": False, "reason": "no device"}]}


def test_a_benchmark_is_requested_stored_and_shown_on_the_worker(conn):
    from synchub import workers
    wid = _worker(conn)
    workers.request_benchmark(conn, wid)
    assert workers.get(conn, wid)["benchmark_requested"] == 1
    workers.store_benchmark(conn, wid, REPORT)
    assert workers.get(conn, wid)["benchmark_requested"] == 0
    last = workers.last_benchmark(conn, wid)
    assert last["ok"] == 1 and "CPU 1.2x" in last["summary"] and last["report"]["results"][1]["reason"] == "no device"
    for _ in range(12):
        workers.store_benchmark(conn, wid, REPORT)
    assert conn.execute("SELECT COUNT(*) FROM worker_benchmark WHERE worker_id = ?", (wid,)).fetchone()[0] == workers.BENCHMARK_KEEP
    conn.execute("UPDATE worker SET capabilities = '{}' WHERE id = ?", (wid,))
    with pytest.raises(workers.WorkerError):
        workers.request_benchmark(conn, wid)                          # an agent without the feature cannot run it


def test_the_workers_page_shows_the_last_benchmark(client, conn):
    from synchub import workers
    wid = _worker(conn)
    assert "Not run yet" in client.get("/workers").get_data(as_text=True)
    workers.store_benchmark(conn, wid, REPORT)
    conn.commit()
    page = client.get("/workers").get_data(as_text=True)
    assert "Last benchmark" in page and "CPU (libx265)" in page and "failed: no device" in page
    assert client.post(f"/api/workers/{wid}/benchmark").get_json()["ok"] is True
    assert "Benchmark queued" in client.get("/workers").get_data(as_text=True)


def test_the_agent_benchmark_times_an_encoder_and_reports_it(tmp_path):
    import shutil
    import sys
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "agent"))
    import synchub_agent as agent

    class Cfg:
        ffmpeg, ffprobe, vaapi_device, work_dir = "ffmpeg", "ffprobe", "/dev/dri/renderD128", str(tmp_path)

    caps = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "encoders": ["libx265"], "vmaf": False, "cores": 2, "host": {"cpu": {"model": "Test CPU"}}}
    report = agent.benchmark_report(Cfg, caps, seconds=2)
    assert report["ok"] and report["results"][0]["encoder"] == "cpu" and report["results"][0]["speed"] > 0 and report["results"][0]["passed"]
    assert "CPU (libx265)" in agent.format_benchmark(report) and not list(tmp_path.iterdir())         # the scratch folder is removed
    broken = agent.benchmark_report(Cfg, {**caps, "ffmpeg": None}, seconds=2)
    assert broken["ok"] is False


# ---------------------------------------------------------------- quality facts on the record page
def test_the_record_page_shows_the_bitrate_against_the_ceiling_and_the_vmaf_of_a_finished_job(client, conn):
    from .library_helpers import add
    settings.set_value("size_mode", "bitrate", conn=conn)
    settings.set_value("size_policy", "1", conn=conn)
    conn.commit()
    rec_id = add(conn, rel="Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv", size=int(60 * GIB), media_type="Movie/Unknown")
    info = {"hdr": "sdr", "bit_depth": 10, "width": 3840, "height": 2160, "video_kbps": 60000}
    conn.execute("UPDATE library_file SET duration_seconds = 7200, video_codec = 'hevc', resolution = '3840x2160', video_info_json = ? WHERE id = ?", (json.dumps(info), rec_id))
    conn.commit()
    page = client.get(f"/library/record/{rec_id}").get_data(as_text=True)
    assert "Picture quality" in page and "60.0 Mbps" in page and "ceiling 18.0 Mbps for 4K" in page and "Would compress" in page
    from synchub import jobs
    job = jobs.create(conn, "library", title="x", ref=("library_file", rec_id), plan={"op": "convert"}, now="2026-10-30T10:00:00+00:00")
    conn.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?", (json.dumps({"vmaf": {"mean": 96.4, "min": 94.8, "floor": 95.0}}), job))
    conn.commit()
    page = client.get(f"/library/record/{rec_id}").get_data(as_text=True)
    assert "96.4" in page and "floor 95.0" in page


def test_the_workers_page_flags_a_worker_that_is_not_on_this_version(client, conn):
    from synchub import __version__, workers
    wid = _worker(conn)
    conn.execute("UPDATE worker SET version = '0.9.0' WHERE id = ?", (wid,))
    conn.commit()
    page = client.get("/workers").get_data(as_text=True)
    assert "update available" in page and "0.9.0" in page
    conn.execute("UPDATE worker SET version = ? WHERE id = ?", (__version__, wid))
    conn.commit()
    assert "update available" not in client.get("/workers").get_data(as_text=True)


# ---------------------------------------------------------------- approving together
def test_the_bulk_page_says_what_will_happen_and_can_set_the_profile_for_all(client, conn):
    from .library_helpers import add
    settings.set_value("size_mode", "bitrate", conn=conn)
    settings.set_value("size_policy", "1", conn=conn)
    conn.commit()
    ids = []
    for n in range(2):
        rec_id = add(conn, rel=f"Film {n} (200{n})/Film {n} (200{n}).mkv", name=f"Film {n} (200{n}).mkv", size=int(60 * GIB), media_type="Movie/Unknown")
        info = {"hdr": "sdr", "bit_depth": 10, "width": 3840, "height": 2160, "video_kbps": 60000}
        conn.execute("UPDATE library_file SET duration_seconds = 7200, video_codec = 'hevc', resolution = '3840x2160', video_info_json = ? WHERE id = ?", (json.dumps(info), rec_id))
        ids.append(rec_id)
    conn.commit()
    page = client.get("/library/bulk/convert").get_data(as_text=True)
    assert "What will happen" in page and "2 compressed, about" in page
    page = client.get("/library/bulk/convert?profile=3").get_data(as_text=True)          # Move only: no rule applies
    assert "2 copied without a rule applying" in page and "compressed, about" not in page
    done = client.post("/api/library/bulk/convert", json={"profile_id": 8, "conf": "medium"}).get_json()
    assert done["ok"] and done["done"] == 2
    assert {conn.execute("SELECT approved_profile_id FROM library_file WHERE id = ?", (i,)).fetchone()[0] for i in ids} == {8}


# ---------------------------------------------------------------- show folders with the year
def test_a_new_episode_gets_the_show_year_and_tag_but_a_library_folder_is_left_alone(conn):
    from synchub.library import recommend as rc
    from synchub.library import vocab as v
    conn.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('show', 'Cold Case', 2003, '4601')")
    settings.set_value("naming_style", "jellyfin", conn=conn)
    conn.commit()
    cfg = v.LibraryConfig(naming_style="jellyfin", destination_root="media")
    new = tv_record(root_name="tfiles")
    assert rc.cfg_for(conn, new, cfg, {}).show_folders == (("Cold Case", "Cold Case (2003)"),)
    from synchub.library import layout
    assert layout.relative_display(new, rc.cfg_for(conn, new, cfg, {})).startswith("TV Shows/Cold Case (2003)/Season 02/")
    settings.set_value("naming_tmdb_ids", "1", conn=conn)
    conn.commit()
    assert layout.relative_display(new, rc.cfg_for(conn, new, cfg, {})).startswith("TV Shows/Cold Case (2003) [tmdbid-4601]/")
    placed = tv_record(root_name="media")
    assert layout.relative_display(placed, rc.cfg_for(conn, placed, cfg, {})).startswith("TV Shows/Cold Case/Season 02/")
    settings.set_value("naming_style", "current", conn=conn)
    conn.commit()
    assert rc.cfg_for(conn, new, cfg, {}).show_folders == ()
