import errno
import json
import logging
import os
import shutil
import threading
import time

import pytest

from .agent_module import agent


def config(tmp_path, **env):
    base = {"SYNCHUB_URL": "http://127.0.0.1:5001", "SYNCHUB_TOKEN": "sw_" + "a" * 48, "SYNCHUB_WORK": str(tmp_path / "work")}
    return agent.Config(None, env={**base, **env}).validate()


def logger(cfg, name="synchub_agent"):
    return agent.build_logger(cfg, console=False)


# ---------------------------------------------------------------- paths inside a share
def test_safe_path_stays_inside_the_share(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    assert agent.safe_path(str(root), "Movies/Heat (1995)/Heat.mkv") == os.path.join(os.path.realpath(root), "Movies", "Heat (1995)", "Heat.mkv")
    for bad in ("", "/etc/passwd", "../x", "a/../../x", "a//b", "./a", "a/./b", "a\x00b", agent.MARKER, "a/.."):
        with pytest.raises(ValueError):
            agent.safe_path(str(root), bad)


def test_safe_path_refuses_a_symlink_that_leads_out(tmp_path):
    root, outside = tmp_path / "media", tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    os.symlink(outside, root / "escape")
    with pytest.raises(ValueError, match="leads out"):
        agent.safe_path(str(root), "escape/new/file.mkv")
    os.symlink(root / "real", root / "inside")
    (root / "real").mkdir()
    assert agent.safe_path(str(root), "inside/file.mkv").endswith("inside/file.mkv")              # a symlink that stays inside the share is fine


# ---------------------------------------------------------------- manifests
def test_a_manifest_write_is_all_or_nothing(tmp_path, monkeypatch):
    path = str(tmp_path / "manifest.json")
    agent.atomic_write_json(path, {"v": 1})
    real_replace = os.replace

    def crash(src, dst):
        raise OSError("power cut")
    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        agent.atomic_write_json(path, {"v": 2})
    monkeypatch.setattr(os, "replace", real_replace)
    assert json.load(open(path)) == {"v": 1}                                # the old complete file survives; never half of the new one


def test_a_returning_job_finds_its_folder_and_an_unreadable_manifest_starts_fresh(tmp_path):
    cfg = config(tmp_path)
    store = agent.JobStore(cfg, logger(cfg))
    job = {"id": 7, "kind": "library", "plan": {"op": "copy"}, "source": {"root": "media", "rel": "a.mkv"}, "dest": {"root": "tfiles", "rel": "a.mkv"}}
    folder, manifest = store.open(job, 1)
    manifest["stages"]["copy_in"] = {"done": True, "checkpoint": 123}
    store.save(folder, manifest)
    again_folder, again = store.open(job, 2)
    assert again_folder == folder and again["stages"]["copy_in"]["checkpoint"] == 123 and [a["attempt"] for a in again["attempts"]] == [1, 2]
    assert store.holding() == [7]
    with open(os.path.join(folder, "manifest.json"), "w") as handle:
        handle.write("{ not json")
    _, fresh = store.open(job, 3)
    assert fresh["stages"] == {}


def test_a_different_job_with_the_same_id_gets_its_own_folder(tmp_path):
    cfg = config(tmp_path)
    store = agent.JobStore(cfg, logger(cfg))
    one = {"id": 1, "kind": "library", "plan": {"op": "copy"}, "source": {"root": "m", "rel": "a"}, "dest": {"root": "t", "rel": "a"}}
    other = {**one, "source": {"root": "m", "rel": "DIFFERENT"}}                # a reset SyncHub database hands out id 1 again
    assert store.folder(one) != store.folder(other)


def test_retention_removes_only_what_has_expired(tmp_path):
    cfg = config(tmp_path)
    store = agent.JobStore(cfg, logger(cfg))
    made = {}
    for job_id, until in ((1, "2000-01-01T00:00:00Z"), (2, "2999-01-01T00:00:00Z"), (3, None)):
        folder, manifest = store.open({"id": job_id, "kind": "library", "plan": {}, "source": None, "dest": None}, 1)
        manifest["keep_until"] = until
        store.save(folder, manifest)
        made[job_id] = folder
    assert store.sweep(active_ids={1}) == 0                                       # a running job is never swept
    assert store.sweep(active_ids=set()) == 1
    assert not os.path.exists(made[1]) and os.path.exists(made[2]) and os.path.exists(made[3])


def test_removing_a_job_also_removes_its_half_written_file_on_the_share(tmp_path):
    cfg = config(tmp_path)
    store = agent.JobStore(cfg, logger(cfg))
    folder, manifest = store.open({"id": 4, "kind": "library", "plan": {}, "source": None, "dest": None}, 1)
    partial, real = tmp_path / "movie.mkv.synchub-partial", tmp_path / "keep.mkv"
    partial.write_bytes(b"half")
    real.write_bytes(b"precious")
    manifest["dest_partial"] = str(partial)
    store.save(folder, manifest)
    store.remove(folder)
    assert not partial.exists() and not os.path.exists(folder)
    folder, manifest = store.open({"id": 5, "kind": "library", "plan": {}, "source": None, "dest": None}, 1)
    manifest["dest_partial"] = str(real)                                          # a path that is not a partial file is never touched
    store.save(folder, manifest)
    store.remove(folder)
    assert real.exists()


# ---------------------------------------------------------------- shares
def marked(path, text="media"):
    path.mkdir(exist_ok=True)
    (path / agent.MARKER).write_text(text + "\n")
    return str(path)


def test_a_share_is_ready_only_when_mounted_marked_and_the_right_one(tmp_path):
    ok = agent.check_root(marked(tmp_path / "media"), "media", write_test=True)
    assert ok["ok"] and ok["state"] == "ok" and ok["free_bytes"] > 0
    assert agent.check_root(str(tmp_path / "missing"), "media")["state"] == "not_mounted"
    empty = tmp_path / "empty"
    empty.mkdir()
    unmarked = agent.check_root(str(empty), "media")
    assert unmarked["state"] == "no_marker" and not unmarked["ok"] and "--init-roots" in unmarked["message"]
    wrong = agent.check_root(marked(tmp_path / "other", "tfiles"), "media")
    assert wrong["state"] == "wrong_marker" and "tfiles" in wrong["message"] and "wrong share" in wrong["message"]
    a_file = tmp_path / "file"
    a_file.write_text("x")
    assert agent.check_root(str(a_file), "media")["state"] == "not_mounted"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_read_only_share_is_reported(tmp_path):
    path = marked(tmp_path / "media")
    os.chmod(path, 0o555)
    try:
        assert agent.check_root(path, "media", write_test=True)["state"] == "read_only"
        assert agent.check_root(path, "media")["state"] == "read_only"
    finally:
        os.chmod(path, 0o755)


def test_a_hung_mount_is_reported_instead_of_freezing_the_agent(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(agent, "check_root", lambda *a, **k: release.wait(30) and {})
    monkeypatch.setattr(agent.RootMonitor, "HANG_SECONDS", 0.2)
    roots = agent.Roots()
    roots.update([{"name": "media", "mount_point": str(tmp_path / "media"), "marker_text": "media"}])
    monitor = agent.RootMonitor(roots, logging.getLogger("test"))
    started = time.monotonic()
    monitor.check_all()
    monitor.check_all()                                                             # the second pass does not pile up another stuck thread
    assert time.monotonic() - started < 2
    result = monitor.snapshot()["media"]
    assert not result["ok"] and ("did not answer" in result["message"] or "not responding" in result["message"])
    assert sum(1 for t in threading.enumerate() if t.name == "check-media") == 1
    release.set()


def test_require_waits_for_the_first_check_and_refuses_an_unready_share(tmp_path):
    roots = agent.Roots()
    roots.update([{"name": "media", "mount_point": str(tmp_path / "nope"), "marker_text": "media"}])
    monitor = agent.RootMonitor(roots, logging.getLogger("test"))
    with pytest.raises(agent.JobFailure, match="has not been checked yet") as caught:
        monitor.require("media", wait=0.3)
    assert caught.value.error_class == "transient"
    monitor.check_all()
    with pytest.raises(agent.JobFailure, match="does not exist"):
        monitor.require("media")
    roots.overrides["media"] = marked(tmp_path / "real")
    monitor.check_all()
    monitor.require("media")                                                        # ready: no exception


def test_this_pcs_own_mount_point_beats_the_one_synchub_suggests(tmp_path):
    roots = agent.Roots({"media": "/somewhere/else"})
    roots.update([{"name": "media", "mount_point": "/mnt/tower/Media", "marker_text": "media"}, {"name": "tfiles", "mount_point": "/mnt/tower/TFiles"}])
    assert roots.path("media") == "/somewhere/else" and roots.path("tfiles") == "/mnt/tower/TFiles" and roots.path("nope") is None
    assert roots.text("tfiles") == "tfiles"


# ---------------------------------------------------------------- settings
def test_settings_come_from_the_file_and_the_environment_wins(tmp_path):
    ini = tmp_path / "a.ini"
    ini.write_text("[synchub]\nurl = http://tower:5001/\ntoken = sw_file\nname = enc\n[worker]\nslots = 2\nwork_dir = /var/x\nmax_copy_mb_s = 40\n[roots]\nMedia = /mnt/m\n")
    cfg = agent.Config(str(ini), env={"SYNCHUB_TOKEN": "sw_env", "SYNCHUB_ROOT_TFILES": "/mnt/t"}).validate()
    assert (cfg.url, cfg.token, cfg.name, cfg.slots, cfg.work_dir, cfg.max_copy_mb_s) == ("http://tower:5001", "sw_env", "enc", 2, "/var/x", 40.0)
    assert cfg.root_overrides == {"media": "/mnt/m", "tfiles": "/mnt/t"} and cfg.use_proxy is False and cfg.verify == "hash"


@pytest.mark.parametrize("env, fragment", [
    ({"SYNCHUB_URL": ""}, "address"), ({"SYNCHUB_URL": "tower:5001"}, "address"), ({"SYNCHUB_TOKEN": ""}, "token"),
    ({"SYNCHUB_SLOTS": "many"}, "whole number"), ({"SYNCHUB_SLOTS": "99"}, "between"), ({"SYNCHUB_VERIFY": "maybe"}, "verify"),
    ({"SYNCHUB_LOG_LEVEL": "LOUD"}, "log_level"), ({"SYNCHUB_MAX_COPY_MB_S": "fast"}, "number"),
])
def test_a_bad_setting_says_what_to_change(tmp_path, env, fragment):
    base = {"SYNCHUB_URL": "http://x:1", "SYNCHUB_TOKEN": "sw_x", "SYNCHUB_WORK": str(tmp_path)}
    with pytest.raises(agent.ConfigError) as caught:
        agent.Config(None, env={**base, **env}).validate()
    assert fragment in str(caught.value)


def test_a_missing_settings_file_is_a_clear_error(tmp_path):
    with pytest.raises(agent.ConfigError, match="does not exist"):
        agent.Config(str(tmp_path / "nope.ini"), env={})


def test_the_token_is_never_printed_whole(tmp_path):
    assert "a" * 20 not in config(tmp_path).masked_token()


# ---------------------------------------------------------------- logs
def test_logs_are_json_lines_with_the_job_and_rotate(tmp_path):
    cfg = config(tmp_path, SYNCHUB_LOG_MAX_MB="1", SYNCHUB_LOG_BACKUPS="2")
    log = logger(cfg)
    folder = tmp_path / "job"
    folder.mkdir()
    joblog = agent.JobLog(str(folder), 9, log)
    joblog.info("Copied", bytes=1234, stage="copy_in")
    joblog.error("It broke", error_class="transient")
    for handler in log.handlers:
        handler.flush()
    lines = [json.loads(line) for line in open(folder / "job.log")]
    assert [(l["level"], l["msg"], l["job"]) for l in lines] == [("INFO", "Copied", 9), ("ERROR", "It broke", 9)] and lines[0]["bytes"] == 1234 and lines[0]["ts"].endswith("Z")
    worker = [json.loads(line) for line in open(tmp_path / "work" / "logs" / "worker.jsonl")]
    assert any(l["msg"] == "Copied" and l["job"] == 9 for l in worker)               # the same events are in the worker log too
    big = "x" * 100_000
    for _ in range(40):
        log.info(big)
    for handler in log.handlers:
        handler.flush()
    names = sorted(os.listdir(tmp_path / "work" / "logs"))
    assert names == ["worker.jsonl", "worker.jsonl.1", "worker.jsonl.2"]              # rotated, and no more than asked for


def test_a_jobs_log_rotates_and_hands_over_its_tail_and_everything(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    joblog = agent.JobLog(str(tmp_path), 1, logger(cfg))
    monkeypatch.setattr(agent.JobLog, "MAX_BYTES", 2000)
    for i in range(100):
        joblog.info(f"line {i}")
    assert os.path.exists(tmp_path / "job.log.1")
    assert "line 99" in joblog.tail(3) and "line 97" in joblog.tail(3) and "line 96" not in joblog.tail(3)
    text, truncated = joblog.full()
    assert "line 99" in text and not truncated
    text, truncated = joblog.full(limit=500)
    assert truncated and len(text) == 500 and text.rstrip().endswith('"job": 1}')


def test_a_log_that_cannot_be_written_never_stops_a_job(tmp_path):
    cfg = config(tmp_path)
    joblog = agent.JobLog(str(tmp_path / "gone" / "deeper"), 1, logger(cfg))
    joblog.info("still fine")                                                      # the folder does not exist: no exception


# ---------------------------------------------------------------- what went wrong, in words
@pytest.mark.parametrize("exc, error_class, retryable, fragment", [
    (FileNotFoundError(2, "No such file", "/mnt/x/a.mkv"), "source_changed", None, "/mnt/x/a.mkv"),
    (PermissionError(13, "Permission denied", "/mnt/x"), "permission_denied", False, "Permission denied"),
    (OSError(errno.ENOSPC, "No space left on device", "/work"), "transient", None, "disk is full"),
    (OSError(errno.ESTALE, "Stale file handle", "/mnt/x"), "transient", None, "Stale file handle"),
    (OSError(errno.EIO, "Input/output error", "/mnt/x"), "transient", None, "Input/output"),
    (ValueError("surprise"), "agent_error", True, "ValueError: surprise"),
])
def test_exceptions_become_failures_with_a_class_and_a_sentence(exc, error_class, retryable, fragment):
    failure = agent.classify(exc)
    assert (failure.error_class, failure.retryable) == (error_class, retryable) and fragment in failure.message


def test_a_failure_the_job_raised_itself_is_passed_through():
    mine = agent.JobFailure("dest_exists", "no", retryable=False)
    assert agent.classify(mine) is mine


# ---------------------------------------------------------------- small helpers
def test_sizes_and_durations_read_naturally():
    assert [agent.human_bytes(n) for n in (0, 900, 1536, 5 * 1024 ** 3, None)] == ["0 B", "900 B", "1.5 KB", "5.0 GB", "?"]
    assert [agent.human_duration(s) for s in (5, 59, 61, 3700)] == ["5 s", "59 s", "1 min", "1 h 1 min"]


def test_hashing_can_be_interrupted_between_blocks(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(os.urandom(3_000_000))
    import hashlib
    assert agent.hash_file(str(path)) == hashlib.sha256(path.read_bytes()).hexdigest()
    assert agent.hash_file(str(path), upto=1000) == hashlib.sha256(path.read_bytes()[:1000]).hexdigest()

    def stop(_n):
        raise agent.Interrupted()
    with pytest.raises(agent.Interrupted):
        agent.hash_file(str(path), check=stop)


# ---------------------------------------------------------------- the heartbeat
class FakeApi:
    def __init__(self, delay=0.0):
        self.posts, self.delay, self.behaviour = [], delay, None

    def post(self, path, body=None):
        time.sleep(self.delay)
        self.posts.append((path, body))
        return self.behaviour(path, body) if self.behaviour else {"ok": True}


class Quiet:
    def __init__(self):
        self.warnings = []

    def warning(self, text, **_):
        self.warnings.append(text)


def reporter(api, monkeypatch, stop=None, interval=0.05, stall=120.0):
    monkeypatch.setattr(agent.Reporter, "INTERVAL", interval)
    monkeypatch.setattr(agent.Reporter, "STALL_SECONDS", stall)
    r = agent.Reporter(api, 5, Quiet(), stop or threading.Event())
    r.start()
    return r


def test_the_heartbeat_sends_the_latest_state_on_its_own(monkeypatch):
    api = FakeApi()
    r = reporter(api, monkeypatch)
    r.update("copy_in", 0.25, "Copying in", 60)
    time.sleep(0.3)
    r.update("copy_out", 0.5, "Copying out", 30)
    time.sleep(0.3)
    r.close()
    assert len(api.posts) >= 4 and all(path == "/jobs/5/progress" for path, _ in api.posts)
    assert api.posts[0][1]["stage"] == "copy_in" and api.posts[-1][1] == {"stage": "copy_out", "progress": 0.5, "message": "Copying out", "eta_seconds": 30}


def test_a_slow_synchub_never_slows_the_job(monkeypatch):
    api = FakeApi(delay=0.5)
    r = reporter(api, monkeypatch)
    started = time.monotonic()
    for i in range(1000):
        r.update("copy_in", i / 1000)
        r.check()
    assert time.monotonic() - started < 0.2
    r.close()


def test_a_job_that_stops_making_progress_stops_sending_heartbeats(monkeypatch):
    api = FakeApi()
    r = reporter(api, monkeypatch, stall=0.3)
    r.update("copy_in", 0.1)
    time.sleep(0.2)
    assert api.posts
    time.sleep(0.3)                                                                  # now stuck for longer than the limit, as on a hung mount
    seen = len(api.posts)
    time.sleep(0.4)
    assert len(api.posts) == seen                                                    # SyncHub is left to take the job back
    r.update("copy_in", 0.2)                                                         # it moves again: the heartbeat comes back
    time.sleep(0.3)
    assert len(api.posts) > seen
    r.close()


def test_the_heartbeat_learns_of_a_cancel_or_a_lost_job(monkeypatch):
    api = FakeApi()
    api.behaviour = lambda path, body: {"ok": True, "cancel": True}
    r = reporter(api, monkeypatch)
    r.update("copy_in", 0.1)
    time.sleep(0.2)
    with pytest.raises(agent.Cancelled):
        r.check()
    r.close()

    def taken(path, body):
        raise agent.ApiError(409, "That job is no longer yours", {"stop": True})
    api = FakeApi()
    api.behaviour = taken
    r = reporter(api, monkeypatch)
    r.update("copy_in", 0.1)
    time.sleep(0.2)
    with pytest.raises(agent.JobLost):
        r.check()
    r.close()


def test_a_shutdown_interrupts_the_job_at_the_next_check(monkeypatch):
    stop = threading.Event()
    r = reporter(FakeApi(), monkeypatch, stop=stop)
    r.check()
    stop.set()
    with pytest.raises(agent.Interrupted):
        r.check()
    r.close()


def test_an_outage_is_survived_and_reporting_resumes(monkeypatch):
    api = FakeApi()
    state = {"down": True}

    def flaky(path, body):
        if state["down"]:
            raise agent.Unreachable("no route to host")
        return {"ok": True}
    api.behaviour = flaky
    r = reporter(api, monkeypatch)
    r.update("copy_in", 0.1)
    time.sleep(0.6)
    r.check()                                                                        # an outage is not a reason to stop the job
    assert len(api.posts) <= 3                                                       # and a down SyncHub is retried gently, not every interval
    state["down"] = False
    r.update("copy_in", 0.2)
    deadline = time.monotonic() + 8
    ok = []
    while time.monotonic() < deadline and not ok:
        time.sleep(0.1)
        ok = [b for _, b in api.posts[-3:] if b["progress"] == 0.2]
    r.close()
    assert ok
    assert r.log.warnings and "Cannot reach SyncHub" in r.log.warnings[0]


# ---------------------------------------------------------------- capabilities and the command line
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_capabilities_list_only_encoders_that_really_work(tmp_path):
    caps = agent.detect_capabilities(config(tmp_path))
    assert caps["ops"] == ["copy", "hash", "scan"] and caps["ffmpeg"].startswith("ffmpeg version") and caps["ffprobe"].startswith("ffprobe version") and caps["cores"] >= 1
    assert set(caps["encoders"]) <= set(agent.TESTED_ENCODERS)
    assert "libx264" in caps["encoders"] or "libx265" in caps["encoders"]
    assert not any(e.endswith(("_nvenc", "_qsv", "_vaapi")) for e in caps["encoders"])           # no GPU here: those encoders exist in ffmpeg but do not work, and are not offered


def test_capabilities_without_ffmpeg_still_work_for_copy_jobs(tmp_path):
    caps = agent.detect_capabilities(config(tmp_path, SYNCHUB_FFMPEG="/nonexistent/ffmpeg", SYNCHUB_FFPROBE="/nonexistent/ffprobe"))
    assert caps["ffmpeg"] is None and caps["ffprobe"] is None and caps["encoders"] == [] and caps["ops"] == ["copy", "hash"]       # no ffprobe: no scans


def test_the_command_line_reports_problems_with_a_distinct_exit_code(tmp_path, capsys, monkeypatch):
    for key in ("SYNCHUB_URL", "SYNCHUB_TOKEN", "SYNCHUB_WORK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    assert agent.main(["--version"]) == 0 and capsys.readouterr().out.strip() == agent.__version__
    assert agent.main([]) == 2 and "address" in capsys.readouterr().err
    assert agent.main(["--config", str(tmp_path / "missing.ini")]) == 2 and "does not exist" in capsys.readouterr().err
    monkeypatch.setenv("SYNCHUB_URL", "http://127.0.0.1:9")
    assert agent.main(["--init-roots"]) == 2 and "token is missing" in capsys.readouterr().err


def test_the_health_check_follows_the_main_loop(tmp_path):
    cfg = config(tmp_path)
    assert agent.command_healthcheck(cfg) == 1                                       # not started yet
    os.makedirs(cfg.work_dir)
    alive = os.path.join(cfg.work_dir, "alive")
    open(alive, "w").close()
    assert agent.command_healthcheck(cfg) == 0
    old = time.time() - 600
    os.utime(alive, (old, old))
    assert agent.command_healthcheck(cfg) == 1


def test_the_default_settings_files_are_only_looked_for_in_the_real_environment(tmp_path, monkeypatch):
    (tmp_path / "synchub-agent.ini").write_text("[synchub]\nurl = http://from-the-file:1\ntoken = sw_file\n")
    monkeypatch.chdir(tmp_path)
    assert agent.Config(None, env={}).url == ""                                      # tests and callers that pass their own environment are not surprised by a file
    monkeypatch.setattr(agent, "DEFAULT_CONFIG_FILES", ("synchub-agent.ini",))
    monkeypatch.delenv("SYNCHUB_URL", raising=False)
    assert agent.Config().url == "http://from-the-file:1"


# ---------------------------------------------------------------- the worker's own log
def write_log(dir_, name, lines):
    (dir_ / name).write_text("".join(f'{{"msg": "{line}"}}\n' for line in lines))


def test_the_log_tail_joins_the_rotated_files_oldest_first(tmp_path):
    write_log(tmp_path, "worker.jsonl.2", ["a1", "a2"])
    write_log(tmp_path, "worker.jsonl.1", ["b1", "b2"])
    write_log(tmp_path, "worker.jsonl", ["c1"])
    text, cut = agent.read_log_tail(str(tmp_path))
    assert [json.loads(line)["msg"] for line in text.splitlines()] == ["a1", "a2", "b1", "b2", "c1"] and cut is False


def test_a_huge_log_is_cut_on_a_line_boundary_and_says_so(tmp_path):
    write_log(tmp_path, "worker.jsonl.1", [f"old{i}" for i in range(500)])
    write_log(tmp_path, "worker.jsonl", [f"new{i}" for i in range(500)])
    text, cut = agent.read_log_tail(str(tmp_path), limit=4000)
    lines = text.splitlines()
    assert cut is True and len(text) <= 4000 and all(json.loads(line)["msg"] for line in lines)          # no half line at the start
    assert json.loads(lines[-1])["msg"] == "new499" and not any("old" in line for line in lines)


def test_a_missing_or_empty_log_is_an_empty_answer_not_an_error(tmp_path):
    assert agent.read_log_tail(str(tmp_path)) == ("", False)
    assert agent.read_log_tail(str(tmp_path / "nowhere")) == ("", False)


def test_the_agent_sends_its_log_and_survives_a_refusal(tmp_path):
    cfg = config(tmp_path)
    log = logger(cfg)
    log.info("something happened", job=7)
    api = FakeApi()
    instance = agent.Agent(cfg, log, api=api)
    instance.upload_worker_log()
    path, body = api.posts[0]
    assert path == "/log" and '"msg": "something happened"' in body["text"] and body["truncated"] is False

    def refuse(path, body):
        raise agent.ApiError(400, "no")
    api.behaviour = refuse
    instance.upload_worker_log()                                                    # a refusal is logged, not raised
    api.behaviour = lambda path, body: (_ for _ in ()).throw(agent.Unreachable("down"))
    instance.upload_worker_log()


# ---------------------------------------------------------------- the host (Worker Status)
CPUINFO = """processor\t: 0
model\t\t: 158
model name\t: Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz
cpu MHz\t\t: 800.000
physical id\t: 0
core id\t\t: 0
cpu cores\t: 6
flags\t\t: fpu sse4_2 avx avx2 aes fma vmx

processor\t: 1
model name\t: Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz
cpu MHz\t\t: 4300.000
physical id\t: 0
core id\t\t: 1
cpu cores\t: 6
flags\t\t: fpu sse4_2 avx avx2 aes fma vmx
"""


def test_the_cpu_is_read_from_proc_cpuinfo(monkeypatch):
    monkeypatch.setattr(agent, "read_file", lambda path, limit=0: CPUINFO if path == "/proc/cpuinfo" else "")
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(12)), raising=False)
    cpu = agent.cpu_details()
    assert cpu["model"] == "Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz" and cpu["threads"] == 12 and cpu["cores"] == 6 and cpu["mhz"] == 4300
    assert cpu["flags"] == ["sse4_2", "avx", "avx2", "fma", "aes"]                  # only the sets that matter for encoding, in a fixed order


def test_a_cpu_that_cannot_be_read_gives_blanks_not_an_error(monkeypatch):
    monkeypatch.setattr(agent, "read_file", lambda path, limit=0: "")
    monkeypatch.setattr(agent, "run_quiet", lambda command, timeout=20: (None, ""))
    cpu = agent.cpu_details()
    assert cpu["cores"] is None and cpu["flags"] == [] and cpu["threads"] >= 1


def test_memory_and_live_figures_come_from_proc(monkeypatch):
    files = {"/proc/meminfo": "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n", "/proc/uptime": "3600.55 7000.1\n",
             "/proc/stat": "cpu  100 0 100 700 100 0 0 0 0 0\n"}
    monkeypatch.setattr(agent, "read_file", lambda path, limit=0: files.get(path, ""))
    assert agent.memory_bytes() == 16384000 * 1024
    stats, sample = agent.live_host_stats(None)
    assert stats["mem_available_bytes"] == 8192000 * 1024 and stats["host_uptime_seconds"] == 3600 and "cpu_percent" not in stats and sample == (1000, 800)
    files["/proc/stat"] = "cpu  300 0 200 800 100 0 0 0 0 0\n"                      # 400 jiffies passed, 100 of them idle
    stats, _ = agent.live_host_stats(sample)
    assert stats["cpu_percent"] == 75.0 and len(stats["load"]) == 3


def test_graphics_cards_come_from_nvidia_smi_and_lspci_without_listing_an_nvidia_card_twice(monkeypatch):
    outputs = {"nvidia-smi": (0, "NVIDIA GeForce GTX 1660, 550.54, 6144\n"),
               "lspci": (0, '00:02.0 "VGA compatible controller" "Intel Corporation" "UHD Graphics 630" -r02 "ASUS" "x"\n'
                            '01:00.0 "VGA compatible controller" "NVIDIA Corporation" "TU116 [GeForce GTX 1660]" -ra1 "x" "y"\n'
                            '00:1f.3 "Audio device" "Intel Corporation" "Cannon Lake PCH cAVS" -r10 "x" "y"\n')}
    monkeypatch.setattr(agent, "run_quiet", lambda command, timeout=20: outputs.get(command[0], (None, "")))
    gpus = agent.gpu_details()
    assert [(g["name"], g["source"]) for g in gpus] == [("NVIDIA GeForce GTX 1660", "nvidia-smi"), ("Intel Corporation UHD Graphics 630", "lspci")]
    assert gpus[0]["driver"] == "550.54" and gpus[0]["memory_mb"] == 6144


def test_host_info_never_raises(monkeypatch):
    monkeypatch.setattr(agent, "cpu_details", lambda: 1 / 0)
    assert agent.host_info() == {}


def test_capabilities_carry_the_host_and_what_ffmpeg_has_built_in(tmp_path):
    caps = agent.detect_capabilities(config(tmp_path, SYNCHUB_FFMPEG="/nonexistent/ffmpeg", SYNCHUB_FFPROBE="/nonexistent/ffprobe"))
    assert caps["host"]["cpu"]["threads"] >= 1 and "ffmpeg" not in caps["host"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_the_encoders_built_into_ffmpeg_are_listed_apart_from_the_ones_that_work(tmp_path):
    caps = agent.detect_capabilities(config(tmp_path))
    ffmpeg = caps["host"]["ffmpeg"]
    assert set(caps["encoders"]) <= set(ffmpeg["encoders_built"]) and isinstance(ffmpeg["hwaccels"], list)


def test_an_arm_board_is_named_by_its_model_line(monkeypatch):
    monkeypatch.setattr(agent, "read_file", lambda path, limit=0: "processor\t: 0\nFeatures\t: fp asimd evtstrm aes crc32\nHardware\t: BCM2835\nModel\t\t: Raspberry Pi 4 Model B Rev 1.4\n" if path == "/proc/cpuinfo" else "")
    cpu = agent.cpu_details()
    assert cpu["model"] == "BCM2835" and cpu["flags"] == ["aes", "asimd"] and cpu["cores"] is None
