"""A real agent scanning real media on the shares, reporting to a real SyncHub on localhost."""
import json
import os
import shutil
import signal
import time

import pytest

from synchub import jobs
from synchub.library import recommend as rc, scan, store
from synchub.library import vocab as v

from .agent_media import HAVE_FFMPEG, make_media
from .test_agent_live import rig, wait                         # noqa: F401  (rig is a fixture)

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
CFG = v.LibraryConfig()


def records(conn_or_rig, root=None):
    with conn_or_rig.db.connection() as conn:
        rows = conn.execute("SELECT * FROM library_file" + (" WHERE root_name = ?" if root else "") + " ORDER BY relative_path", (root,) if root else ()).fetchall()
        return {r["relative_path"]: dict(r) for r in rows}


def start(rig, **kw):
    with rig.db.connection() as conn:
        return scan.start_scan(conn, kw.pop("root", "tfiles"), **kw)


def run_state(rig, run_id):
    with rig.db.connection() as conn:
        return scan.describe_run(conn, conn.execute("SELECT * FROM library_run WHERE id = ?", (run_id,)).fetchone())


def wait_run(rig, run_id, *, status=("completed", "completed_with_errors"), timeout=90):
    return wait(lambda: (r := run_state(rig, run_id))["status"] in status and r, timeout=timeout, what=f"the run to be {status}")


def library(rig):
    """A small library on the TFiles share: a movie, a TV episode in the right folders, a multi-language movie, an old AVI, and noise."""
    t = rig.roots["tfiles"]
    make_media(t / "Movies" / "Heat.1995.1080p.BluRay.x264-GRP.mkv")
    make_media(t / "TV Shows" / "Show Name" / "Season 1" / "Show.Name.S01E02.720p.HDTV.mkv")
    make_media(t / "Movies" / "Multi 2001.mkv", audio=(("eng", "aac"), ("ita", "aac")), subtitles=("eng", "fra"))
    make_media(t / "Old" / "Old Movie 1999.avi", video="libx264", audio=(("eng", "mp3"),))
    (t / "Movies" / "notes.txt").write_text("not a video")
    make_media(t / "@eaDir" / "thumb.mkv")


def test_a_scan_analyses_every_video_file_leaves_out_the_noise_and_fills_the_queues(rig):
    library(rig)
    rig.start()
    run = wait_run(rig, start(rig))
    found = records(rig)
    assert sorted(found) == ["Movies/Heat.1995.1080p.BluRay.x264-GRP.mkv", "Movies/Multi 2001.mkv", "Old/Old Movie 1999.avi", "TV Shows/Show Name/Season 1/Show.Name.S01E02.720p.HDTV.mkv"]
    assert run["counts"]["found"] == 4 and run["counts"]["analyzed"] == 4 and run["counts"]["inserted"] == 4 and run["status"] == "completed" and run["progress"] == 1.0
    heat = found["Movies/Heat.1995.1080p.BluRay.x264-GRP.mkv"]
    assert (heat["recommended_name"], heat["media_type"], heat["recommended_action"], heat["analysis_status"], heat["source_dir"]) == ("Heat (1995).mkv", "Movie/Unknown", "copy_rename", "completed", "tfiles:")
    assert heat["source_path"] == os.path.join(str(rig.roots["tfiles"]), "Movies/Heat.1995.1080p.BluRay.x264-GRP.mkv") and heat["analysis_logic_version"] == v.ANALYSIS_LOGIC_VERSION and heat["analysis_run_id"] == run["id"]
    episode = found["TV Shows/Show Name/Season 1/Show.Name.S01E02.720p.HDTV.mkv"]
    assert (episode["media_type"], episode["show_title"], episode["season_number"], episode["recommended_name"]) == ("TV Episode", "Show Name", 1, "Show Name - S01E02.mkv")
    multi = found["Movies/Multi 2001.mkv"]
    assert json.loads(multi["proposed_track_changes_json"])[0]["op"] == "keep_tracks" and multi["english_audio_status"] == "present"
    avi = found["Old/Old Movie 1999.avi"]
    assert avi["recommended_action"] == "remux_copy" and avi["recommended_name"] == "Old Movie (1999).mkv"
    with rig.db.connection() as conn:
        groups = {rec["relative_path"]: r.group for rec, r in rc.classify(conn, CFG)}
        csv_rows = store.get_record(conn, heat["id"])
    assert groups["Movies/Heat.1995.1080p.BluRay.x264-GRP.mkv"] == "ready" and groups["Movies/Multi 2001.mkv"] == "suggested" and groups["Old/Old Movie 1999.avi"] == "convert"
    assert csv_rows["file_name"].startswith("Heat.1995")
    log = rig.joblog(run["job_ids"][0])
    assert any("Skipping a folder that is left out" in line["msg"] and line["folder"] == "@eaDir" for line in log) and any(line["msg"] == "Found 4 video file(s)" for line in log)


def test_a_second_scan_looks_only_at_what_changed_and_notices_what_went_missing(rig):
    library(rig)
    rig.start()
    wait_run(rig, start(rig))
    first = records(rig)
    heat = first["Movies/Heat.1995.1080p.BluRay.x264-GRP.mkv"]
    with rig.db.connection() as conn:
        store.set_approval(conn, heat["id"], v.APPROVAL_APPROVED, "ann")
    t = rig.roots["tfiles"]
    (t / "Old" / "Old Movie 1999.avi").unlink()
    make_media(t / "Movies" / "Heat.1995.1080p.BluRay.x264-GRP.mkv", seconds=3)                      # the same name, a different file
    make_media(t / "Movies" / "Brand New 2020.mkv")
    run = wait_run(rig, start(rig))
    counts = run["counts"]
    assert (counts["found"], counts["need"], counts["unchanged"], counts["new"], counts["analyzed"], counts["missing"]) == (4, 2, 2, 1, 2, 1)
    after = records(rig)
    assert after["Old/Old Movie 1999.avi"]["missing_since"] and not after["Movies/Multi 2001.mkv"]["missing_since"] and "Movies/Brand New 2020.mkv" in after
    changed = after["Movies/Heat.1995.1080p.BluRay.x264-GRP.mkv"]
    assert changed["source_revision"] == 2 and changed["approval_status"] == "pending" and changed["size_bytes"] != heat["size_bytes"]          # an approval does not outlive its file
    assert after["Movies/Multi 2001.mkv"]["analyzed_at"] == first["Movies/Multi 2001.mkv"]["analyzed_at"]                                     # an unchanged file was not analysed again
    assert any(line["msg"] == "2 of 4 file(s) need analysing" for line in rig.joblog(run["job_ids"][0]))


def test_a_scan_of_one_folder_stays_in_it_and_only_marks_missing_there(rig):
    library(rig)
    rig.start()
    wait_run(rig, start(rig))
    (rig.roots["tfiles"] / "Old" / "Old Movie 1999.avi").unlink()
    (rig.roots["tfiles"] / "Movies" / "Multi 2001.mkv").unlink()
    run = wait_run(rig, start(rig, prefix="Movies"))
    found = records(rig)
    assert run["counts"]["found"] == 1 and found["Movies/Multi 2001.mkv"]["missing_since"] and not found["Old/Old Movie 1999.avi"]["missing_since"]


def test_a_forced_scan_analyses_everything_again(rig):
    library(rig)
    rig.start()
    wait_run(rig, start(rig))
    run = wait_run(rig, start(rig, force=True))
    assert run["counts"]["need"] == 4 and run["counts"]["analyzed"] == 4 and run["counts"]["changed"] == 0


def test_a_folder_that_does_not_exist_is_a_clear_failure_not_an_empty_scan(rig):
    rig.start()
    run = wait_run(rig, start(rig, prefix="Nope"), status=("failed",))
    assert "does not exist in the share tfiles" in run["error"]
    assert rig.job(run["job_ids"][0])["error_class"] == "bad_job" and rig.job(run["job_ids"][0])["attempts"] == 1


def test_a_scan_the_agent_does_not_understand_fails_instead_of_mixing_logic_versions(rig):
    rig.start()
    run_id = start(rig)
    with rig.db.connection() as conn:
        (job,) = scan.run_jobs(conn, run_id)
        conn.execute("UPDATE job SET plan = ? WHERE id = ?", (json.dumps({**job["plan"], "logic_version": 999}), job["id"]))
    run = wait_run(rig, run_id, status=("failed",))
    assert "differs from SyncHub's" in run["error"] and rig.job(run["job_ids"][0])["error_class"] == "unsupported_job"


def test_an_agent_without_the_analysis_module_does_not_offer_to_scan(rig, monkeypatch):
    from .agent_module import agent
    monkeypatch.setattr(agent, "media_analysis", None)
    instance = rig.start()
    run_id = start(rig)
    wait(lambda: instance.registered, what="the agent to register")
    time.sleep(1.0)
    with rig.db.connection() as conn:
        from synchub import workers
        assert "scan" not in workers.get(conn, rig.worker_id)["capabilities"]["ops"]
    assert run_state(rig, run_id)["status"] == "queued"


# ---------------------------------------------------------------- duplicates across both shares
def test_a_duplicate_search_finds_the_same_file_on_both_shares_and_keeps_the_one_in_the_library(rig):
    media, tfiles = rig.roots["media"], rig.roots["tfiles"]
    placed = make_media(media / "Movies" / "Heat (1995)" / "Heat (1995).mkv", seconds=3)
    for where in (tfiles / "Downloads", tfiles / "Old"):
        where.mkdir(parents=True, exist_ok=True)
        shutil.copy2(placed, where / "Heat.1995.1080p.mkv")
    big = os.urandom(3 * 1024 * 1024)
    (tfiles / "Downloads" / "Other One 2001.mkv").write_bytes(big)                                       # same size, different content: ruled out by the partial read
    (tfiles / "Downloads" / "Other Two 2002.mkv").write_bytes(big[:-1] + bytes([big[-1] ^ 1]))
    rig.start()
    wait_run(rig, start(rig, root="media"))
    wait_run(rig, start(rig, root="tfiles"))
    before = records(rig)
    assert all(r["duplicate_status"] == v.DUP_NOT_CHECKED for r in before.values()) and len(before) == 5
    with rig.db.connection() as conn:
        run_id = scan.start_duplicate_search(conn, "ann")
    run = wait_run(rig, run_id)
    found = records(rig)
    group = "Movies/Heat (1995)/Heat (1995).mkv"
    kept, copies = found[group], [found["Downloads/Heat.1995.1080p.mkv"], found["Old/Heat.1995.1080p.mkv"]]
    assert kept["duplicate_status"] == v.DUP_PRIMARY and kept["root_name"] == "media" and kept["sha256"]
    assert [c["duplicate_status"] for c in copies] == [v.DUP_OTHER, v.DUP_OTHER] and {c["duplicate_group"] for c in copies} == {kept["duplicate_group"]}
    assert found["Downloads/Other One 2001.mkv"]["duplicate_status"] == v.DUP_RULED_OUT and found["Downloads/Other Two 2002.mkv"]["duplicate_status"] == v.DUP_RULED_OUT
    assert run["counts"]["groups"] == 1 and run["counts"]["hashed"] == 3 and run["counts"]["ruled_out"] == 2
    with rig.db.connection() as conn:
        by_path = {rec["relative_path"]: r for rec, r in rc.classify(conn, CFG)}
    assert by_path[group].kind == "in_place" and by_path["Downloads/Heat.1995.1080p.mkv"].kind == "skip_duplicate" and by_path["Old/Heat.1995.1080p.mkv"].headline == "Skip this duplicate"
    with rig.db.connection() as conn:
        assert scan.start_duplicate_search(conn, "ann") and scan.list_runs(conn)[0]["status"] == "completed"           # nothing left to read: finished at once


# ---------------------------------------------------------------- interruptions
def test_a_killed_agent_resumes_a_scan_and_only_looks_at_the_rest(rig):
    t = rig.roots["tfiles"]
    template = make_media(t / "template.mkv")
    for i in range(24):
        shutil.copy2(template, t / "Movies" / f"Film {1990 + i}.mkv") if (t / "Movies").exists() else (os.makedirs(t / "Movies"), shutil.copy2(template, t / "Movies" / f"Film {1990 + i}.mkv"))
    template.unlink()
    run_id = start(rig)
    first = rig.spawn(SYNCHUB_SCAN_THREADS=1, SYNCHUB_SCAN_BATCH=5)
    wait(lambda: len(records(rig)) >= 5, what="the first records to arrive")
    first.send_signal(signal.SIGKILL)
    first.wait(10)
    stored = len(records(rig))
    assert 5 <= stored < 24 and run_state(rig, run_id)["status"] == "running"
    rig.spawn(SYNCHUB_SCAN_THREADS=1, SYNCHUB_SCAN_BATCH=5)
    run = wait_run(rig, run_id, timeout=120)
    assert len(records(rig)) == 24 and run["counts"]["found"] >= 24
    log = rig.joblog(run["job_ids"][0])
    need = [line for line in log if "need analysing" in line["msg"]]
    assert need and int(need[-1]["msg"].split(" ")[0]) <= 24 - stored + 5                                    # the second run looked only at (about) the rest
    assert rig.job(run["job_ids"][0])["attempts"] == 1                                                      # the same attempt carried on


def test_cancelling_a_scan_stops_it_and_keeps_what_was_reported(rig):
    t = rig.roots["tfiles"]
    (t / "Movies").mkdir()
    template = make_media(t / "template.mkv")
    for i in range(30):
        shutil.copy2(template, t / "Movies" / f"Film {1990 + i}.mkv")
    template.unlink()
    run_id = start(rig)
    rig.start(SYNCHUB_SCAN_THREADS=1, SYNCHUB_SCAN_BATCH=3)
    wait(lambda: len(records(rig)) >= 3, what="the first records")
    job_id = run_state(rig, run_id)["job_ids"][0]
    with rig.db.connection() as conn:
        jobs.cancel(conn, job_id, "ann")
    wait_run(rig, run_id, status=("cancelled",))
    kept = len(records(rig))
    time.sleep(1.5)
    assert 3 <= kept < 30 and len(records(rig)) == kept                                                      # it really stopped, and nothing reported was thrown away
    with rig.db.connection() as conn:
        assert all(r["missing_since"] is None for r in conn.execute("SELECT missing_since FROM library_file"))


def test_a_folder_that_could_not_be_listed_means_nothing_is_marked_missing(rig, monkeypatch):
    from .agent_module import agent
    library(rig)
    rig.start()
    wait_run(rig, start(rig))
    (rig.roots["tfiles"] / "Old" / "Old Movie 1999.avi").unlink()
    real = agent.media_analysis.walk_inventory

    def walk_with_a_problem(*args, **kw):
        files, unreadable, errors = real(*args, **kw)
        return files, unreadable, errors + ["[Errno 13] Permission denied: '/mnt/tfiles/Locked'"]
    monkeypatch.setattr(agent.media_analysis, "walk_inventory", walk_with_a_problem)
    run = wait_run(rig, start(rig))
    assert run["status"] == "completed_with_errors" and run["counts"]["walk_errors"] == 1 and "Permission denied" in run["counts"]["last_walk_error"]
    assert not records(rig)["Old/Old Movie 1999.avi"]["missing_since"]                       # it may simply be in the folder nobody could read
    assert any(line["msg"].startswith("Some folders could not be listed") for line in rig.joblog(run["job_ids"][0]))


def test_a_report_that_is_being_retried_gives_up_at_once_when_the_job_is_cancelled():
    from .agent_module import agent

    class Down:
        def post(self, path, body=None):
            raise agent.Unreachable("no route to host")

    class Quiet:
        def warning(self, *a, **k):
            pass

    class Reporter:
        def __init__(self):
            self.calls = 0

        def check(self):
            self.calls += 1
            if self.calls > 3:
                raise agent.Cancelled()

    fake = type("Fake", (), {"api": Down(), "job": {"id": 7}, "log": Quiet(), "stage": "scan", "reporter": Reporter()})()
    started = time.monotonic()
    with pytest.raises(agent.Cancelled):
        agent.JobRunner.tell(fake, "/scan/records", {}, patience=300)
    assert time.monotonic() - started < 10                                                    # it did not sit out its five minutes of patience


def test_a_report_synchub_refuses_is_a_clear_failure_and_one_for_a_job_taken_away_stops_the_work():
    from .agent_module import agent

    def runner(status, body):
        class Api:
            def post(self, path, payload=None):
                raise agent.ApiError(status, "no", body)
        quiet = type("Quiet", (), {"warning": lambda *a, **k: None})()
        reporter = type("R", (), {"check": lambda self: None})()
        return type("Fake", (), {"api": Api(), "job": {"id": 7}, "log": quiet, "stage": "scan", "reporter": reporter})()
    with pytest.raises(agent.JobFailure) as refused:
        agent.JobRunner.tell(runner(400, {}), "/scan/records", {})
    assert refused.value.error_class == "bad_job" and refused.value.retryable is False
    with pytest.raises(agent.JobLost):
        agent.JobRunner.tell(runner(409, {"stop": True}), "/scan/records", {})
