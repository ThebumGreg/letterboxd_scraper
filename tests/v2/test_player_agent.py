"""player/synchub_player.py (the program on the TV player PC) and player/setup-player.sh, run against a real SyncHub on a local port."""
import importlib.util
import os
import subprocess
import threading
from pathlib import Path

import pytest

from synchub import db, players

ROOT = Path(__file__).resolve().parents[2]
PLAYER = ROOT / "player"


def load_module():
    spec = importlib.util.spec_from_file_location("synchub_player_under_test", PLAYER / "synchub_player.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def agent():
    return load_module()


@pytest.fixture
def server(app):
    from werkzeug.serving import make_server
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(5)


@pytest.fixture
def enrolled(v2, server, tmp_path):
    with db.connection() as conn:
        player_id, token = players.create(conn, "den", "Den TV")
    return {"id": player_id, "token": token, "cfg": {"url": server, "token": token, "name": "den", "state_dir": tmp_path / "state", "github_token": ""}}


# ---------------------------------------------------------------- the files
def test_the_player_version_matches_synchub(agent):
    import synchub
    assert agent.__version__ == synchub.__version__


def test_the_scripts_are_valid_and_executable():
    assert subprocess.run(["bash", "-n", str(PLAYER / "setup-player.sh")]).returncode == 0
    assert os.access(PLAYER / "setup-player.sh", os.X_OK) and os.access(PLAYER / "synchub_player.py", os.X_OK)
    unit = (PLAYER / "synchub-player.service").read_text()
    assert "Restart=always" in unit and "synchub_player.py" in unit and "video render input" in unit


# ---------------------------------------------------------------- configuration and this PC
def test_config_comes_from_the_file_with_the_environment_on_top(agent, tmp_path):
    ini = tmp_path / "p.ini"
    ini.write_text("[synchub]\nurl = http://synchub:5001/\ntoken = sp_abc\nname = den\n")
    cfg = agent.load_config(str(ini), env={})
    assert cfg["url"] == "http://synchub:5001" and cfg["token"] == "sp_abc" and cfg["name"] == "den"
    assert agent.load_config(str(ini), env={"SYNCHUB_NAME": "other"})["name"] == "other"


@pytest.mark.parametrize("text,message", [("[synchub]\ntoken = x\n", "address"), ("[synchub]\nurl = http://a\n", "token"), ("[synchub]\nurl = ftp://a\ntoken = x\n", "address")])
def test_a_bad_config_says_what_is_missing(agent, tmp_path, text, message):
    ini = tmp_path / "p.ini"
    ini.write_text(text)
    with pytest.raises(agent.ConfigError, match=message):
        agent.load_config(str(ini), env={})
    with pytest.raises(agent.ConfigError, match="does not exist"):
        agent.load_config(str(tmp_path / "missing.ini"), env={})


def test_graphics_and_screens_are_read_from_sysfs(agent, tmp_path):
    drm = tmp_path / "drm"
    (drm / "renderD128" / "device").mkdir(parents=True)
    (drm / "renderD128" / "device" / "vendor").write_text("0x8086\n")
    (drm / "card0-HDMI-A-1").mkdir()
    (drm / "card0-HDMI-A-1" / "status").write_text("connected\n")
    (drm / "card0-DP-1").mkdir()
    (drm / "card0-DP-1" / "status").write_text("disconnected\n")
    assert agent.graphics(drm) == ["Intel (renderD128)"] and agent.screens(drm) == ["HDMI-A-1"]
    assert agent.graphics(tmp_path / "none") == [] and agent.screens(tmp_path / "none") == []


def test_status_has_the_figures_the_page_shows(agent):
    got = agent.status()
    assert {"load", "free_memory_mb", "uptime_text", "program_seconds", "screens"} <= set(got)


# ---------------------------------------------------------------- talking to a real SyncHub
def test_it_registers_polls_and_answers_a_ping_and_a_log_request(agent, enrolled):
    log = agent.build_logger(enrolled["cfg"]["state_dir"])
    log.info("a line for the log")
    with db.connection() as conn:
        players.send(conn, enrolled["id"], "ping")
        players.request_log(conn, enrolled["id"])
    agent.serve(enrolled["cfg"], log, rounds=2, sleep=lambda s: None)
    with db.connection() as conn:
        got = players.get(conn, enrolled["id"])
        assert got["version"] == agent.__version__ and got["online"] and "load" in got["status"]
        assert players.recent_commands(conn, enrolled["id"])[0]["status"] == "done" and "pong" in players.recent_commands(conn, enrolled["id"])[0]["result"]
        assert "a line for the log" in players.stored_log(conn, enrolled["id"])["content"]


def test_a_restart_command_ends_the_loop_after_answering(agent, enrolled):
    with db.connection() as conn:
        players.send(conn, enrolled["id"], "restart")
    assert agent.serve(enrolled["cfg"], agent.build_logger(enrolled["cfg"]["state_dir"]), sleep=lambda s: None) == "restart"
    with db.connection() as conn:
        assert players.recent_commands(conn, enrolled["id"])[0]["status"] == "done"


def test_a_wrong_token_is_reported_not_crashed_on(agent, enrolled, capsys):
    bad = dict(enrolled["cfg"], token="sp_wrong")
    api = agent.Api(bad)
    with pytest.raises(agent.ApiError) as caught:
        api.post("/poll", {"wait": 0})
    assert caught.value.status == 401
    with pytest.raises(agent.Unreachable):
        agent.Api(dict(bad, url="http://127.0.0.1:1")).post("/poll", {"wait": 0}, timeout=2)


def test_check_reports_the_token_result(agent, enrolled, tmp_path, capsys):
    ini = tmp_path / "p.ini"
    ini.write_text(f"[synchub]\nurl = {enrolled['cfg']['url']}\ntoken = {enrolled['token']}\nname = den\nstate_dir = {tmp_path / 'st'}\n")
    assert agent.main(["--config", str(ini), "--check"]) == 0
    assert "token accepted" in capsys.readouterr().out
    ini.write_text(ini.read_text().replace(enrolled["token"], "sp_wrong"))
    assert agent.main(["--config", str(ini), "--check"]) == 1
    assert "refused (401)" in capsys.readouterr().out


# ---------------------------------------------------------------- updating from GitHub
def make_repos(tmp_path):
    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "a.txt").write_text("1")
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "one", cwd=origin)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    return origin, clone, git


def test_update_fast_forwards_and_says_when_nothing_is_new(agent, tmp_path, monkeypatch):
    origin, clone, git = make_repos(tmp_path)
    monkeypatch.setattr(agent, "checkout_dir", lambda: clone)
    assert agent.do_update() == (False, agent.do_update()[1]) and "up to date" in agent.do_update()[1]
    (origin / "a.txt").write_text("2")
    git("commit", "-q", "-am", "two", cwd=origin)
    changed, text = agent.do_update()
    assert changed and "updated" in text and (clone / "a.txt").read_text() == "2"


def test_update_never_discards_local_work(agent, tmp_path, monkeypatch):
    origin, clone, git = make_repos(tmp_path)
    monkeypatch.setattr(agent, "checkout_dir", lambda: clone)
    (clone / "b.txt").write_text("mine")
    git("add", ".", cwd=clone)
    git("commit", "-q", "-m", "local", cwd=clone)
    (origin / "a.txt").write_text("2")
    git("commit", "-q", "-am", "two", cwd=origin)
    with pytest.raises(RuntimeError):
        agent.do_update()
    assert (clone / "b.txt").read_text() == "mine"


def test_the_update_command_reports_failure_without_restarting(agent, enrolled, monkeypatch):
    def broken():
        raise RuntimeError("no network")
    monkeypatch.setattr(agent, "do_update", broken)
    with db.connection() as conn:
        players.send(conn, enrolled["id"], "update")
    assert agent.serve(enrolled["cfg"], agent.build_logger(enrolled["cfg"]["state_dir"]), rounds=1, sleep=lambda s: None) == "stopped"
    with db.connection() as conn:
        command = players.recent_commands(conn, enrolled["id"])[0]
        assert command["status"] == "failed" and "no network" in command["result"]


def test_a_successful_update_restarts_the_program(agent, enrolled, monkeypatch):
    monkeypatch.setattr(agent, "do_update", lambda: (True, "updated abc -> def"))
    with db.connection() as conn:
        players.send(conn, enrolled["id"], "update")
    assert agent.serve(enrolled["cfg"], agent.build_logger(enrolled["cfg"]["state_dir"]), sleep=lambda s: None) == "restart"


# ---------------------------------------------------------------- the installer (checks only: it cannot install packages here)
def run_setup(*args, **env):
    return subprocess.run(["bash", str(PLAYER / "setup-player.sh"), "--dry-run", *args], capture_output=True, text=True, env={**os.environ, "SYNCHUB_ALLOW_LOCALHOST": "1", **env}, timeout=60)


def test_setup_needs_an_address_and_a_token():
    assert "--url" in run_setup("--token", "sp_x").stderr
    assert "--token" in run_setup("--url", "http://synchub:5001").stderr
    assert "localhost" in run_setup("--url", "http://localhost:5001", "--token", "sp_x", SYNCHUB_ALLOW_LOCALHOST="").stderr


def test_setup_stops_on_a_wrong_token_and_goes_on_with_a_good_one(enrolled, server):
    bad = run_setup("--url", server, "--token", "sp_wrong", "--name", "den")
    assert bad.returncode == 1 and "refused the token" in bad.stderr
    good = run_setup("--url", server, "--token", enrolled["token"], "--name", "den")
    assert good.returncode == 0, good.stderr
    assert "the token works" in good.stdout and "[dry run] write" in good.stdout
    assert enrolled["token"] not in good.stdout + good.stderr                      # the token is not echoed


def test_setup_reports_an_unreachable_address():
    result = run_setup("--url", "http://127.0.0.1:1", "--token", "sp_x")
    assert result.returncode == 1 and "Cannot reach SyncHub" in result.stderr
