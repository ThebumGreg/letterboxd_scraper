"""Automatic worker updates: the SyncHub side (idle check, report, notification) and agent/auto-update.sh against real git repositories."""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from synchub import jobs, workers
from synchub.web import fleet
from synchub.notifications import events

from .test_notification_rules import admin, notes
from .test_notifications import push  # noqa: F401  (the pretend Pushover)

AUTO = Path(__file__).resolve().parents[2] / "agent" / "auto-update.sh"
UPDATE = Path(__file__).resolve().parents[2] / "agent" / "update-worker.sh"


# ---------------------------------------------------------------- SyncHub
def authed(v2, name="w1"):
    with v2.connection() as conn:
        wid, token = workers.create(conn, name)
    return wid, {"Authorization": f"Bearer {token}"}


def test_the_check_says_whether_a_restart_would_cut_a_job_off(client, v2):
    wid, headers = authed(v2)
    assert client.post("/api/worker/v1/update/check", json={}).status_code == 401
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["idle"] is True
    with v2.connection() as conn:
        jobs.create(conn, "library", title="A job")
        jobs.claim(conn, workers.get(conn, wid), 1)
    got = client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()
    assert got["idle"] is False and got["running"] == 1


def test_a_report_is_stored_shown_and_notified(client, v2, push):
    wid, headers = authed(v2, "worker-1")
    with v2.connection() as conn:
        admin(conn)
    ok = client.post("/api/worker/v1/update/report", headers=headers, json={"status": "ok", "version": "1.5.0", "previous": "1.4.0", "commit": "abc1234"})
    assert ok.status_code == 200
    bad = client.post("/api/worker/v1/update/report", headers=headers, json={"status": "failed", "commit": "def5678", "restored": "abc1234 (1.5.0)", "error": "pip: no space left"})
    assert bad.status_code == 200
    assert client.post("/api/worker/v1/update/report", headers=headers, json={"status": "maybe"}).status_code == 400
    with v2.connection() as conn:
        done, failed = notes(conn, "worker_update_done")[0], notes(conn, "worker_update_failed")[0]
        assert workers.last_update(conn, wid)["status"] == "failed"
    assert "worker-1 now runs 1.5.0 (abc1234), up from 1.4.0" in done["body"] and done["priority"] == -1
    assert "pip: no space left" in failed["body"] and "put back to abc1234 (1.5.0)" in failed["body"]
    page = client.get("/workers").get_data(as_text=True)
    assert "auto-update: update to def5678 failed" in page and "pip: no space left" in page


def test_both_events_can_be_switched_off_per_person():
    for code in ("worker_update_done", "worker_update_failed"):
        assert events.get(code).audience == events.ADMIN and events.get(code).configurable


# ---------------------------------------------------------------- agent/auto-update.sh
def sh(cmd, cwd, **env):
    done = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env={**os.environ, **env})
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


def commit(repo, version, note):
    (repo / "agent").mkdir(exist_ok=True)
    (repo / "agent" / "synchub_agent.py").write_text(f'__version__ = "{version}"\n')
    sh(f'git add -A && git -c user.name=t -c user.email=t@t commit -qm "{note}"', repo)


@pytest.fixture
def box(tmp_path):
    origin, clone, dirw, state, bin_dir = (tmp_path / n for n in ("origin.git", "clone", "worker", "state", "bin"))
    for p in (dirw, state, bin_dir, clone):
        p.mkdir()
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    sh(f"git clone -q {origin} {clone}", tmp_path)
    sh("git checkout -q -b main", clone)
    commit(clone, "1.0.0", "first")
    sh("git push -q -u origin main", clone)
    (dirw / ".env").write_text("SYNCHUB_URL=http://hub:5001\nSYNCHUB_TOKEN=sw_secret\n")
    calls = tmp_path / "calls.log"
    calls.write_text("")
    # curl: the idle check answers $SHIM_IDLE; a report is written down. SHIM_HUB_DOWN makes every call fail.
    curl = ('echo "curl $*" >> "$SHIM_LOG"\n[ -z "${SHIM_HUB_DOWN:-}" ] || { echo "curl: (7) down" >&2; exit 7; }\n'
            'case "$*" in *update/check*) if [ -n "${SHIM_ANSWER:-}" ]; then printf \"%s\\n\" \"$SHIM_ANSWER\"; else echo "{\\"ok\\":true,\\"idle\\":${SHIM_IDLE:-true}}"; fi;; *) echo "{\\"ok\\":true}";; esac')
    # the updater: succeeds, or fails with a build error while $SHIM_FAIL_UPDATE exists; writes down which version it was asked to deploy
    updater = ('case "$*" in *--ai-status*) cat "$SHIM_AI" 2>/dev/null || echo off; exit 0;; esac\n'
               'case "$*" in *"--ai-quality on"*) echo on > "$SHIM_AI";; *"--ai-quality off"*) echo off > "$SHIM_AI";; esac\n'
               'echo "update $* $(sed -n "s/.*\\"\\(.*\\)\\".*/\\1/p" "$SYNCHUB_REPO/agent/synchub_agent.py")" >> "$SHIM_LOG"\n'
               'if [ -f "$SHIM_FAIL_UPDATE" ] && grep -q 2.0.0 "$SYNCHUB_REPO/agent/synchub_agent.py"; then echo "ERROR: build failed: no space left"; exit 1; fi')
    for name, body in (("curl", curl), ("updater", updater), ("logger", "true"), ("docker", 'echo "docker $*" >> "$SHIM_LOG"')):
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    fail = tmp_path / "fail"
    maker = tmp_path / "maker"                                  # where new releases are made and pushed from
    sh(f"git clone -q {origin} {maker}", tmp_path)

    def run(*args, **extra):
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SHIM_LOG": str(calls), "SHIM_FAIL_UPDATE": str(fail), "SHIM_AI": str(tmp_path / "ai-state"), "SYNCHUB_UPDATE_CONF": str(tmp_path / "none"),
               "SYNCHUB_REPO": str(clone), "SYNCHUB_WORKER_DIR": str(dirw), "SYNCHUB_UPDATE_STATE": str(state), "SYNCHUB_UPDATE_LOCK": str(tmp_path / "lock"),
               "SYNCHUB_UPDATE_SCRIPT": str(bin_dir / "updater"), **extra}
        done = subprocess.run(["bash", str(AUTO), *args], capture_output=True, text=True, env=env, timeout=60)
        return done.returncode, done.stdout + done.stderr, calls.read_text()

    return type("Box", (), {"clone": clone, "origin": origin, "run": run, "fail": fail, "state": state, "calls": calls, "maker": maker, "sh": staticmethod(sh)})


def publish(box, version):
    box.sh("git pull -q --ff-only", box.maker)
    commit(box.maker, version, f"v{version}")
    box.sh("git push -q origin main", box.maker)


def head_version(box):
    return re.search(r'"(.*)"', (box.clone / "agent" / "synchub_agent.py").read_text()).group(1)


def test_nothing_new_does_nothing(box):
    code, out, calls = box.run()
    assert code == 0 and "update " not in calls and "update/report" not in calls


def reports_of(calls):
    """The reports the helper sent, without the 'started' notice that only makes the Jobs page show the update as running."""
    return [c for c in calls.splitlines() if "update/report" in c and '"status":"started"' not in c]


def test_an_idle_worker_is_updated_and_the_result_reported(box):
    publish(box, "1.1.0")
    code, out, calls = box.run()
    assert code == 0 and "Updated 1.0.0 (" in out and "-> 1.1.0 (" in out
    assert "update --dir" in calls and "--no-pull 1.1.0" in calls and head_version(box) == "1.1.0"
    report = reports_of(calls)[0]
    assert '"status":"ok"' in report and '"version":"1.1.0"' in report and '"previous":"1.0.0"' in report and "Bearer sw_secret" in report


def test_a_busy_worker_waits_unless_it_is_inside_the_window(box):
    publish(box, "1.1.0")
    code, out, calls = box.run(SHIM_IDLE="false")
    assert code == 0 and "waiting" in out and "update " not in calls and head_version(box) == "1.0.0"
    code, out, calls = box.run(SHIM_IDLE="false", SYNCHUB_UPDATE_WINDOW="00:00-23:59")
    assert "inside the maintenance window" in out and head_version(box) == "1.1.0"


def test_a_window_outside_the_time_or_not_understood_does_not_let_a_busy_worker_update(box):
    publish(box, "1.1.0")
    now = subprocess.run(["date", "+%H"], capture_output=True, text=True).stdout.strip()
    elsewhere = f"{(int(now) + 3) % 24:02d}:00-{(int(now) + 4) % 24:02d}:00"
    for window in (elsewhere, "tonight", ""):
        box.run(SHIM_IDLE="false", SYNCHUB_UPDATE_WINDOW=window)
        assert head_version(box) == "1.0.0", window


def test_a_window_over_midnight_counts(box):
    publish(box, "1.1.0")
    now = int(subprocess.run(["date", "+%H"], capture_output=True, text=True).stdout)
    window = f"{(now + 23) % 24:02d}:00-{(now + 2) % 24:02d}:00"          # started an hour ago, ends in two hours (may wrap past midnight)
    box.run(SHIM_IDLE="false", SYNCHUB_UPDATE_WINDOW=window)
    assert head_version(box) == "1.1.0"


def test_when_synchub_does_not_answer_it_waits_and_keeps_the_report(box):
    publish(box, "1.1.0")
    code, out, calls = box.run(SHIM_HUB_DOWN="1")
    assert "waiting" in out and head_version(box) == "1.0.0"
    code, out, calls = box.run(SHIM_HUB_DOWN="1", SYNCHUB_UPDATE_WINDOW="00:00-23:59")           # window: updates anyway, cannot report
    assert head_version(box) == "1.1.0" and (box.state / "pending.json").exists()
    code, out, calls = box.run()                                                                   # SyncHub is back: the kept report goes out
    assert not (box.state / "pending.json").exists() and len(reports_of(calls)) == 2 and '"status":"ok"' in reports_of(calls)[-1]


def test_a_failed_update_puts_the_old_code_back_and_is_not_tried_again(box):
    publish(box, "2.0.0")
    box.fail.write_text("1")
    code, out, calls = box.run()
    assert code == 1 and "FAILED" in out and head_version(box) == "1.0.0"
    updates = [c for c in calls.splitlines() if c.startswith("update ")]
    assert [u.rsplit(" ", 1)[1] for u in updates] == ["2.0.0", "1.0.0"]              # the new code was tried, then the old code was deployed again
    report = reports_of(calls)[0]
    assert '"status":"failed"' in report and "no space left" in report and '"restored"' in report
    for _ in range(3):
        again = box.run()
        assert again[0] == 0 and again[2].count("update ") == 2 and len(reports_of(again[2])) == 1        # nothing more, nothing more reported
    box.fail.unlink()
    (box.state / "last-check").unlink()                                              # an hour later
    publish(box, "2.1.0")
    code, out, calls = box.run()
    assert code == 0 and head_version(box) == "2.1.0"                                  # newer code is tried again


def test_commits_of_its_own_are_never_overwritten(box):
    commit(box.clone, "9.9.9", "local work")
    publish(box, "1.1.0")
    code, out, calls = box.run()
    assert code == 0 and "not on origin" in out and head_version(box) == "9.9.9" and "update " not in calls


def test_install_dry_run_writes_the_timer(box):
    code, out, _ = box.run("--install", "--dry-run", "--window", "02:00-06:00", "--dir", "/opt/w", SYNCHUB_UNIT_DIR="/x", SYNCHUB_REPO=str(AUTO.parents[1]))
    assert code == 0 and "OnUnitActiveSec=1min" in out and 'SYNCHUB_UPDATE_WINDOW="02:00-06:00"' in out and 'SYNCHUB_WORKER_DIR="/opt/w"' in out
    assert box.run("--install", "--dry-run", "--window", "tonight", SYNCHUB_REPO=str(AUTO.parents[1]))[0] == 1


# ---------------------------------------------------------------- agent/update-worker.sh keeps the running container when the build fails
def test_update_worker_says_the_old_worker_keeps_running_when_the_build_fails(tmp_path):
    repo = tmp_path / "repo"
    (repo / "agent").mkdir(parents=True)
    sh("git init -q", repo)
    for f in ("Dockerfile", "synchub_agent.py", "media_analysis.py"):
        (repo / "agent" / f).write_text("x\n")
    shutil_copy = tmp_path / "agent-copy"
    shutil_copy.mkdir()
    (repo / "agent" / "update-worker.sh").write_text(UPDATE.read_text())
    sh("git add -A && git -c user.name=t -c user.email=t@t commit -qm x", repo)
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "docker-compose.yml").write_text("services: {}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = ('case "$*" in\n  "compose config") echo "    context: ' + str(repo / "agent") + '" ;;\n  "compose up -d --build") echo "build failed" >&2; exit 1 ;;\n'
              '  "compose ps -q --status running") echo abc123 ;;\n  *) ;;\nesac')
    for name, body in (("docker", docker), ("mount", "echo x /mnt/tower/ x")):
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    done = subprocess.run(["bash", str(repo / "agent" / "update-worker.sh"), "--dir", str(worker), "--no-pull"], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert done.returncode == 1 and "still running, on its old code" in done.stderr


# ---------------------------------------------------------------- requests from the Workers page, settings from SyncHub
def answer(**fields):
    import json
    return json.dumps({"ok": True, "idle": True, "running": 0, "auto_update": True, "window": "", "request": None, **fields})


def test_an_update_request_updates_now_without_waiting_for_the_hour(box):
    publish(box, "1.1.0")
    box.run()                                                                       # the hourly check ran a minute ago and found it
    (box.clone.parent / "x").write_text("")
    publish(box, "1.2.0")
    code, out, calls = box.run(SHIM_ANSWER=answer(request="update"))
    assert "asked for an update now" in out and head_version(box) == "1.2.0"


def test_an_update_request_with_nothing_new_is_reported_as_current_and_retries_a_failed_commit(box):
    code, out, calls = box.run(SHIM_ANSWER=answer(request="update"))
    assert code == 0 and "Already on the newest code" in out and '"status":"current"' in calls
    publish(box, "2.0.0")
    box.fail.write_text("1")
    box.run(SHIM_ANSWER=answer(request="update"))
    assert head_version(box) == "1.0.0" and (box.state / "failed").exists()
    box.fail.unlink()
    box.run(SHIM_ANSWER=answer(request="update"))                                   # the person asked again: the failed commit is tried again
    assert head_version(box) == "2.0.0"


def test_a_restart_request_restarts_the_container_and_nothing_else(box):
    publish(box, "1.1.0")
    code, out, calls = box.run(SHIM_ANSWER=answer(request="restart"))
    assert "Restarted the worker" in out and head_version(box) == "1.0.0" and "update " not in calls


def test_automatic_updates_switched_off_in_synchub_stop_the_hourly_check(box):
    publish(box, "1.1.0")
    box.run(SHIM_ANSWER=answer(auto_update=False))
    assert head_version(box) == "1.0.0"
    box.run(SHIM_ANSWER=answer(auto_update=True))
    assert head_version(box) == "1.1.0"


def test_the_window_from_synchub_wins_over_the_conf_file(box):
    publish(box, "1.1.0")
    box.run(SHIM_ANSWER=answer(idle=False, running=1, window="00:00-23:59"), SYNCHUB_UPDATE_WINDOW="")
    assert head_version(box) == "1.1.0"


def test_a_busy_worker_is_looked_at_again_next_minute_and_logged_once(box):
    publish(box, "1.1.0")
    _, first, _ = box.run(SHIM_IDLE="false")
    _, second, _ = box.run(SHIM_IDLE="false")
    assert "waiting" in first and "waiting" not in second and head_version(box) == "1.0.0"
    box.run()
    assert head_version(box) == "1.1.0"


# ---------------------------------------------------------------- SyncHub: worker settings and host requests
def test_settings_are_validated_stored_and_sent_to_the_agent(client, v2):
    wid, headers = authed(v2, "w2")
    with v2.connection() as conn:
        workers.update(conn, wid, slots=3, settings={"encoder": "VAAPI", "gpu_slots": "1", "max_copy_mb_s": "40", "log_level": "debug", "update_window": "02:00-06:00", "auto_update": False})
        w = workers.get(conn, wid)
    assert w["settings"] == {"encoder": "vaapi", "max_copy_mb_s": 40.0, "log_level": "DEBUG", "update_window": "02:00-06:00", "auto_update": False}     # gpu_slots 1 is the default: not stored
    sent = client.post("/api/worker/v1/poll", json={}, headers=headers).get_json()["settings"]
    assert sent == {"encoder": "vaapi", "max_copy_mb_s": 40.0, "log_level": "DEBUG", "slots": 3}               # only what was chosen
    for bad in ({"encoder": "tpu"}, {"gpu_slots": 0}, {"update_window": "tonight"}, {"scan_threads": "x"}):
        with v2.connection() as conn, pytest.raises(workers.WorkerError):
            workers.update(conn, wid, settings=bad)
    with v2.connection() as conn:
        workers.update(conn, wid, settings={"encoder": "", "update_window": ""})              # blank = back to the default
        assert "encoder" not in workers.get(conn, wid)["settings"]


def test_host_requests_wait_for_idle_and_are_handed_over_once(client, v2):
    wid, headers = authed(v2, "w3")
    with v2.connection() as conn:
        jobs.create(conn, "library", title="A job")
        jobs.claim(conn, workers.get(conn, wid), 1)
        workers.request_host(conn, wid, "update")
    busy = client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()
    assert busy["request"] is None and busy["idle"] is False
    with v2.connection() as conn:
        conn.execute("UPDATE job SET status = 'done' WHERE worker_id = ?", (wid,))
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["request"] == "update"
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["request"] is None
    with v2.connection() as conn:
        workers.request_host(conn, wid, "restart")
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["request"] == "restart"       # a restart does not wait
    with v2.connection() as conn:
        assert workers.host_seen(conn, wid)


def test_the_workers_page_buttons_and_settings(client, v2):
    wid, headers = authed(v2, "w4")
    page = client.get("/workers").get_data(as_text=True)
    assert 'data-worker-action="host_update"' in page and 'data-worker-action="host_restart"' in page and "setGpuSlots" in page
    said = client.post(f"/api/workers/{wid}/host_update", json={}).get_json()
    assert said["ok"] and "starts within a minute" in said["message"] and "has not checked in" in said["message"]
    assert "Update queued" in client.get("/workers").get_data(as_text=True)
    assert client.post(f"/api/workers/{wid}/update", json={"settings": {"encoder": "tpu"}}).get_json()["ok"] is False
    client.post("/api/worker/v1/update/report", headers=headers, json={"status": "current", "version": "1.4.0", "commit": "abc"})
    assert "already up to date" in client.get("/workers").get_data(as_text=True)


def test_a_failing_fetch_is_reported_once_and_cleared_when_it_works(box):
    good = subprocess.run(["git", "-C", str(box.clone), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(box.clone), "remote", "set-url", "origin", "/nonexistent/repo"], check=True)
    for _ in range(3):
        box.run()
    calls = box.run()[2]
    reports = reports_of(calls)
    assert len(reports) == 1 and '"status":"problem"' in reports[0] and "fetch failed" in reports[0]       # told once, not every minute
    subprocess.run(["git", "-C", str(box.clone), "remote", "set-url", "origin", good], check=True)
    calls = box.run()[2]
    assert '"status":"current"' in calls and not (box.state / "problem").exists()


def test_a_problem_report_is_shown_with_its_log_and_told_once(client, v2, push):
    wid, _ = authed(v2, "worker-1")
    with v2.connection() as conn:
        admin(conn)
        w = {"id": wid}
        first = workers.report_update(conn, w["id"], {"status": "problem", "error": "git fetch failed: Permission denied", "log": "line 1\nline 2"})
        workers.report_update(conn, w["id"], {"status": "problem", "error": "git fetch failed: Permission denied"})
        assert first["log"] == "line 1\nline 2"
        shown = fleet.describe_update(workers.last_update(conn, w["id"]))
        assert shown["ok"] is False and "problem" in shown["text"] and "Permission denied" in shown["error"]
        assert len(notes(conn, "worker_update_failed")) == 1


def test_the_logs_button_asks_the_helper_for_its_own_log_once_and_the_log_is_kept(client, v2):
    wid, headers = authed(v2)
    with v2.connection() as conn:
        workers.request_log(conn, wid)
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["want_log"] is True
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["want_log"] is False        # asked once
    assert client.post("/api/worker/v1/update/log", json={"log": "Last check for new code: 19:12"}, headers=headers).status_code == 200
    with v2.connection() as conn:
        assert "Last check" in workers.updater_log(conn, wid)["content"]


def test_the_helper_uploads_its_log_when_asked():
    text = AUTO.read_text()
    assert 'jget want_log' in text and "/update/log" in text and "Last check for new code" in text


def test_the_quick_setup_says_where_to_clone_and_a_temporary_folder_is_refused():
    from synchub import storage
    quick = storage.quick_setup("http://hub:5001", "sw_x")
    assert "git clone" in quick and "~/letterboxd_scraper" in quick and "cd ~/letterboxd_scraper" in quick
    assert "a temporary folder" in (AUTO.parent / "setup-worker.sh").read_text()


# ---------------------------------------------------------------- updates and benchmarks on the Jobs page
def _tasks(v2):
    with v2.connection() as conn:
        return [(t["kind"], t["status"], t["source"], t["message"]) for t in reversed(workers.recent_tasks(conn))]


def test_an_update_from_the_button_is_followed_from_queued_to_done(client, v2):
    wid, headers = authed(v2, "t1")
    with v2.connection() as conn:
        workers.request_host(conn, wid, "update", by="Greg")
        workers.request_host(conn, wid, "update", by="Greg")                       # pressing twice keeps one task
    assert [t[:3] for t in _tasks(v2)] == [("update", "queued", "manual")]
    client.post("/api/worker/v1/update/check", json={}, headers=headers)
    assert _tasks(v2)[0][1] == "running"
    client.post("/api/worker/v1/update/report", json={"status": "started", "commit": "abc1234"}, headers=headers)
    client.post("/api/worker/v1/update/report", json={"status": "ok", "version": "1.3.0", "previous": "1.2.0", "commit": "abc1234", "log": "Updated 1.2.0 -> 1.3.0"}, headers=headers)
    assert _tasks(v2) == [("update", "done", "manual", "Updated 1.2.0 to 1.3.0 (abc1234)")]
    fragment = client.get("/jobs/fragment").get_data(as_text=True)
    assert "Update worker t1" in fragment and "Updated 1.2.0 to 1.3.0" in fragment and "Completed" in fragment
    page = client.get("/api/worker-tasks/1").get_json()["task"]
    assert page["log"] == "Updated 1.2.0 -> 1.3.0" and page["requested_by"] == "Greg"


def test_a_restart_and_a_relay_setup_are_rows_on_the_jobs_page_too(client, v2):
    wid, headers = authed(v2, "t3")
    with v2.connection() as conn:
        workers.request_host(conn, wid, "restart", by="Greg")
    assert [t[:3] for t in _tasks(v2)] == [("restart", "queued", "manual")]
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["request"] == "restart"
    assert [t[:2] for t in _tasks(v2)] == [("restart", "done")]
    with v2.connection() as conn:
        workers.request_host(conn, wid, "relay", by="Greg")
    client.post("/api/worker/v1/update/check", json={}, headers=headers)
    assert [t[:2] for t in _tasks(v2)] == [("restart", "done"), ("relay", "done")]
    fragment = client.get("/jobs/fragment").get_data(as_text=True)
    assert "Restart worker t3" in fragment and "Set up camera relay on t3" in fragment


def test_a_busy_worker_shows_why_the_update_waits_and_an_automatic_update_makes_its_own_task(client, v2):
    wid, headers = authed(v2, "t2")
    with v2.connection() as conn:
        jobs.create(conn, "library", title="A job")
        jobs.claim(conn, workers.get(conn, wid), 1)
        workers.request_host(conn, wid, "update")
    client.post("/api/worker/v1/update/check", json={}, headers=headers)
    assert "finish 1 job" in _tasks(v2)[0][3] and _tasks(v2)[0][1] == "queued"
    # the hourly check (nobody pressed a button): started, then failed
    client.post("/api/worker/v1/update/report", json={"status": "started", "commit": "d"}, headers=headers)
    client.post("/api/worker/v1/update/report", json={"status": "failed", "commit": "d", "error": "build broke", "restored": "c (1.2.0)"}, headers=headers)
    assert [t[:2] for t in _tasks(v2)] == [("update", "failed")]
    with v2.connection() as conn:
        # a check that finds nothing new and an unexplained problem leave no task behind
        before = len(workers.recent_tasks(conn))
        workers.report_update(conn, wid, {"status": "current", "commit": "x"})
        workers.report_update(conn, wid, {"status": "problem", "error": "git fetch failed"})
        assert len(workers.recent_tasks(conn)) == before
        workers.report_update(conn, wid, {"status": "ok", "version": "1.4.0", "commit": "e"})              # an automatic update nobody started is still recorded
        assert workers.recent_tasks(conn)[0]["source"] == "auto"


def test_a_requested_update_or_benchmark_nobody_answers_times_out_and_a_queued_one_can_be_cancelled(v2):
    with v2.connection() as conn:
        wid, _ = workers.create(conn, "t3")
        conn.execute("UPDATE worker SET capabilities = ? WHERE id = ?", ('{"features": ["benchmark"]}', wid))
        workers.request_host(conn, wid, "update")
        workers.request_benchmark(conn, wid)
        first = workers.recent_tasks(conn)[1]["id"]
        workers.cancel_task(conn, first)
        assert workers.get(conn, wid)["host_request"] is None
        with pytest.raises(workers.WorkerError):
            workers.cancel_task(conn, first)
        later = "2099-01-01 00:00:00"
        tasks = workers.recent_tasks(conn, now=later)
        assert [t["status"] for t in tasks] == ["failed", "cancelled"] and "never sent a benchmark report" in tasks[0]["error"]
        assert workers.get(conn, wid)["benchmark_requested"] == 0


def test_a_benchmark_becomes_running_when_the_worker_is_told_and_done_with_its_results(client, v2):
    import json as _json
    from .test_phase_profiles import REPORT
    wid, headers = authed(v2, "t4")
    with v2.connection() as conn:
        conn.execute("UPDATE worker SET capabilities = ? WHERE id = ?", (_json.dumps({"features": ["benchmark"]}), wid))
        workers.request_benchmark(conn, wid, by="Greg")
    assert client.post("/api/worker/v1/poll", json={}, headers=headers).get_json()["benchmark"] is True
    assert _tasks(v2)[0][:2] == ("benchmark", "running")
    client.post("/api/worker/v1/benchmark", json={"report": REPORT}, headers=headers)
    assert _tasks(v2)[0][1] == "done"
    with v2.connection() as conn:
        assert "CPU (libx265): 1.2x, VMAF 96.1" in workers.get_task(conn, workers.recent_tasks(conn)[0]["id"])["log"]


# ---------------------------------------------------------------- the AI quality test layer: Workers page setting -> rebuild
def test_the_ai_setting_rebuilds_an_idle_worker_with_the_layer_and_tells_synchub(box):
    code, out, calls = box.run(SHIM_ANSWER=answer(ai_quality=True))
    assert code == 0 and "Rebuilding the worker with the AI quality test layer" in out
    assert "--no-pull --ai-quality on" in calls
    sent = [c for c in calls.splitlines() if "update/report" in c]
    assert '"status":"started"' in sent[0] and "AI quality test layer" in sent[0] and '"status":"ok"' in sent[-1] and "now on" in sent[-1]
    again = box.run(SHIM_ANSWER=answer(ai_quality=True))[2]                              # already built that way: nothing more
    assert again.count("--ai-quality on") == 1
    off = box.run(SHIM_ANSWER=answer(ai_quality=False))[2]
    assert "--ai-quality off" in off


def test_the_ai_rebuild_waits_for_an_idle_worker_and_ignores_a_silent_synchub(box):
    code, out, calls = box.run(SHIM_ANSWER=answer(ai_quality=True, idle=False))
    assert "AI quality test layer is waiting" in out and "--ai-quality" not in calls
    assert "--ai-quality" not in box.run(SHIM_ANSWER=answer())[2]                      # SyncHub did not mention it: leave the image alone


def bash_update(tmp_path, args, build_ok=True):
    repo = tmp_path / "repo"
    if not repo.exists():
        (repo / "agent").mkdir(parents=True)
        sh("git init -q", repo)
        for f in ("Dockerfile", "synchub_agent.py", "media_analysis.py"):
            (repo / "agent" / f).write_text("x\n")
        (repo / "agent" / "update-worker.sh").write_text(UPDATE.read_text())
        sh("git add -A && git -c user.name=t -c user.email=t@t commit -qm x", repo)
    worker = tmp_path / "worker"
    worker.mkdir(exist_ok=True)
    (worker / "docker-compose.yml").write_text("services: {}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    build = 'echo built' if build_ok else 'echo "build failed" >&2; exit 1'
    docker = ('case "$*" in\n  "compose config") echo "    context: ' + str(repo / "agent") + '" ;;\n  "compose up -d --build") ' + build + ' ;;\n'
              '  "compose ps -q --status running") echo abc123 ;;\n  "compose ps -q") echo abc123 ;;\n  inspect*) echo healthy ;;\n  *) ;;\nesac')
    for name, body in (("docker", docker), ("mount", "echo x /mnt/tower/ x")):
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    done = subprocess.run(["bash", str(repo / "agent" / "update-worker.sh"), "--dir", str(worker), "--no-pull", *args], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return done, repo / "agent" / "AI_QUALITY"


def test_update_worker_switches_the_ai_layer_and_reports_its_state(tmp_path):
    status = lambda: bash_update(tmp_path, ["--ai-status"])[0].stdout.strip().splitlines()[-1]
    assert status() == "off"
    done, flag = bash_update(tmp_path, ["--ai-quality", "on"])
    assert done.returncode == 0 and flag.read_text().strip() == "on" and status() == "on"
    assert bash_update(tmp_path, [])[1].read_text().strip() == "on"                      # no option: the choice is kept through updates
    assert bash_update(tmp_path, ["--ai-quality", "off"])[1].read_text().strip() == "off"
    assert bash_update(tmp_path, ["--ai-quality", "maybe"])[0].returncode == 1


def test_a_failed_build_puts_the_ai_layer_choice_back(tmp_path):
    done, flag = bash_update(tmp_path, ["--ai-quality", "on"], build_ok=False)
    assert done.returncode == 1 and "back to 'off'" in done.stderr and flag.read_text().strip() == "off"


def test_the_workers_ai_setting_is_sent_to_the_pcs_helper(client, v2):
    wid, headers = authed(v2, "t-ai")
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["ai_quality"] is False
    with v2.connection() as conn:
        workers.update(conn, wid, settings={"ai_quality": True})
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["ai_quality"] is True
    note = {"status": "ok", "note": "AI quality test layer is now on"}
    with v2.connection() as conn:
        workers.report_update(conn, wid, {"status": "started", "note": "Rebuilding the worker with the AI quality test layer"})
        assert workers.recent_tasks(conn)[0]["message"].startswith("Rebuilding") and workers.last_update(conn, wid) is None
        workers.report_update(conn, wid, note)
        task = workers.recent_tasks(conn)[0]
        assert task["status"] == "done" and task["message"] == "AI quality test layer is now on" and workers.last_update(conn, wid) is None


# ---------------------------------------------------------------- the PC's drives and the work folder chosen in SyncHub
def shim(box, name, body):
    (box.state.parent / "worker" / "docker-compose.yml").write_text("services: {}\n")      # a worker folder has one; the helper leaves a folder without it alone
    f = box.state.parent / "bin" / name
    f.write_text(f"#!/bin/sh\n{body}\n")
    f.chmod(f.stat().st_mode | stat.S_IXUSR)


LSBLK = '''{"blockdevices": [
 {"name":"sda","path":"/dev/sda","type":"disk","size":250059350016,"fstype":null,"label":null,"mountpoint":null,"rota":false,"model":"Spare SSD","children":[
   {"name":"sda1","path":"/dev/sda1","type":"part","size":1073741824,"fstype":"vfat","mountpoint":"/boot/efi","rota":false,"model":null},
   {"name":"sda3","path":"/dev/sda3","type":"part","size":200000000000,"fstype":"LVM2_member","mountpoint":null,"rota":false,"model":null,"children":[
     {"name":"ubuntu--vg-ubuntu--lv","path":"/dev/mapper/ubuntu--vg-ubuntu--lv","type":"lvm","size":200000000000,"fstype":"ext4","mountpoint":"/","rota":false,"model":null}]}]},
 {"name":"sdb","path":"/dev/sdb","type":"disk","size":500107862016,"fstype":null,"label":null,"mountpoint":null,"rota":false,"model":"Old SSD","children":[
   {"name":"sdb1","path":"/dev/sdb1","type":"part","size":500000000000,"fstype":"ext4","label":"synchub-work","mountpoint":"/","rota":false,"model":null}]},
 {"name":"sdc","path":"/dev/sdc","type":"disk","size":1000204886016,"fstype":null,"label":null,"mountpoint":null,"rota":true,"model":"Blank HDD"}]}'''


def posted(calls, what):
    import json
    return [json.loads(c.split("--data ", 1)[1].rsplit(" http", 1)[0]) for c in calls.splitlines() if what in c and "--data " in c]


def test_the_helper_lists_the_drives_when_asked_and_only_then(box):
    shim(box, "lsblk", f"cat <<'JSON'\n{LSBLK}\nJSON")
    first = posted(box.run(SHIM_ANSWER=answer())[2], "update/disks")
    assert len(first) == 1 and "disks" not in first[0] and "smartmontools is not installed" in first[0]["smart_note"]      # the health check runs on its own once; no drive list
    assert posted(box.run(SHIM_ANSWER=answer())[2], "update/disks") == first                                           # and not again for six hours
    code, out, calls = box.run(SHIM_ANSWER=answer(want_disks=True))
    sent = [b for b in posted(calls, "update/disks") if "disks" in b]
    assert code == 0 and len(sent) == 1
    names = {d["name"]: d for d in sent[0]["disks"]}
    assert "sda1" not in names and "sda3" not in names                              # the EFI partition and the LVM member are not offered
    assert names["ubuntu--vg-ubuntu--lv"]["system"] is True and names["sdb1"]["ssd"] is True
    assert names["sdc"]["mounted"] is False and names["sdc"]["ssd"] is False and names["sdc"]["model"] == "Blank HDD"


def test_drive_health_is_read_with_smartctl_and_sent_with_the_sleep_check(box):
    shim(box, "lsblk", "printf '/dev/sdb disk Old SSD\\n/dev/loop0 loop\\n'")
    shim(box, "smartctl", """echo '{"model_name":"Old SSD","smart_status":{"passed":false},"temperature":{"current":41},"power_on_time":{"hours":4800},"ata_smart_attributes":{"table":[]}}'""")
    shim(box, "systemctl", "echo masked")
    body = posted(box.run(SHIM_ANSWER=answer(want_disks=True))[2], "update/disks")[-1]
    assert body["smart"] == [{"device": "/dev/sdb", "model": "Old SSD", "passed": False, "temp_c": 41, "wear_pct": None, "hours": 4800}]
    assert body["sleep"] is False                                                   # sleep.target is masked: the PC cannot suspend


def test_a_work_folder_on_the_system_disk_or_a_missing_one_is_refused(box, tmp_path):
    env = (box.state.parent / "worker" / ".env")
    before = env.read_text()
    (tmp_path / "mnt").mkdir()
    shim(box, "findmnt", 'echo /')
    code, out, calls = box.run(SHIM_ANSWER=answer(work_dir=str(tmp_path / "mnt")))
    assert code == 0 and "system disk" in calls and '"status":"failed"' in calls and env.read_text() == before
    code, out, calls = box.run(SHIM_ANSWER=answer(work_dir=str(tmp_path / "nothing" / "work")))
    assert "does not exist on the PC" in calls and env.read_text() == before


def test_a_busy_worker_keeps_its_folder_until_it_is_idle_then_switches(box, tmp_path):
    env = box.state.parent / "worker" / ".env"
    drive = tmp_path / "ssd"
    drive.mkdir()
    shim(box, "findmnt", f"echo {drive}")
    shim(box, "docker", 'echo "docker $*" >> "$SHIM_LOG"\ncase "$*" in "compose ps --status running --services") echo worker;; esac')
    want = str(drive / "synchub-work")
    (drive / "synchub-work").mkdir()
    code, out, calls = box.run(SHIM_ANSWER=answer(work_dir=want, idle=False, running=1))
    assert '"status":"waiting"' in calls and "WORK_DIR" not in env.read_text() and "docker compose up -d" not in calls
    code, out, calls = box.run(SHIM_ANSWER=answer(work_dir=want))
    assert f"WORK_DIR={want}" in env.read_text() and "docker compose up -d" in calls and '"status":"ok"' in calls
    assert "SYNCHUB_TOKEN=sw_secret" in env.read_text()                              # the rest of .env is untouched
    again = box.run(SHIM_ANSWER=answer(work_dir=want))[2]
    assert again.count("docker compose up -d") == 1                                   # already there: nothing more


def test_a_switch_that_does_not_start_the_worker_is_put_back(box, tmp_path):
    env = box.state.parent / "worker" / ".env"
    before = env.read_text()
    drive = tmp_path / "ssd"
    (drive / "synchub-work").mkdir(parents=True)
    shim(box, "findmnt", f"echo {drive}")
    shim(box, "docker", 'echo "docker $*" >> "$SHIM_LOG"\n[ "$*" = "compose up -d" ] && echo "boom" >&2 && exit 1\nexit 0')
    code, out, calls = box.run(SHIM_ANSWER=answer(work_dir=str(drive / "synchub-work")))
    assert env.read_text() == before and '"status":"failed"' in calls and "put back" in calls


def test_the_work_folder_and_drive_requests_reach_the_helper_and_the_page(client, v2):
    wid, headers = authed(v2)
    with v2.connection() as conn:
        workers.update(conn, wid, settings={"work_dir": "/mnt/ssd/synchub-work", "gpu_busy": "wait"})
    got = client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()
    assert got["work_dir"] == "/mnt/ssd/synchub-work" and got["want_disks"] is False
    with v2.connection() as conn:
        for bad in ({"work_dir": "/mnt/../etc"}, {"work_dir": "relative/path"}, {"gpu_busy": "never"}):
            with pytest.raises(workers.WorkerError):
                workers.update(conn, wid, settings=bad)
    assert client.post(f"/api/workers/{wid}/host_disks").get_json()["ok"] is True
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["want_disks"] is True
    assert client.post("/api/worker/v1/update/check", json={}, headers=headers).get_json()["want_disks"] is False      # asked once
    assert client.post("/api/worker/v1/update/disks", headers=headers, json={"work_dir": "/opt/synchub-worker/work", "disks": [
        {"device": "/dev/sdb1", "name": "sdb1", "mount": "/mnt/ssd", "fstype": "ext4", "mounted": True, "size": 500 * 10 ** 9, "free": 400 * 10 ** 9, "used": 100 * 10 ** 9, "ssd": True},
        {"device": "/dev/mapper/root", "name": "root", "mount": "/", "fstype": "ext4", "mounted": True, "size": 200 * 10 ** 9, "free": 100 * 10 ** 9, "system": True}]}).status_code == 200
    client.post("/api/worker/v1/update/workdir", headers=headers, json={"status": "failed", "message": "not a mounted drive"})
    assert client.post("/api/worker/v1/update/workdir", headers=headers, json={"status": "maybe"}).status_code == 400
    page = client.get(f"/api/workers/{wid}/disks").get_json()
    assert page["ok"] and [d["name"] for d in page["disks"]["disks"]] == ["sdb1", "root"] and page["report"]["status"] == "failed"
    assert [d["in_use"] for d in page["disks"]["disks"]] == [False, True]                    # /opt/synchub-worker/work is on the system disk
    html = client.get("/workers").get_data(as_text=True)
    assert 'id="setWorkDir"' in html and 'id="setGpuBusy"' in html and 'id="scanDisksBtn"' in html
