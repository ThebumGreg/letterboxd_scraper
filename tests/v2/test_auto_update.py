"""Automatic worker updates: the SyncHub side (idle check, report, notification) and agent/auto-update.sh against real git repositories."""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from synchub import jobs, workers
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
            'case "$*" in *update/check*) if [ -n "${SHIM_ANSWER:-}" ]; then echo "$SHIM_ANSWER"; else echo "{\\"ok\\":true,\\"idle\\":${SHIM_IDLE:-true}}"; fi;; *) echo "{\\"ok\\":true}";; esac')
    # the updater: succeeds, or fails with a build error while $SHIM_FAIL_UPDATE exists; writes down which version it was asked to deploy
    updater = ('echo "update $* $(sed -n "s/.*\\"\\(.*\\)\\".*/\\1/p" "$SYNCHUB_REPO/agent/synchub_agent.py")" >> "$SHIM_LOG"\n'
               'if [ -f "$SHIM_FAIL_UPDATE" ] && grep -q 2.0.0 "$SYNCHUB_REPO/agent/synchub_agent.py"; then echo "ERROR: build failed: no space left"; exit 1; fi')
    for name, body in (("curl", curl), ("updater", updater), ("logger", "true"), ("docker", 'echo "docker $*" >> "$SHIM_LOG"')):
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    fail = tmp_path / "fail"
    maker = tmp_path / "maker"                                  # where new releases are made and pushed from
    sh(f"git clone -q {origin} {maker}", tmp_path)

    def run(*args, **extra):
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SHIM_LOG": str(calls), "SHIM_FAIL_UPDATE": str(fail), "SYNCHUB_UPDATE_CONF": str(tmp_path / "none"),
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


def test_an_idle_worker_is_updated_and_the_result_reported(box):
    publish(box, "1.1.0")
    code, out, calls = box.run()
    assert code == 0 and "Updated 1.0.0 -> 1.1.0" in out
    assert "update --dir" in calls and "--no-pull 1.1.0" in calls and head_version(box) == "1.1.0"
    report = [c for c in calls.splitlines() if "update/report" in c][0]
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
    assert not (box.state / "pending.json").exists() and calls.count("update/report") == 2 and '"status":"ok"' in [c for c in calls.splitlines() if "update/report" in c][-1]


def test_a_failed_update_puts_the_old_code_back_and_is_not_tried_again(box):
    publish(box, "2.0.0")
    box.fail.write_text("1")
    code, out, calls = box.run()
    assert code == 1 and "FAILED" in out and head_version(box) == "1.0.0"
    updates = [c for c in calls.splitlines() if c.startswith("update ")]
    assert [u.rsplit(" ", 1)[1] for u in updates] == ["2.0.0", "1.0.0"]              # the new code was tried, then the old code was deployed again
    report = [c for c in calls.splitlines() if "update/report" in c][0]
    assert '"status":"failed"' in report and "no space left" in report and '"restored"' in report
    for _ in range(3):
        again = box.run()
        assert again[0] == 0 and again[2].count("update ") == 2 and again[2].count("update/report") == 1        # nothing more, nothing more reported
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
