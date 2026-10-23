import json
import os
import stat
import time

import pytest

from .agent_media import HAVE_FFMPEG, analysis as a, make_media

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")


def settings(**kw):
    kw.setdefault("retry_delay", 0.01)
    return a.Settings(**kw)


def analyse(path, rel=None, **kw):
    path = str(path)
    st = os.stat(path)
    return a.analyze_file(settings(**kw), rel or os.path.basename(path), path, st.st_size, st.st_mtime)


def plans(rec):
    return json.loads(rec["proposed_track_changes_json"] or "[]")


# ---------------------------------------------------------------- naming (the original's rules, as ported)
@pytest.mark.parametrize("filename, rel, name, kind, show, season", [
    ("The.Matrix.1999.1080p.BluRay.x264-SPARKS.mkv", "The.Matrix.1999.1080p.BluRay.x264-SPARKS.mkv", "The Matrix (1999).mkv", "Movie/Unknown", None, None),
    ("heat 1995.mkv", "heat 1995.mkv", "Heat (1995).mkv", "Movie/Unknown", None, None),
    ("Spider-Man.2002.720p.WEB-DL.AAC2.0.H.264-NTb.mp4", "Spider-Man.2002.720p.WEB-DL.AAC2.0.H.264-NTb.mp4", "Spider-Man (2002).mp4", "Movie/Unknown", None, None),
    ("Show.Name.S01E02.720p.HDTV.x264-GRP.mkv", "TV Shows/Show Name/Season 1/Show.Name.S01E02.720p.HDTV.x264-GRP.mkv", "Show Name - S01E02.mkv", "TV Episode", "Show Name", 1),
    ("show.s02e05-e06.mkv", "TV/show/show.s02e05-e06.mkv", "Show - S02E05-E06.mkv", "TV Episode", "Show", 2),
    ("1x03.mkv", "Series/Lost/Season 1/1x03.mkv", "Lost - S01E03.mkv", "TV Episode", "Lost", 1),
    ("[www.torrentsite.com] Alien 1979.mkv", "[www.torrentsite.com] Alien 1979.mkv", "Alien (1979).mkv", "Movie/Unknown", None, None),
])
def test_names_are_cleaned_the_way_the_original_tool_cleaned_them(filename, rel, name, kind, show, season):
    n = a.clean_filename(filename, rel)
    assert (n["name"], n["media_type"], n["show"] or None, n["season"]) == (name, kind, show, season)


def test_a_show_is_taken_from_the_folders_inside_the_share_only():
    inside = a.clean_filename("S01E01.mkv", "TV Shows/The Wire/Season 1/S01E01.mkv")
    assert inside["show"] == "The Wire" and inside["season"] == 1
    bare = a.clean_filename("S01E01.mkv", "S01E01.mkv")
    assert bare["show"] == "Unknown Show" and bare["review"] and bare["label"] == "Low"


def test_a_name_with_characters_windows_cannot_hold_is_flagged():
    assert a.recommended_name_problems("Heat (1995).mkv") == []
    assert any("invalid" in p for p in a.recommended_name_problems("Heat: 1995.mkv")) and any("reserved" in p for p in a.recommended_name_problems("nul.mkv"))
    assert any("trailing" in p for p in a.recommended_name_problems("Heat .mkv"))
    assert a.clean_filename("Heat: Part 1 1995.mkv", "x.mkv")["name"] == "Heat - Part 1 (1995).mkv"


# ---------------------------------------------------------------- analysing real files
@needs_ffmpeg
def test_a_plain_movie_is_copied_unchanged_with_a_clean_name_and_nothing_to_review(tmp_path):
    path = make_media(tmp_path / "Heat.1995.1080p.BluRay.x264-GRP.mkv", audio=(("eng", "aac"),))
    rec = analyse(path)
    assert rec["recommended_name"] == "Heat (1995).mkv" and rec["media_type"] == "Movie/Unknown" and rec["analysis_status"] == "completed"
    assert rec["integrity_status"] == a.INT_TAIL and rec["integrity_mode"] == "tail" and "NOT proof" in rec["integrity_scope"]
    assert rec["container"].startswith("matroska") and rec["video_codec"] == "h264" and rec["resolution"] == "160x120" and rec["audio_codecs"] == "aac"
    assert rec["video_streams"] == 1 and rec["audio_streams"] == 1 and rec["subtitle_streams"] == 0 and rec["duration_seconds"] == pytest.approx(2.0, abs=0.3)
    assert rec["english_audio_status"] == "present" and rec["proposed_track_changes_json"] is None and rec["logic_version"] == a.LOGIC_VERSION
    f = rec["findings"]
    assert f["action"] == "copy_rename" and f["reasons"] == [] and f["naming_error"] is False and rec["file_name"].startswith("Heat.1995")
    assert [s["lang"] for s in json.loads(rec["streams_json"])] == ["eng"]


@needs_ffmpeg
def test_foreign_tracks_are_proposed_for_removal_only_when_english_is_there(tmp_path):
    path = make_media(tmp_path / "Movie 2001.mkv", audio=(("eng", "aac"), ("ita", "aac")), subtitles=("eng", "fra"))
    rec = analyse(path)
    (proposal,) = plans(rec)
    assert proposal["op"] == "keep_tracks" and [t["language"] for t in proposal["remove_audio"]] == ["ita"] and [t["language"] for t in proposal["remove_subtitle"]] == ["fra"]
    assert proposal["keep_audio"] == [1] and proposal["default_audio"] == 1 and ("tracks", ) in [(r[0],) for r in rec["findings"]["reasons"]]
    assert rec["findings"]["action"] == "copy_rename" and rec["audio_flags"] and "AUDIO_MULTIPLE_STREAMS" in rec["audio_flags"]
    foreign_only = analyse(make_media(tmp_path / "Foreign 2001.mkv", audio=(("ita", "aac"),)))
    assert plans(foreign_only) == [] and foreign_only["english_audio_status"] == "none"
    assert any(c == "audio" and "No English audio" in t for c, t in foreign_only["findings"]["reasons"])


@needs_ffmpeg
def test_an_unknown_language_is_held_for_a_person_and_a_title_can_settle_it(tmp_path):
    unknown = analyse(make_media(tmp_path / "Unknown 2001.mkv", audio=((None, "aac"),)))
    assert unknown["english_audio_status"] == "unknown" and any(c == "audio" for c, _ in unknown["findings"]["reasons"]) and "AUDIO_ENGLISH_UNCONFIRMED" in unknown["audio_flags"]
    titled = analyse(make_media(tmp_path / "Titled 2001.mkv", audio=((None, "aac"),), titles={("a", 0): "English 2.0"}))
    assert titled["english_audio_status"] == "present" and "LANG_INFERRED_FROM_TITLE" in titled["audio_flags"] and not any(c == "audio" for c, _ in titled["findings"]["reasons"])


@needs_ffmpeg
def test_a_tv_episode_is_placed_by_its_show_and_season(tmp_path):
    path = make_media(tmp_path / "TV Shows" / "Show Name" / "Season 1" / "Show.Name.S01E02.720p.HDTV.mkv")
    rec = analyse(path, rel="TV Shows/Show Name/Season 1/Show.Name.S01E02.720p.HDTV.mkv")
    assert (rec["media_type"], rec["show_title"], rec["season_number"], rec["recommended_name"]) == ("TV Episode", "Show Name", 1, "Show Name - S01E02.mkv")


@needs_ffmpeg
def test_a_legacy_container_is_a_lossless_remux_and_a_legacy_codec_is_a_reencode(tmp_path):
    remux = analyse(make_media(tmp_path / "Old Movie 1999.avi", video="libx264", audio=(("eng", "mp3"),)))
    (plan,) = plans(remux)
    assert remux["findings"]["action"] == "remux_copy" and plan["op"] == "convert" and plan["target_extension"] == ".mkv" and plan["video"]["action"] == "copy"
    assert remux["recommended_name"] == "Old Movie (1999).mkv"                                                  # the name carries the new extension
    reencode = analyse(make_media(tmp_path / "Older Movie 1999.avi", video="mpeg4", audio=(("eng", "mp3"),)))
    assert reencode["findings"]["action"] == "transcode_copy" and plans(reencode)[0]["video"] == {"action": "transcode", "from": "mpeg4", "codec": "h264"}


@needs_ffmpeg
def test_a_file_that_is_not_media_fails_the_analysis_and_goes_to_a_person(tmp_path):
    junk = tmp_path / "Junk 2000.mkv"
    junk.write_bytes(os.urandom(5000))
    rec = analyse(junk)
    assert rec["analysis_status"] == "failed" and rec["integrity_status"] == a.INT_AUDIT_ERROR and rec["findings"]["action"] == "manual_review"
    assert rec["audit_error"] and rec["proposed_track_changes_json"] is None and rec["english_audio_status"] is None
    assert rec["findings"]["reasons"] == [] and json.loads(rec["errors_json"]) == [rec["audit_error"]]


@needs_ffmpeg
def test_an_empty_file_is_flagged_as_such(tmp_path):
    empty = tmp_path / "Empty 2000.mkv"
    empty.write_bytes(b"")
    rec = analyse(empty)
    assert rec["findings"]["action"] == "manual_review" and ["integrity", "File is empty"] in rec["findings"]["reasons"]


@needs_ffmpeg
def test_a_damaged_file_is_found_by_decoding(tmp_path):
    good = make_media(tmp_path / "Damaged 2000.mkv", seconds=4)
    data = bytearray(good.read_bytes())
    for i in range(len(data) // 3, len(data) // 3 + 400):
        data[i] = (data[i] + 91) % 256
    damaged = tmp_path / "Bad 2000.mkv"
    damaged.write_bytes(bytes(data))
    rec = analyse(damaged, integrity_mode="full")
    assert rec["integrity_status"] in (a.INT_DECODE_ERROR, a.INT_AUDIT_ERROR) and rec["findings"]["action"] == "manual_review"


@needs_ffmpeg
def test_the_integrity_mode_decides_what_is_proven(tmp_path):
    path = make_media(tmp_path / "Modes 2000.mkv")
    assert analyse(path, integrity_mode="metadata")["integrity_status"] == a.INT_METADATA
    assert analyse(path, integrity_mode="full")["integrity_status"] == a.INT_FULL and analyse(path, integrity_mode="full")["integrity_scope"] == "Entire file decoded"
    bad = analyse(path, integrity_mode="nonsense")
    assert bad["integrity_status"] == a.INT_AUDIT_ERROR and "Unknown integrity mode" in bad["audit_error"]


# ---------------------------------------------------------------- when the tools misbehave
def fake_tool(tmp_path, body, name="tool"):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_a_tool_that_takes_too_long_is_a_timeout_not_a_verdict(tmp_path):
    clip = tmp_path / "T 2000.mkv"
    clip.write_bytes(b"x" * 100)
    slow = fake_tool(tmp_path, "sleep 30")
    started = time.monotonic()
    rec = analyse(clip, ffprobe=slow, ffprobe_timeout=0.5, retry_attempts=2)
    assert time.monotonic() - started < 6
    assert rec["integrity_status"] == a.INT_TIMEOUT and rec["analysis_status"] == "incomplete" and "timed out" in rec["audit_error"]


def test_an_io_error_from_the_tool_is_a_temporary_problem_that_is_retried_and_reported(tmp_path):
    clip = tmp_path / "T 2000.mkv"
    clip.write_bytes(b"x" * 100)
    counter = tmp_path / "count"
    flaky = fake_tool(tmp_path, f'echo x >> {counter}; echo "Input/output error" >&2; exit 1')
    rec = analyse(clip, ffprobe=flaky, retry_attempts=3)
    assert rec["integrity_status"] == a.INT_IO_ERROR and rec["analysis_status"] == "incomplete" and len(counter.read_text().split()) == 3


def test_cancelling_stops_a_running_tool_at_once(tmp_path):
    clip = tmp_path / "T 2000.mkv"
    clip.write_bytes(b"x" * 100)
    slow = fake_tool(tmp_path, "sleep 30")
    start = time.monotonic()

    def check():
        if time.monotonic() - start > 0.4:
            raise KeyboardInterrupt("cancelled")
    with pytest.raises(KeyboardInterrupt):
        analyse(clip, ffprobe=slow, ffprobe_timeout=60, check=check)
    assert time.monotonic() - start < 3


def test_a_missing_tool_is_an_audit_error_with_a_reason(tmp_path):
    clip = tmp_path / "T 2000.mkv"
    clip.write_bytes(b"x" * 100)
    rec = analyse(clip, ffprobe=str(tmp_path / "no-such-ffprobe"))
    assert rec["integrity_status"] == a.INT_AUDIT_ERROR and rec["analysis_status"] == "failed" and "no-such-ffprobe" in rec["audit_error"]


# ---------------------------------------------------------------- finding files
def tree(root, names):
    for name in names:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * (len(name) + 1))


def test_the_walk_lists_video_files_inside_a_share_with_forward_slash_paths_in_order(tmp_path):
    tree(tmp_path, ["TV Shows/Show/Season 1/b.mkv", "TV Shows/Show/Season 1/a.MKV", "Movies/Heat/Heat.mp4", "Movies/Heat/notes.txt", "Movies/Heat/poster.jpg", "top.avi", "Movies/x.srt"])
    files, unreadable, errors = a.walk_inventory(settings(), str(tmp_path), "", a.ExcludeRules([], "tfiles"))
    assert [f["rel"] for f in files] == ["top.avi", "Movies/Heat/Heat.mp4", "TV Shows/Show/Season 1/a.MKV", "TV Shows/Show/Season 1/b.mkv"]
    assert unreadable == [] and errors == [] and all(f["size"] > 0 and f["mtime"] > 0 for f in files)


def test_a_scan_of_one_folder_stays_in_that_folder(tmp_path):
    tree(tmp_path, ["TV Shows/Show/a.mkv", "Movies/Heat/h.mkv"])
    files, _, _ = a.walk_inventory(settings(), str(tmp_path), "TV Shows", a.ExcludeRules([], "tfiles"))
    assert [f["rel"] for f in files] == ["TV Shows/Show/a.mkv"]


def test_excluded_folders_are_left_out_by_name_wildcard_or_share_path(tmp_path):
    tree(tmp_path, ["Movies/Heat/h.mkv", "Movies/Extras/e.mkv", "Movies/Heat/Sample/s.mkv", "@eaDir/x.mkv", "TV Shows/Show/Featurettes/f.mkv", "Old/Stuff/o.mkv", "Keep/k.mkv"])
    skipped = []
    rules = a.ExcludeRules(["extras", "*Sample*", "@eaDir", "tfiles:Old/Stuff", "media:Keep", "Feature*"], "tfiles")
    files, _, _ = a.walk_inventory(settings(), str(tmp_path), "", rules, on_skip=skipped.append)
    assert sorted(f["rel"] for f in files) == ["Keep/k.mkv", "Movies/Heat/h.mkv"]                    # 'media:Keep' is about another share: it does not apply here
    assert sorted(skipped) == ["@eaDir", "Movies/Extras", "Movies/Heat/Sample", "Old/Stuff", "TV Shows/Show/Featurettes"]


def test_exclude_rules_say_which_files_are_inside_a_left_out_folder():
    rules = a.ExcludeRules(["Extras", "tfiles:Old"], "TFiles")
    assert rules.path("Movies/Extras/e.mkv") and rules.path("OLD/deep/er/x.mkv") and rules.path("extras/e.mkv")
    assert not rules.path("Movies/Heat/h.mkv") and not rules.path("Extras.mkv") and not rules.path("Oldies/x.mkv")
    assert a.ExcludeRules(["  ", ""], "tfiles").names == []


def test_a_file_that_cannot_be_read_is_listed_with_the_reason_and_a_folder_that_cannot_be_listed_is_reported(tmp_path, monkeypatch):
    tree(tmp_path, ["a/ok.mkv", "a/bad.mkv", "b/inside.mkv"])
    real_stat, real_walk = os.stat, os.walk

    def stat(path, *args, **kw):
        if str(path).endswith("bad.mkv"):
            raise OSError(5, "Input/output error", str(path))
        return real_stat(path, *args, **kw)

    def walk(top, onerror=None, **kw):
        for entry in real_walk(top, onerror=onerror, **kw):
            yield entry
        if onerror:
            onerror(OSError(13, "Permission denied", str(tmp_path / "locked")))
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "walk", walk)
    files, unreadable, errors = a.walk_inventory(settings(retry_attempts=2), str(tmp_path), "", a.ExcludeRules([], "tfiles"))
    assert sorted(f["rel"] for f in files) == ["a/ok.mkv", "b/inside.mkv"]
    assert [(u["rel"], u["file_name"]) for u in unreadable] == [("a/bad.mkv", "bad.mkv")] and "stat failed" in unreadable[0]["unreadable_reason"] and "Input/output" in unreadable[0]["unreadable_reason"]
    assert errors and "locked" in errors[0]


# ---------------------------------------------------------------- reading files to find duplicates
def hfile(tmp_path, name, content):
    p = tmp_path / name
    p.write_bytes(content)
    st = os.stat(p)
    return {"id": abs(hash(name)) % 10_000, "path": str(p), "size": st.st_size, "mtime": st.st_mtime}


def test_identical_files_get_the_same_hash_and_different_ones_are_ruled_out_without_a_full_read(tmp_path):
    head = os.urandom(3000)
    a1 = hfile(tmp_path, "a1", head + b"A" * 5000)
    a2 = hfile(tmp_path, "a2", head + b"A" * 5000)
    c1 = hfile(tmp_path, "c1", os.urandom(8000))
    a_results = {r["id"]: r for r in a.hash_group(settings(partial_read_bytes=1024), [a1, a2, c1], known=0)}
    import hashlib
    expected = hashlib.sha256((head + b"A" * 5000)).hexdigest()
    assert a_results[a1["id"]]["sha256"] == a_results[a2["id"]]["sha256"] == expected
    assert a_results[c1["id"]].get("ruled_out") is True and "sha256" not in a_results[c1["id"]] and a_results[c1["id"]]["partial"]
    assert a_results[a1["id"]]["size"] == 8000 and a_results[a1["id"]]["mtime"] == a1["mtime"]


def test_when_other_files_of_that_size_are_already_known_nothing_is_ruled_out_early(tmp_path):
    x = hfile(tmp_path, "x", os.urandom(8000))
    y = hfile(tmp_path, "y", os.urandom(8000))
    results = a.hash_group(settings(partial_read_bytes=1024), [x, y], known=1)
    assert all("sha256" in r for r in results) and len(results) == 2                      # the unknown copy may match a hash SyncHub already holds


def test_small_files_are_hashed_in_full_without_a_prefilter(tmp_path):
    x, y = hfile(tmp_path, "x", b"1" * 100), hfile(tmp_path, "y", b"2" * 100)
    assert all("sha256" in r for r in a.hash_group(settings(), [x, y], known=0))


def test_a_file_that_cannot_be_read_is_reported_and_the_others_still_are_hashed(tmp_path):
    good = hfile(tmp_path, "good", b"g" * 100)
    gone = {"id": 777, "path": str(tmp_path / "gone"), "size": 100, "mtime": 1.0}
    results = {r["id"]: r for r in a.hash_group(settings(retry_attempts=1), [good, gone], known=0)}
    assert "sha256" in results[good["id"]] and "Hash failed" in results[777]["error"]


def test_a_file_that_changed_while_hashing_is_an_error_not_a_wrong_hash(tmp_path):
    f = hfile(tmp_path, "f", b"z" * 100)
    f["size"] = 99
    (result,) = a.hash_group(settings(retry_attempts=1), [f, hfile(tmp_path, "g", b"y" * 99)][:1], known=0)
    assert "size changed" in result["error"]


def test_hashing_reports_progress_and_each_result_as_it_exists(tmp_path):
    files = [hfile(tmp_path, "p1", b"1" * 5000), hfile(tmp_path, "p2", b"1" * 5000)]
    got, seen = [], []
    a.hash_group(settings(hash_chunk=1000), files, known=0, progress=seen.append, on_result=got.append)
    assert sum(seen) == 10000 and len(got) == 2


def test_hashing_can_be_cancelled_between_blocks(tmp_path):
    big = hfile(tmp_path, "big", b"b" * 50000)

    def check():
        raise KeyboardInterrupt("stop")
    with pytest.raises(KeyboardInterrupt):
        a.hash_group(settings(hash_chunk=1000, check=check), [big, hfile(tmp_path, "big2", b"b" * 50000)], known=0)


@needs_ffmpeg
def test_a_recommended_name_windows_cannot_use_is_a_naming_reason_for_a_person(tmp_path):
    reserved = tmp_path / "nul.mkv"
    reserved.write_bytes(os.urandom(300))
    rec = analyse(reserved)
    assert rec["recommended_name"] == "Nul.mkv" or rec["recommended_name"] == "nul.mkv"
    assert any(c == "naming" and "reserved Windows name" in t for c, t in rec["findings"]["reasons"])


def test_the_folder_a_share_is_mounted_at_never_becomes_a_show_name(tmp_path):
    mount = tmp_path / "Breaking Bad"                                    # a mount point whose folder name looks like a show
    mount.mkdir()
    (mount / "S01E01.mkv").write_bytes(os.urandom(300))
    rec = analyse(mount / "S01E01.mkv", rel="S01E01.mkv")
    assert rec["media_type"] == "TV Episode" and rec["show_title"] == "Unknown Show" and rec["naming_confidence"] == "Low"
    nested = mount / "The Wire" / "Season 2"
    nested.mkdir(parents=True)
    (nested / "S02E03.mkv").write_bytes(os.urandom(300))
    assert analyse(nested / "S02E03.mkv", rel="The Wire/Season 2/S02E03.mkv")["show_title"] == "The Wire"
