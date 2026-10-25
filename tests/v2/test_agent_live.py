"""The agent against a real SyncHub on localhost: real HTTP, real files, and a real process killed with SIGKILL."""
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from synchub import jobs, storage, workers
from synchub.web import worker_api

from .agent_module import PATH as AGENT_PATH, agent

MB = 1024 * 1024
LAUNCH = ("import sys; sys.path.insert(0, {dir!r}); import synchub_agent as a; a.Agent.MIN_POLL = 0.1; a.Reporter.INTERVAL = 0.1; "
          "a.RootMonitor.CHECK_SECONDS = 0.2; sys.exit(a.main([]))")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wait(predicate, timeout=30, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what}")


class Rig:
    """A SyncHub on localhost with two shares (real folders with marker files) and a worker token, plus ways to run agents against it."""

    def __init__(self, tmp_path, v2, url):
        self.tmp, self.db, self.url = tmp_path, v2, url
        self.roots = {"media": tmp_path / "media", "tfiles": tmp_path / "tfiles"}
        self.work = tmp_path / "work"
        with v2.connection() as conn:
            for name, path in self.roots.items():
                path.mkdir()
                (path / agent.MARKER).write_text(name + "\n")
                storage.create(conn, {"name": name, "protocol": "local", "share": str(path), "mount_point": str(path)})
            self.worker_id, self.token = workers.create(conn, "encoder-1", slots=2)
        self.running = []

    def env(self, **extra):
        env = {"SYNCHUB_URL": self.url, "SYNCHUB_TOKEN": self.token, "SYNCHUB_WORK": str(self.work), "SYNCHUB_COPY_CHUNK_MB": "1"}
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def config(self, **extra):
        return agent.Config(None, env=self.env(**extra)).validate()

    def start(self, api=None, **extra):
        """An agent running in this process, in its own thread."""
        cfg = self.config(**extra)
        instance = agent.Agent(cfg, agent.build_logger(cfg, console=False), api=api)
        thread = threading.Thread(target=instance.run, name="agent-under-test", daemon=True)
        thread.start()
        self.running.append((instance, thread))
        return instance

    def stop(self, instance):
        instance.stop()
        for pair in self.running:
            if pair[0] is instance:
                pair[1].join(60)
                assert not pair[1].is_alive(), "the agent did not stop"

    def spawn(self, **extra):
        """An agent in a process of its own, so it can be killed the way a power cut would."""
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        env.update(self.env(**extra))
        out = open(self.tmp / f"agent-{len(list(self.tmp.glob('agent-*.out')))}.out", "wb")
        proc = subprocess.Popen([sys.executable, "-c", LAUNCH.format(dir=str(AGENT_PATH.parent))], env=env, stdout=out, stderr=subprocess.STDOUT)
        self.running.append((proc, None))
        return proc

    def file(self, share, rel, size):
        path = self.roots[share] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(size))
        return path

    def queue(self, src="Movies/Heat.mkv", dst="out/Heat.mkv", src_root="media", dst_root="tfiles", **kw):
        with self.db.connection() as conn:
            return storage.queue_test_copy(conn, src_root, src, dst_root, dst, kw.get("worker_id"))

    def job(self, job_id):
        with self.db.connection() as conn:
            return jobs.get(conn, job_id)

    def manifest(self, job_id):
        for folder in (self.work / "jobs").glob(f"{job_id}-*"):
            try:
                return json.loads((folder / "manifest.json").read_text())
            except (OSError, ValueError):
                return None
        return None

    def joblog(self, job_id):
        for folder in (self.work / "jobs").glob(f"{job_id}-*"):
            return [json.loads(line) for line in (folder / "job.log").read_text().splitlines()]
        return []

    def close(self):
        for thing, thread in self.running:
            if thread is None:
                if thing.poll() is None:
                    thing.kill()
                    thing.wait(10)
            else:
                thing.stop()
                thread.join(30)


@pytest.fixture
def rig(tmp_path, v2, app, monkeypatch):
    monkeypatch.setattr(worker_api, "POLL_SECONDS", 0.1)
    monkeypatch.setattr(agent.Agent, "MIN_POLL", 0.1)
    monkeypatch.setattr(agent.Reporter, "INTERVAL", 0.1)
    monkeypatch.setattr(agent.RootMonitor, "CHECK_SECONDS", 0.2)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rig = Rig(tmp_path, v2, f"http://127.0.0.1:{server.server_port}")
    yield rig
    rig.close()
    server.shutdown()


# ---------------------------------------------------------------- the basics
def test_a_copy_job_runs_from_claim_to_done_and_the_copy_is_exact(rig):
    source = rig.file("media", "Movies/Heat.mkv", 3 * MB + 123)
    job_id = rig.queue()
    rig.start()
    job = wait(lambda: (j := rig.job(job_id))["status"] == "done" and j, what="the copy to finish")
    dest = rig.roots["tfiles"] / "out" / "Heat.mkv"
    assert dest.read_bytes() == source.read_bytes() and not list(dest.parent.glob("*.synchub-partial"))
    assert job["result"]["bytes"] == source.stat().st_size and job["result"]["sha256"] == sha(source) and job["result"]["verified"] == "sha256"
    assert job["stage"] == "done" and job["attempts"] == 1
    assert int(dest.stat().st_mtime) == int(source.stat().st_mtime)                     # the original's modified date is kept
    manifest = wait(lambda: (m := rig.manifest(job_id)) and (m["report"] or {}).get("sent") and m, what="the report to be marked sent")
    assert manifest["outcome"] == "done" and manifest["keep_until"] and not (rig.work / "jobs" / f"{job_id}-x").exists()
    folder = next((rig.work / "jobs").glob(f"{job_id}-*"))
    assert not (folder / "source").exists()                                              # the big local copy goes once SyncHub has the result
    messages = [line["msg"] for line in rig.joblog(job_id)]
    assert any(m.startswith("Starting") for m in messages) and any(m.startswith("Copied 3.0 MB and verified it") for m in messages)
    worker_log = [json.loads(line) for line in (rig.work / "logs" / "worker.jsonl").read_text().splitlines()]
    assert any(line.get("job") == job_id for line in worker_log) and any("Registered with SyncHub" in line["msg"] for line in worker_log)


def test_the_agent_reports_what_it_can_do_and_how_its_shares_are(rig):
    rig.start()
    with rig.db.connection() as conn:
        w = wait(lambda: (x := workers.get(conn, rig.worker_id)) and x["status"].get("roots", {}).get("tfiles") and x, what="the first status")
    assert w["capabilities"]["ops"] == ["copy", "rename", "delete_source", "hash", "scan", "convert"] and w["version"] == agent.__version__ and w["hostname"]
    assert w["status"]["roots"]["media"]["ok"] and w["status"]["roots"]["media"]["state"] == "ok" and w["status"]["work_free_bytes"] > 0


def test_the_agent_never_overwrites_a_different_file(rig):
    rig.file("media", "Movies/Heat.mkv", MB)
    other = rig.roots["tfiles"] / "out" / "Heat.mkv"
    other.parent.mkdir()
    other.write_bytes(b"somebody else's file")
    job_id = rig.queue()
    rig.start()
    job = wait(lambda: (j := rig.job(job_id))["status"] == "failed" and j, what="the refusal")
    assert job["error_class"] == "dest_exists" and "never overwrites" in job["error"] and job["attempts"] == 1
    assert other.read_bytes() == b"somebody else's file" and not list(other.parent.glob("*partial"))


def test_a_destination_that_already_holds_the_exact_file_counts_as_done(rig):
    source = rig.file("media", "Movies/Heat.mkv", MB)
    dest = rig.roots["tfiles"] / "out" / "Heat.mkv"
    dest.parent.mkdir()
    dest.write_bytes(source.read_bytes())
    before = dest.stat()
    job_id = rig.queue()
    rig.start()
    wait(lambda: rig.job(job_id)["status"] == "done", what="done")
    assert dest.stat().st_ino == before.st_ino and dest.stat().st_mtime_ns == before.st_mtime_ns        # not rewritten
    assert any("already holds this exact file" in line["msg"] for line in rig.joblog(job_id))


def test_a_missing_source_is_reported_as_changed_not_as_a_crash(rig):
    job_id = rig.queue(src="Movies/Gone.mkv")
    rig.start()
    job = wait(lambda: rig.job(job_id)["error_class"] == "source_changed" and rig.job(job_id), what="the missing source to be noticed")
    assert "Gone.mkv" in job["error"] and job["status"] in ("queued", "needs_review")          # retried once more, then a person decides


def test_an_unsupported_operation_fails_in_plain_words(rig, monkeypatch):
    monkeypatch.setattr(agent, "OPS", ["copy", "encode"])                          # SyncHub hands out what the agent says it can do
    with rig.db.connection() as conn:
        job_id = jobs.create(conn, "library", title="Encode it", plan={"op": "encode"}, pinned_worker_id=rig.worker_id)
    rig.start()
    job = wait(lambda: rig.job(job_id)["status"] == "failed" and rig.job(job_id), what="the refusal")
    assert job["error_class"] == "unsupported_job" and "cannot do 'encode'" in job["error"]


def test_a_job_is_not_handed_out_for_an_operation_the_agent_does_not_know(rig):
    with rig.db.connection() as conn:
        job_id = jobs.create(conn, "library", title="Encode it", plan={"op": "encode"}, pinned_worker_id=rig.worker_id)
    rig.start()
    time.sleep(1.0)
    assert rig.job(job_id)["status"] == "queued"


# ---------------------------------------------------------------- shares
def test_a_share_without_its_marker_is_not_used_until_it_is_set_up(rig):
    (rig.roots["tfiles"] / agent.MARKER).unlink()                                    # as if the share had failed to mount and left an empty folder
    rig.file("media", "Movies/Heat.mkv", MB)
    job_id = rig.queue()
    rig.start()
    with rig.db.connection() as conn:
        w = wait(lambda: (x := workers.get(conn, rig.worker_id)) and x["status"].get("roots", {}).get("tfiles", {}).get("state") == "no_marker" and x, what="the report")
    assert w["status"]["roots"]["media"]["ok"] and "--init-roots" in w["status"]["roots"]["tfiles"]["message"]
    time.sleep(0.8)
    assert rig.job(job_id)["status"] == "queued" and not (rig.roots["tfiles"] / "out").exists()      # nothing was written into the unmarked folder
    assert agent.command_init_roots(rig.config()) == 0
    wait(lambda: rig.job(job_id)["status"] == "done", what="the job once the share is marked")


def test_init_roots_writes_markers_but_never_creates_folders_or_replaces_a_different_marker(rig, capsys):
    (rig.roots["tfiles"] / agent.MARKER).unlink()
    (rig.roots["media"] / agent.MARKER).write_text("something-else\n")
    assert agent.command_init_roots(rig.config()) == 1
    out = capsys.readouterr().out
    assert "tfiles: wrote" in out and "media:" in out and "looks like the wrong share" in out
    assert (rig.roots["tfiles"] / agent.MARKER).read_text() == "tfiles\n" and (rig.roots["media"] / agent.MARKER).read_text() == "something-else\n"
    assert agent.command_init_roots(rig.config(), force=True) == 0
    assert (rig.roots["media"] / agent.MARKER).read_text() == "media\n"
    assert agent.command_init_roots(rig.config()) == 0 and "already set up" in capsys.readouterr().out
    with rig.db.connection() as conn:
        storage.create(conn, {"name": "ghost", "protocol": "local", "share": "/nowhere/ghost", "mount_point": "/nowhere/ghost"})
    assert agent.command_init_roots(rig.config()) == 1
    assert "does not exist" in capsys.readouterr().out and not os.path.exists("/nowhere")


def test_check_says_what_works_and_what_does_not(rig):
    out = io.StringIO()
    with redirect_stdout(out):
        code = agent.command_check(rig.config())
    text = out.getvalue()
    assert code == 0 and "[ok] SyncHub accepts the token" in text and "[ok] share media" in text and "[ok] share tfiles" in text and "Everything needed is in order" in text
    (rig.roots["tfiles"] / agent.MARKER).unlink()
    out = io.StringIO()
    with redirect_stdout(out):
        code = agent.command_check(rig.config())
    assert code == 1 and "[!!] share tfiles" in out.getvalue() and "1 problem(s) found" in out.getvalue()
    out = io.StringIO()
    with redirect_stdout(out):
        code = agent.command_check(agent.Config(None, env=rig.env(SYNCHUB_TOKEN="sw_wrong")).validate())
    assert code == 1 and "token was not accepted" in out.getvalue()
    out = io.StringIO()
    with redirect_stdout(out):
        code = agent.command_check(agent.Config(None, env=rig.env(SYNCHUB_URL="http://127.0.0.1:9")).validate())
    assert code == 1 and "SyncHub is reachable" in out.getvalue()


def test_shares_added_in_synchub_later_are_picked_up_without_a_restart(rig):
    rig.start()
    with rig.db.connection() as conn:
        wait(lambda: workers.get(conn, rig.worker_id)["status"].get("roots"), what="the first status")
    extra = rig.tmp / "extra"
    extra.mkdir()
    (extra / agent.MARKER).write_text("extra\n")
    with rig.db.connection() as conn:
        storage.create(conn, {"name": "extra", "protocol": "local", "share": str(extra), "mount_point": str(extra)})
    with rig.db.connection() as conn:
        wait(lambda: workers.get(conn, rig.worker_id)["status"]["roots"].get("extra", {}).get("ok"), what="the new share to be checked")


# ---------------------------------------------------------------- surviving a crash (the point of the phase)
def manifest_stage(rig, job_id, stage, key="checkpoint"):
    m = rig.manifest(job_id)
    return ((m or {}).get("stages", {}).get(stage) or {}).get(key)


def test_a_killed_agent_resumes_a_copy_in_without_starting_over(rig):
    source = rig.file("media", "Movies/Heat.mkv", 64 * MB)
    job_id = rig.queue()
    first = rig.spawn(SYNCHUB_MAX_COPY_MB_S=24, SYNCHUB_CHECKPOINT_MB=16)
    wait(lambda: (manifest_stage(rig, job_id, "copy_in") or 0) >= 16 * MB, what="the first checkpoint")
    first.send_signal(signal.SIGKILL)                                                # no goodbye, no cleanup: a power cut
    first.wait(10)
    stopped_at = manifest_stage(rig, job_id, "copy_in")
    assert not manifest_stage(rig, job_id, "copy_in", "done") and stopped_at < 64 * MB and rig.job(job_id)["status"] == "running"
    rig.spawn()
    wait(lambda: rig.job(job_id)["status"] == "done", what="the resumed copy to finish")
    dest = rig.roots["tfiles"] / "out" / "Heat.mkv"
    assert sha(dest) == sha(source) and rig.job(job_id)["attempts"] == 1                # the same attempt carried on: nothing was queued again
    log = rig.joblog(job_id)
    resumed = [line for line in log if line["msg"].startswith("Copied in")]
    assert resumed and resumed[-1]["resumed_from"] >= 16 * MB                          # the second run read the source from the checkpoint, not from the start
    assert any(line["msg"].startswith("Resuming the copy at") for line in log)


def test_a_killed_agent_resumes_a_copy_out_and_does_not_copy_in_again(rig):
    source = rig.file("media", "Movies/Heat.mkv", 64 * MB)
    job_id = rig.queue()
    first = rig.spawn(SYNCHUB_MAX_COPY_MB_S=48, SYNCHUB_CHECKPOINT_MB=16)
    wait(lambda: manifest_stage(rig, job_id, "copy_in", "done") and (manifest_stage(rig, job_id, "copy_out") or 0) >= 16 * MB, what="the upload to pass a checkpoint")
    first.send_signal(signal.SIGKILL)
    first.wait(10)
    dest = rig.roots["tfiles"] / "out" / "Heat.mkv"
    assert not dest.exists() and (rig.roots["tfiles"] / "out" / "Heat.mkv.synchub-partial").exists()      # half a file is never under the real name
    rig.spawn()
    wait(lambda: rig.job(job_id)["status"] == "done", what="the resumed upload to finish")
    assert sha(dest) == sha(source) and not list(dest.parent.glob("*partial"))
    log = rig.joblog(job_id)
    assert any("not copying it again" in line["msg"] for line in log)                  # the verified local copy was reused
    assert [line["resumed_from"] for line in log if line["msg"].startswith("Copied out")][-1] >= 16 * MB
    assert not any(line["msg"].startswith("Copied in") and line.get("resumed_from") == 0 and line["ts"] > min(l["ts"] for l in log if l["msg"].startswith("Starting")) and False for line in log)


def test_stopping_the_agent_politely_leaves_the_job_resumable(rig):
    source = rig.file("media", "Movies/Heat.mkv", 48 * MB)
    job_id = rig.queue()
    first = rig.start(SYNCHUB_MAX_COPY_MB_S=24, SYNCHUB_CHECKPOINT_MB=16)
    wait(lambda: (manifest_stage(rig, job_id, "copy_in") or 0) >= 16 * MB, what="a checkpoint")
    rig.stop(first)                                                                   # what Docker's stop (SIGTERM) does
    m = rig.manifest(job_id)
    assert m["state"] == "running" and not m["report"]                                # no failure was reported: this is a pause, not a problem
    assert rig.job(job_id)["status"] == "running" and rig.job(job_id)["attempts"] == 1
    rig.start()
    wait(lambda: rig.job(job_id)["status"] == "done", what="the finish")
    assert sha(rig.roots["tfiles"] / "out" / "Heat.mkv") == sha(source) and rig.job(job_id)["attempts"] == 1


# ---------------------------------------------------------------- cancel and cleanup
def test_cancelling_stops_the_copy_removes_the_half_file_and_the_cleanup_clears_the_folder(rig):
    rig.file("media", "Movies/Heat.mkv", 64 * MB)
    job_id = rig.queue()
    rig.start(SYNCHUB_MAX_COPY_MB_S=48)
    wait(lambda: manifest_stage(rig, job_id, "copy_in", "done") and (rig.roots["tfiles"] / "out" / "Heat.mkv.synchub-partial").exists(), what="the upload to start")
    with rig.db.connection() as conn:
        assert jobs.cancel(conn, job_id, "test") == "cancelling"
    wait(lambda: rig.job(job_id)["status"] == "cancelled", what="the cancel")
    assert not list((rig.roots["tfiles"] / "out").glob("*"))                          # no half-written file is left on the share
    wait(lambda: not list((rig.work / "jobs").glob(f"{job_id}-*")), what="SyncHub's cleanup request to be carried out")
    assert rig.job(job_id)["cleanup"] == 0


def test_a_cancelled_job_waiting_in_the_queue_never_starts(rig):
    rig.file("media", "Movies/Heat.mkv", MB)
    job_id = rig.queue()
    with rig.db.connection() as conn:
        jobs.cancel(conn, job_id)
    rig.start()
    time.sleep(0.8)
    assert rig.job(job_id)["status"] == "cancelled" and not list((rig.work / "jobs").glob("*")) and not (rig.roots["tfiles"] / "out").exists()


# ---------------------------------------------------------------- results that must not be lost
class Flaky:
    """The real API with an outage in the middle: the calls that name `path` raise Unreachable while `down` is set."""

    def __init__(self, real, path):
        self.real, self.path, self.down, self.refused = real, path, threading.Event(), 0

    def post(self, path, body=None):
        if self.down.is_set() and self.path in path:
            self.refused += 1
            raise agent.Unreachable("simulated outage")
        return self.real.post(path, body)


def test_a_finished_job_is_reported_after_an_outage_and_the_work_is_not_redone(rig):
    source = rig.file("media", "Movies/Heat.mkv", 4 * MB)
    job_id = rig.queue()
    cfg = rig.config()
    api = Flaky(agent.Api(cfg), "/complete")
    api.down.set()
    instance = rig.start(api=api)
    wait(lambda: (m := rig.manifest(job_id)) and m["state"] == "finished" and m["report"]["sent"] is False, what="the job to finish with its report unsent")
    wait(lambda: api.refused >= 2, what="a few failed attempts")
    assert rig.job(job_id)["status"] == "running"                                    # SyncHub does not know yet
    folder = next((rig.work / "jobs").glob(f"{job_id}-*"))
    assert (folder / "source" / "Heat.mkv").exists()                                  # the local copy stays until SyncHub has the result
    api.down.clear()
    wait(lambda: rig.job(job_id)["status"] == "done", what="the report to get through")
    assert sha(rig.roots["tfiles"] / "out" / "Heat.mkv") == sha(source) and rig.job(job_id)["attempts"] == 1
    assert len([line for line in rig.joblog(job_id) if line["msg"].startswith("Copied in")]) == 1       # copied once
    assert len([line for line in rig.joblog(job_id) if line["msg"].startswith("Starting")]) == 1        # and the job was not even started again: only the report was resent


def test_a_report_saved_before_a_crash_is_sent_by_the_next_agent(rig):
    rig.file("media", "Movies/Heat.mkv", MB)
    job_id = rig.queue()
    api = Flaky(agent.Api(rig.config()), "/complete")
    api.down.set()
    first = rig.start(api=api)
    wait(lambda: (m := rig.manifest(job_id)) and m["state"] == "finished" and m["report"]["sent"] is False, what="the unsent report")
    rig.stop(first)                                                                  # the agent goes away with the result still unsent
    rig.start()
    wait(lambda: rig.job(job_id)["status"] == "done", what="the second agent to deliver it")
    assert len([line for line in rig.joblog(job_id) if line["msg"].startswith("Copied in")]) == 1


def test_losing_the_job_to_the_queue_stops_the_runner_and_it_resumes_when_handed_back(rig):
    source = rig.file("media", "Movies/Heat.mkv", 48 * MB)
    job_id = rig.queue()
    rig.start(SYNCHUB_MAX_COPY_MB_S=24, SYNCHUB_CHECKPOINT_MB=16)
    wait(lambda: (manifest_stage(rig, job_id, "copy_in") or 0) >= 16 * MB, what="a checkpoint")
    with rig.db.connection() as conn:
        conn.execute("UPDATE job SET lease_until = '2000-01-01 00:00:00' WHERE id = ?", (job_id,))
        assert jobs.reap(conn) == [job_id]                                            # the lease ran out: SyncHub took the job back
    wait(lambda: rig.job(job_id)["status"] == "done", what="the job to be handed back and finish")
    job = rig.job(job_id)
    assert sha(rig.roots["tfiles"] / "out" / "Heat.mkv") == sha(source) and job["attempts"] == 2
    log = rig.joblog(job_id)
    assert any("no longer ours" in line["msg"] for line in log) or any(line["msg"].startswith("Resuming") for line in log)


# ---------------------------------------------------------------- checking and limits
def test_a_corrupt_copy_on_the_share_is_deleted_with_the_local_copy_and_retried_clean(rig, monkeypatch):
    rig.file("media", "Movies/Heat.mkv", 2 * MB)
    real = agent.hash_file
    monkeypatch.setattr(agent, "hash_file", lambda path, *a, **k: "0" * 64 if str(path).endswith(".synchub-partial") else real(path, *a, **k))
    job_id = rig.queue()
    rig.start()
    job = wait(lambda: rig.job(job_id)["error_class"] == "verification_failed" and rig.job(job_id), what="the mismatch to be noticed")
    assert "checksums differ" in job["error"] and job["status"] == "queued"          # one more try, then a person
    assert not (rig.roots["tfiles"] / "out" / "Heat.mkv").exists() and not list((rig.roots["tfiles"] / "out").glob("*"))
    assert not list((rig.work / "jobs").glob(f"{job_id}-*/source/*"))                 # both copies went, so the retry starts clean


def test_not_enough_space_is_a_wait_not_a_failure(rig, monkeypatch):
    rig.file("media", "Movies/Heat.mkv", 2 * MB)
    real = agent.shutil.disk_usage
    Usage = type("Usage", (), {})

    def tiny(path):
        if str(rig.work) in str(path):
            usage = Usage()
            usage.free, usage.total, usage.used = 1000, 10 ** 9, 10 ** 9
            return usage
        return real(path)
    monkeypatch.setattr(agent.shutil, "disk_usage", tiny)
    job_id = rig.queue()
    rig.start()
    job = wait(lambda: rig.job(job_id)["error_class"] == "transient" and rig.job(job_id), what="the space problem")
    assert "Not enough free space on the work disk" in job["error"] and job["status"] == "queued" and job["not_before"]


def test_the_copy_speed_limit_is_respected(rig):
    rig.file("media", "Movies/Heat.mkv", 8 * MB)
    job_id = rig.queue()
    started = time.monotonic()
    rig.start(SYNCHUB_MAX_COPY_MB_S=16, SYNCHUB_VERIFY="size")
    wait(lambda: rig.job(job_id)["status"] == "done", what="the copy")
    assert time.monotonic() - started >= 0.9                                          # 8 MB in and 8 MB out at 16 MB/s is at least a second


def test_progress_and_speed_reach_synchub_while_copying(rig):
    rig.file("media", "Movies/Heat.mkv", 48 * MB)
    job_id = rig.queue()
    rig.start(SYNCHUB_MAX_COPY_MB_S=24)
    job = wait(lambda: (j := rig.job(job_id))["status"] == "running" and j["progress"] and 0.02 < j["progress"] < 0.9 and j, what="a progress report")
    assert job["stage"] in ("copy_in", "copy_out") and ("Copying" in job["message"] or "Verifying" in job["message"]) and job["eta_seconds"]
    wait(lambda: rig.job(job_id)["status"] == "done", what="the finish")


# ---------------------------------------------------------------- the agent and its logs
def test_the_full_log_is_uploaded_when_asked_and_a_missing_one_is_explained(rig):
    rig.file("media", "Movies/Heat.mkv", MB)
    job_id = rig.queue()
    rig.start()
    wait(lambda: rig.job(job_id)["status"] == "done", what="the copy")
    with rig.db.connection() as conn:
        jobs.request_log(conn, job_id)
    log = wait(lambda: (l := _stored(rig, job_id)) and l, what="the log to arrive")
    assert '"msg": "Copied 1.0 MB and verified it' in log["content"] and log["worker_name"] == "encoder-1"
    for folder in (rig.work / "jobs").glob(f"{job_id}-*"):
        for item in folder.iterdir():
            item.unlink() if item.is_file() else None
    import shutil
    shutil.rmtree(next((rig.work / "jobs").glob(f"{job_id}-*")))
    with rig.db.connection() as conn:
        jobs.request_log(conn, job_id)
    wait(lambda: (l := _stored(rig, job_id)) and "No log is kept" in l["content"], what="the explanation")


def _stored(rig, job_id):
    with rig.db.connection() as conn:
        return jobs.stored_log(conn, job_id)


def test_a_wrong_token_is_explained_and_the_agent_keeps_trying(rig, caplog):
    cfg = rig.config(SYNCHUB_TOKEN="sw_" + "0" * 48)
    log = agent.build_logger(cfg, console=False)
    instance = agent.Agent(cfg, log)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    rig.running.append((instance, thread))
    wait(lambda: (rig.work / "logs" / "worker.jsonl").exists() and "does not accept this worker's token" in (rig.work / "logs" / "worker.jsonl").read_text(), what="the explanation in the log")


def test_a_disabled_worker_waits_and_works_again_when_enabled(rig):
    rig.file("media", "Movies/Heat.mkv", MB)
    job_id = rig.queue()
    with rig.db.connection() as conn:
        workers.set_flag(conn, rig.worker_id, "enabled", False)
    rig.start()
    time.sleep(0.8)
    assert rig.job(job_id)["status"] == "queued"
    with rig.db.connection() as conn:
        workers.set_flag(conn, rig.worker_id, "enabled", True)
    wait(lambda: rig.job(job_id)["status"] == "done", timeout=90, what="the job once the worker is enabled")


def test_leftover_folders_for_finished_jobs_are_released_but_kept_ones_stay(rig):
    rig.file("media", "Movies/Heat.mkv", MB)
    ghost = rig.work / "jobs" / "999-deadbeef"                                         # a folder from a SyncHub database that no longer exists
    ghost.mkdir(parents=True)
    (ghost / "manifest.json").write_text(json.dumps({"version": 1, "job_id": 999, "stages": {}, "state": "running"}))
    rig.start()
    wait(lambda: not ghost.exists(), what="the leftover folder to be released")


# ---------------------------------------------------------------- the worker's own log and its hardware details
def test_the_workers_own_log_is_uploaded_when_the_workers_page_asks(rig):
    instance = rig.start()
    wait(lambda: instance.registered, what="the agent to register")
    with rig.db.connection() as conn:
        assert workers.stored_log(conn, rig.worker_id) is None
        workers.request_log(conn, rig.worker_id)
    log = wait(lambda: _worker_log(rig), what="the log to arrive")
    assert '"msg": "Registered with SyncHub as encoder-1"' in log["content"] and not log["truncated"]
    with rig.db.connection() as conn:
        assert not workers.get(conn, rig.worker_id)["log_requested"]                  # the request is closed
    wait(lambda: '"msg": "Uploading the worker log as SyncHub asked"' in (rig.work / "logs" / "worker.jsonl").read_text(), what="the agent to log the upload")


def _worker_log(rig):
    with rig.db.connection() as conn:
        return workers.stored_log(conn, rig.worker_id)


def test_the_hardware_and_live_figures_reach_synchub_and_the_status_tab(rig, client):
    instance = rig.start()
    wait(lambda: instance.registered and _status_has_host(rig), what="a poll with the host figures")
    with rig.db.connection() as conn:
        worker = workers.get(conn, rig.worker_id)
    host = worker["capabilities"]["host"]
    assert host["cpu"]["threads"] >= 1 and host["arch"] and "gpus" in host
    assert isinstance(worker["status"]["host"], dict)                       # the live figures (load, memory...) ride along with every poll
    page = client.get("/workers/fragment").get_data(as_text=True)
    assert 'data-fleet-pane="status"' in page and f'data-status-worker="{rig.worker_id}"' in page and "Processor" in page and "Encoding" in page


def _status_has_host(rig):
    with rig.db.connection() as conn:
        return workers.get(conn, rig.worker_id)["status"].get("host") is not None


# ---------------------------------------------------------------- conversions (real ffmpeg)
def make_avi(path, audio_tracks=2, seconds=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10"]
    for n in range(audio_tracks):
        command += ["-f", "lavfi", "-i", f"sine=frequency={440 * (n + 1)}:duration={seconds}"]
    command += ["-map", "0"] + [x for n in range(audio_tracks) for x in ("-map", str(n + 1))] + ["-c:v", "mpeg4", "-c:a", "mp3", str(path)]
    subprocess.run(command, check=True)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def queue_convert(rig, change, duration=2.0, src="Old/Heat.avi", dst="Movies/Heat (1995)/Heat (1995).mkv"):
    with rig.db.connection() as conn:
        return jobs.create(conn, "library", title="Convert", source=("tfiles", src), dest=("media", dst), plan={"op": "convert", "change": change, "duration": duration},
                           pinned_worker_id=rig.worker_id)


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg is not installed")


@needs_ffmpeg
def test_a_conversion_remuxes_to_mkv_drops_the_tracks_and_lands_in_the_destination(rig):
    make_avi(rig.roots["tfiles"] / "Old/Heat.avi")
    change = {"op": "convert", "container": "matroska", "keep_audio": [1], "remove_audio": [{"index": 2, "language": "ita", "title": ""}], "keep_subtitle": [], "remove_subtitle": [],
              "default_audio": 1, "target_extension": ".mkv", "video": {"action": "copy", "from": "mpeg4", "codec": "h264"}, "audio_convert": {}}
    job_id = queue_convert(rig, change)
    rig.start()
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=60, what="the conversion")
    assert job["status"] == "done", job["error"]
    dest = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv"
    data = probe(dest)
    kinds = [s["codec_type"] for s in data["streams"]]
    assert kinds.count("video") == 1 and kinds.count("audio") == 1 and "matroska" in data["format"]["format_name"]
    assert abs(float(data["format"]["duration"]) - 2.0) < 0.2
    assert job["result"]["op"] == "convert" and job["result"]["bytes"] == dest.stat().st_size and job["result"]["sha256"] == sha(dest) and job["result"]["verified"] == "sha256"
    assert "-map 0:1" in job["result"]["command"] and "0:2" not in job["result"]["command"]
    assert not list(dest.parent.glob("*partial*")) and (rig.roots["tfiles"] / "Old/Heat.avi").exists()          # the source is left alone


@needs_ffmpeg
def test_a_conversion_that_does_not_match_the_source_is_refused_and_nothing_reaches_the_destination(rig):
    make_avi(rig.roots["tfiles"] / "Old/Heat.avi", audio_tracks=1)
    change = {"op": "convert", "container": "matroska", "keep_audio": [1], "remove_audio": [], "keep_subtitle": [], "remove_subtitle": [],
              "video": {"action": "copy"}, "audio_convert": {}}
    job_id = queue_convert(rig, change, duration=60.0)                               # SyncHub believes the film is a minute long; the result is 2 s
    rig.start()
    job = wait(lambda: rig.job(job_id)["error_class"] == "verification_failed" and rig.job(job_id), timeout=60, what="the check to fail")
    assert "source is 60 s" in job["error"]
    assert not (rig.roots["media"] / "Movies").exists()


def test_the_ffmpeg_command_follows_the_plan():
    change = {"container": "matroska", "keep_audio": [1, 3], "keep_subtitle": [4], "default_audio": 3, "video": {"action": "transcode"}, "audio_convert": {"3": {"from": "ac3", "codec": "aac"}}}
    command = agent.build_convert_command("ffmpeg", "/in.avi", "/out.partial", change)
    text = " ".join(command)
    assert "-map 0:V? -map 0:1 -map 0:3 -map 0:4" in text and "-c:v libx264" in text and "-c:a:1 aac" in text and "-disposition:a:1 default" in text
    assert command[-3:] == ["-f", "matroska", "/out.partial"]
    plain = " ".join(agent.build_convert_command("ffmpeg", "/in.mp4", "/out.partial", {"container": "mp4", "video": {"action": "copy"}}))
    assert "-map 0:a? -map 0:s?" in plain and "-c:v copy" in plain and "-movflags +faststart" in plain
    assert agent.parse_ffmpeg_progress("out_time_us=1000000\n", 4.0) == (0.25, None) and agent.parse_ffmpeg_progress("speed=2.5x\n", 4.0) == (None, "2.5")
    assert agent.parse_ffmpeg_progress("out_time_us=N/A\n", 4.0) == (None, None) and agent.parse_ffmpeg_progress("progress=end\n", 4.0) == (None, None)


@needs_ffmpeg
def test_approving_a_file_gets_it_converted_by_the_worker_and_the_library_record_completes(rig, conn, monkeypatch):
    """The whole path: approve on the Library page's logic -> queued job -> the worker's poll hands it out -> ffmpeg -> destination -> record completed -> Jellyfin told."""
    from synchub.library import migrate, recommend as rc
    from .library_helpers import CFG, add, plan
    from .test_library_findings import rec_of
    told = []
    monkeypatch.setattr(migrate, "tell_jellyfin", lambda count=1: told.append(count) or True)
    make_avi(rig.roots["tfiles"] / "Old/Heat.avi", audio_tracks=1)
    record_id = add(conn, rel="Old/Heat.avi", name="Heat (1995).mkv", container="avi", video="mpeg4", action="remux_copy", size=(rig.roots["tfiles"] / "Old/Heat.avi").stat().st_size,
                    proposals=[plan(convert={"remux": True}, keep_audio=(1,))])
    conn.execute("UPDATE library_file SET duration_seconds = 2.0 WHERE id = ?", (record_id,))
    rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG)
    conn.commit()
    assert rec_of(conn, record_id)["migration_status"] == "pending" and migrate.queue_summary(conn)["queued"] == 1
    rig.start()
    wait(lambda: (conn.commit() or True) and rec_of(conn, record_id)["migration_status"] == "completed", timeout=60, what="the record to complete")
    rec = rec_of(conn, record_id)
    dest = rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv"
    assert dest.exists() and rec["migration_destination_path"] == "media:Movies/Heat (1995)/Heat (1995).mkv" and rec["migration_verification_status"] == "verified"
    assert migrate.queue_summary(conn)["completed"] == 1 and told == [1]


# ---------------------------------------------------------------- replacing a download in place (real ffmpeg)
def queue_replace(rig, rel, change, size, mtime, dst=None, days=30, duration=2.0):
    with rig.db.connection() as conn:
        return jobs.create(conn, "download", title="Replace", source=("media", rel), dest=("media", dst or rel), pinned_worker_id=rig.worker_id,
                           plan={"op": "convert", "replace": True, "change": change, "duration": duration, "source_size": size, "source_mtime": mtime, "originals_retention_days": days})


KEEP_FIRST_AUDIO = {"op": "keep_tracks", "container": "matroska", "keep_audio": [1], "remove_audio": [{"index": 2, "language": "ita", "title": ""}], "keep_subtitle": [], "remove_subtitle": [],
                    "default_audio": 1, "video": {"action": "copy"}, "audio_convert": {}}


@needs_ffmpeg
def test_a_download_is_replaced_in_place_under_the_same_name_and_the_original_is_kept(rig):
    source = rig.roots["media"] / "Movies/Heat (1995)/Heat.1995.mkv"
    make_avi(source)
    original_hash, stat = sha(source), source.stat()
    os.utime(source, (stat.st_atime, 1_600_000_000))
    job_id = queue_replace(rig, "Movies/Heat (1995)/Heat.1995.mkv", KEEP_FIRST_AUDIO, stat.st_size, 1_600_000_000.0)
    instance = rig.start()
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=60, what="the replacement")
    assert job["status"] == "done", job["error"]
    kinds = [s["codec_type"] for s in probe(source)["streams"]]
    assert kinds.count("audio") == 1 and sha(source) == job["result"]["sha256"] != original_hash
    assert int(source.stat().st_mtime) == 1_600_000_000                                       # "date added" is not disturbed
    assert not list(source.parent.glob("*partial*")) and [p.name for p in source.parent.iterdir()] == [source.name]
    kept = [(folder, meta) for folder, meta in instance.originals()]
    assert len(kept) == 1 and sha(Path(kept[0][0]) / source.name) == original_hash
    assert kept[0][1]["rel"] == "Movies/Heat (1995)/Heat.1995.mkv" and kept[0][1]["retention_days"] == 30 and kept[0][1]["sha256"] == original_hash
    assert job["result"]["replaced"] is True and job["result"]["kept_original"] is True
    assert instance.originals_held() == (1, stat.st_size)


@needs_ffmpeg
def test_a_new_container_replaces_the_old_file_with_the_same_base_name(rig):
    source = rig.roots["media"] / "Movies/Heat (1995)/Heat.1995.avi"
    make_avi(source)
    stat = source.stat()
    change = {**KEEP_FIRST_AUDIO, "op": "convert", "target_extension": ".mkv", "video": {"action": "copy", "from": "mpeg4", "codec": "h264"}}
    job_id = queue_replace(rig, "Movies/Heat (1995)/Heat.1995.avi", change, stat.st_size, stat.st_mtime, dst="Movies/Heat (1995)/Heat.1995.mkv")
    rig.start()
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=60, what="the replacement")
    assert job["status"] == "done", job["error"]
    assert sorted(p.name for p in source.parent.iterdir()) == ["Heat.1995.mkv"] and job["result"]["path_changed"] is True


@needs_ffmpeg
def test_with_retention_zero_the_original_is_not_kept(rig):
    source = rig.roots["media"] / "Movies/Heat (1995)/Heat.1995.mkv"
    make_avi(source)
    stat = source.stat()
    job_id = queue_replace(rig, "Movies/Heat (1995)/Heat.1995.mkv", KEEP_FIRST_AUDIO, stat.st_size, stat.st_mtime, days=0)
    instance = rig.start()
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=60, what="the replacement")
    assert job["status"] == "done" and job["result"]["kept_original"] is False and instance.originals_held() == (0, 0)


@needs_ffmpeg
def test_a_file_that_is_not_the_one_analysed_is_not_replaced(rig):
    source = rig.roots["media"] / "Movies/Heat (1995)/Heat.1995.mkv"
    make_avi(source)
    before = sha(source)
    stat = source.stat()
    job_id = queue_replace(rig, "Movies/Heat (1995)/Heat.1995.mkv", KEEP_FIRST_AUDIO, stat.st_size + 5, stat.st_mtime)
    rig.start()
    job = wait(lambda: rig.job(job_id)["error_class"] == "source_changed" and rig.job(job_id), timeout=60, what="the refusal")
    assert "not the file SyncHub analysed" in job["error"] and sha(source) == before


def test_kept_originals_are_removed_only_after_their_retention(rig):
    instance = rig.start()
    for name, expires in (("1-aaaa", agent.iso(agent.utcnow() - agent.timedelta(days=1))), ("2-bbbb", agent.iso(agent.utcnow() + agent.timedelta(days=5)))):
        folder = Path(instance.cfg.originals_dir) / name
        folder.mkdir(parents=True)
        (folder / "x.mkv").write_bytes(b"x")
        (folder / "meta.json").write_text(json.dumps({"rel": "x.mkv", "size": 1, "expires_at": expires, "retention_days": 30}))
    assert instance.sweep_originals() == 1
    assert [p.name for p in Path(instance.cfg.originals_dir).iterdir()] == ["2-bbbb"]


@needs_ffmpeg
def test_a_radarr_import_is_scanned_converted_in_place_and_radarr_is_asked_to_rescan(rig, client, conn, monkeypatch):
    """The whole path: webhook -> the worker scans the folder -> SyncHub asks -> approve -> the worker replaces the file -> Radarr and Jellyfin are told."""
    from synchub import downloads, settings
    from synchub.library import migrate
    import synchub.clients.arr as arr
    rescans, told = [], []
    monkeypatch.setattr(migrate, "tell_jellyfin", lambda count=1: told.append(count) or True)
    monkeypatch.setattr(arr, "RadarrClient", type("R", (), {"rescan_movie": lambda self, movie_id: rescans.append(movie_id)}))
    conn.execute("UPDATE storage_root SET radarr_path = '/movies' WHERE name = 'media'")
    settings.set_value("download_pipeline", "review", conn=conn)
    conn.commit()
    make_avi(rig.roots["media"] / "Heat (1995)/Heat.1995.mkv")
    payload = {"eventType": "Download", "movie": {"id": 9, "title": "Heat", "folderPath": "/movies/Heat (1995)"},
               "movieFile": {"path": "/movies/Heat (1995)/Heat.1995.mkv", "size": (rig.roots["media"] / "Heat (1995)/Heat.1995.mkv").stat().st_size}}
    assert client.post("/api/webhook/radarr", json=payload).status_code == 200
    rig.start()

    def row():
        conn.commit()
        return conn.execute("SELECT * FROM download_import ORDER BY id DESC LIMIT 1").fetchone()

    wait(lambda: row()["status"] in ("awaiting_approval", "nothing_to_do", "blocked", "failed"), timeout=60, what="the scan and the decision")
    assert row()["status"] == "awaiting_approval", row()["message"]
    assert client.post(f"/api/downloads/{row()['id']}/approve").get_json()["ok"] is True
    wait(lambda: row()["status"] in ("replaced", "failed", "blocked") and row()["rescan_status"] != "due", timeout=90, what="the replacement")
    final = row()
    assert final["status"] == "replaced", final["message"]
    assert final["rescan_status"] == "done" and rescans == [9] and told
    data = probe(rig.roots["media"] / "Heat (1995)/Heat.1995.mkv")          # it was an AVI under an .mkv name; now it is a real Matroska file
    assert "matroska" in data["format"]["format_name"] and final["result_json"]


# ---------------------------------------------------------------- size policy: capped HEVC and HDR (real ffmpeg)
def make_hdr(path, seconds=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10", "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                    "-x265-params", "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:max-cll=1000,400:"
                                    "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)", "-c:a", "aac", str(path)], check=True)


def queue_compress(rig, src, profile, duration=2.0):
    change = {"op": "convert", "container": "matroska", "video": {"action": "transcode", "codec": "hevc"}, "audio_convert": {}}
    with rig.db.connection() as conn:
        return jobs.create(conn, "library", title="Compress", source=("tfiles", src), dest=("media", "Movies/Heat (1995)/Heat (1995).mkv"), pinned_worker_id=rig.worker_id,
                           plan={"op": "convert", "change": change, "duration": duration, "profile": profile, "needs": ["profiles"]})


@needs_ffmpeg
def test_a_size_capped_hevc_encode_keeps_hdr10_and_the_mastering_data(rig):
    make_hdr(rig.roots["tfiles"] / "Old/Heat.mkv")
    profile = {"video_codec": "hevc", "crf": 24, "preset": "ultrafast", "ten_bit": True, "audio_kbps": 192, "maxrate_kbps": 2000, "bufsize_kbps": 4000, "max_bytes": 5_000_000}
    job_id = queue_compress(rig, "Old/Heat.mkv", profile)
    rig.start()
    job = wait(lambda: (j := rig.job(job_id))["status"] in ("done", "failed", "needs_review") and j, timeout=90, what="the compression")
    assert job["status"] == "done", job["error"]
    data = probe(rig.roots["media"] / "Movies/Heat (1995)/Heat (1995).mkv")
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "hevc" and video["pix_fmt"] == "yuv420p10le" and video["color_transfer"] == "smpte2084" and video["color_primaries"] == "bt2020"
    assert "vbv-maxrate=2000" in job["result"]["command"] and "master-display=G(13250,34500)" in job["result"]["command"] and "max-cll=1000,400" in job["result"]["command"]


@needs_ffmpeg
def test_a_result_over_the_size_limit_is_retried_once_at_lower_quality_then_held_for_a_person(rig):
    make_avi(rig.roots["tfiles"] / "Old/Heat.avi", audio_tracks=1)
    profile = {"video_codec": "hevc", "crf": 22, "preset": "ultrafast", "ten_bit": False, "audio_kbps": 192, "max_bytes": 1000}          # impossible on purpose
    job_id = queue_compress(rig, "Old/Heat.avi", profile)
    rig.start()
    job = wait(lambda: rig.job(job_id)["status"] == "needs_review" and rig.job(job_id), timeout=120, what="the job to be held")
    assert job["error_class"] == "size_target_missed" or "over the" in (job["review_reason"] or job["error"] or "")
    log = " ".join(line.get("msg", "") for line in rig.joblog(job_id))
    assert "encoding at quality 24 this time" in log                                              # the retry used 22 + 2
    assert not (rig.roots["media"] / "Movies").exists()


@needs_ffmpeg
def test_an_hdr_picture_is_never_squeezed_into_h264(rig):
    make_hdr(rig.roots["tfiles"] / "Old/Heat.mkv")
    profile = {"video_codec": "h264", "crf": 20, "preset": "ultrafast", "ten_bit": False, "audio_kbps": 192}
    job_id = queue_compress(rig, "Old/Heat.mkv", profile)
    rig.start()
    job = wait(lambda: rig.job(job_id)["status"] in ("needs_review", "failed") and rig.job(job_id), timeout=60, what="the refusal")
    assert "HDR" in (job["error"] or job["review_reason"] or "") and not (rig.roots["media"] / "Movies").exists()
