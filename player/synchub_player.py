#!/usr/bin/env python3
"""SyncHub player: the program on the small PC behind a TV.

SyncHub is the master. This program only calls out to SyncHub (nothing is opened on this PC): it registers, then holds a request open and takes the
commands SyncHub has queued (ping, update, restart; playing media comes in a later step). Standard library only.

    synchub_player.py --config /etc/synchub-player.ini      run
    synchub_player.py --config /etc/synchub-player.ini --check   show what it sees and whether SyncHub accepts the token, then stop
"""
import argparse
import configparser
import json
import logging
import logging.handlers
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "1.4.0"      # kept equal to SyncHub's version (synchub/__init__.py); see docs/RELEASING.md
PROTOCOL = 1
API = "/api/player/v1"
LOG_NAME = "player.log"
MAX_LOG_BYTES = 900_000
START = time.time()
EXIT_RESTART = 0           # systemd restarts the program (Restart=always) after any exit


class ConfigError(Exception):
    pass


class Unreachable(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- configuration
def load_config(path=None, env=None):
    """url, token, name and the state folder, from the ini file with the environment (SYNCHUB_URL, SYNCHUB_TOKEN, SYNCHUB_NAME, SYNCHUB_STATE_DIR) on top."""
    env = os.environ if env is None else env
    parser = configparser.ConfigParser()
    if path:
        if not Path(path).is_file():
            raise ConfigError(f"The config file {path} does not exist.")
        parser.read(path)
    pick = lambda key, var, default="": (env.get(var) or parser.get("synchub", key, fallback=default)).strip()
    cfg = {"url": pick("url", "SYNCHUB_URL").rstrip("/"), "token": pick("token", "SYNCHUB_TOKEN"), "name": pick("name", "SYNCHUB_NAME", socket.gethostname()),
           "state_dir": Path(pick("state_dir", "SYNCHUB_STATE_DIR", "/var/lib/synchub-player")), "github_token": pick("github_token", "GITHUB_TOKEN")}
    if not cfg["url"].startswith(("http://", "https://")):
        raise ConfigError("The SyncHub address (url) is missing or does not start with http:// or https://.")
    if not cfg["token"]:
        raise ConfigError("The token is missing.")
    return cfg


# ---------------------------------------------------------------- what this PC is
def run_text(command, timeout=8):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return (done.stdout or done.stderr or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def mpv_version():
    if not shutil.which("mpv"):
        return ""
    first = run_text(["mpv", "--version"]).splitlines()[:1]
    return first[0].replace("mpv ", "", 1).split(" ")[0] if first else ""


VENDORS = {"0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA"}


def graphics(drm=Path("/sys/class/drm")):
    """The render devices (the GPUs mpv can decode on) with their vendor."""
    out = []
    try:
        for node in sorted(drm.glob("renderD*")):
            vendor = (node / "device" / "vendor").read_text().strip() if (node / "device" / "vendor").exists() else ""
            driver = os.path.basename(os.path.realpath(node / "device" / "driver")) if (node / "device" / "driver").exists() else ""
            out.append(f"{VENDORS.get(vendor, 'Graphics')} ({node.name}{', ' + driver if driver else ''})")
    except OSError:
        pass
    return out


def screens(drm=Path("/sys/class/drm")):
    """The connectors with a screen plugged in, such as HDMI-A-1. Telling SyncHub this proves the HDMI cable to the TV."""
    out = []
    try:
        for node in sorted(drm.glob("card*-*")):
            if (node / "status").read_text().strip() == "connected":
                out.append(node.name.split("-", 1)[1])
    except OSError:
        pass
    return out


def capabilities():
    return {"mpv": mpv_version(), "graphics": graphics(), "screens": screens(), "python": platform.python_version(), "git": bool(shutil.which("git"))}


def uptime_text(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}d {hours}h" if days else f"{hours}h {rest // 60}m"


def status():
    """The few figures that change, sent with every poll."""
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    machine = 0
    try:
        machine = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    free_mb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                free_mb = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return {"load": load, "free_memory_mb": free_mb, "uptime_text": uptime_text(machine) if machine else "", "program_seconds": int(time.time() - START), "screens": screens()}


# ---------------------------------------------------------------- talking to SyncHub
class Api:
    def __init__(self, cfg):
        self.base = cfg["url"] + API
        self.token = cfg["token"]

    def post(self, path, body=None, timeout=30):
        request = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "User-Agent": f"synchub-player/{__version__}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode()).get("message") or exc.reason
            except (ValueError, OSError):
                message = exc.reason
            raise ApiError(exc.code, str(message))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise Unreachable(str(exc))


def build_logger(state_dir):
    log = logging.getLogger("synchub-player")
    log.setLevel(logging.INFO)
    if not log.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        log.addHandler(console)
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(state_dir / LOG_NAME, maxBytes=1_000_000, backupCount=2)
            file_handler.setFormatter(formatter)
            log.addHandler(file_handler)
        except OSError as exc:
            log.warning("Cannot write the log file in %s: %s", state_dir, exc)
    return log


def read_log_tail(state_dir, limit=MAX_LOG_BYTES):
    """The end of the log (older rotated files first), cut to `limit` characters. Returns (text, truncated)."""
    parts = []
    for name in (f"{LOG_NAME}.2", f"{LOG_NAME}.1", LOG_NAME):
        try:
            parts.append((state_dir / name).read_text(errors="replace"))
        except OSError:
            continue
    text = "".join(parts)
    return (text[-limit:], True) if len(text) > limit else (text, False)


# ---------------------------------------------------------------- commands
def checkout_dir():
    return Path(__file__).resolve().parent.parent


GITHUB_TOKEN = ""          # set from the config for a private repository; it travels in the environment of each git call only


def git_env():
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if GITHUB_TOKEN:
        import base64
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0="Authorization: Basic " + base64.b64encode(f"x-access-token:{GITHUB_TOKEN}".encode()).decode())
    return env


def git(*args, cwd=None):
    done = subprocess.run(["git", *args], cwd=cwd or checkout_dir(), capture_output=True, text=True, timeout=180, env=git_env())
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "git failed").strip()[-400:])
    return done.stdout.strip()


def do_update():
    """Fetches the newest code from GitHub and fast-forwards this checkout. Returns (changed, text). Never discards local work: a diverged checkout fails."""
    if not shutil.which("git"):
        raise RuntimeError("git is not installed on this PC.")
    before = git("rev-parse", "--short", "HEAD")
    git("fetch", "--quiet", "origin")
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if git("rev-parse", "HEAD") == git("rev-parse", upstream):
        return False, f"already up to date ({before})"
    git("merge", "--ff-only", upstream)
    return True, f"updated {before} -> {git('rev-parse', '--short', 'HEAD')}"


def handle(command, api, log):
    """Does one command and answers SyncHub. Returns True when the program must exit so systemd starts it again."""
    kind, command_id = command.get("kind"), command.get("id")
    answer = lambda ok, result: api.post(f"/commands/{command_id}/answer", {"ok": ok, "result": result})
    log.info("command %s: %s", command_id, kind)
    if kind == "ping":
        answer(True, f"pong from {socket.gethostname()}, version {__version__}")
        return False
    if kind == "restart":
        answer(True, "restarting")
        return True
    if kind == "update":
        try:
            changed, text = do_update()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            log.error("update failed: %s", exc)
            answer(False, f"update failed: {exc}")
            return False
        log.info(text)
        answer(True, text + ("; restarting" if changed else ""))
        return changed
    answer(False, f"this player does not know the command '{kind}'")
    return False


# ---------------------------------------------------------------- the loop
def serve(cfg, log, api=None, sleep=time.sleep, rounds=None):
    """Registers (retrying until SyncHub answers), then polls. Returns when a command asked for a restart (or after `rounds` polls, for tests)."""
    api = api or Api(cfg)
    delay = 5
    while True:
        try:
            info = api.post("/register", {"protocol": PROTOCOL, "version": __version__, "os": os_name(), "hostname": socket.gethostname(), "capabilities": capabilities()})
            log.info("registered with SyncHub as %s", info.get("name"))
            break
        except ApiError as exc:
            log.error("SyncHub refused the registration (%s): %s", exc.status, exc)
        except Unreachable as exc:
            log.warning("cannot reach SyncHub: %s", exc)
        sleep(delay)
        delay = min(delay * 2, 60)
    wait, pause, delay, done = info.get("wait_seconds", 20), info.get("poll_seconds", 2), 2, 0
    while rounds is None or done < rounds:
        done += 1
        try:
            answer = api.post("/poll", {"wait": wait, "status": status()}, timeout=wait + 20)
            delay = 2
        except ApiError as exc:
            log.error("poll refused (%s): %s", exc.status, exc)
            sleep(60 if exc.status in (401, 403, 426) else delay)
            continue
        except Unreachable as exc:
            log.warning("cannot reach SyncHub: %s", exc)
            sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if answer.get("send_log"):
            text, truncated = read_log_tail(cfg["state_dir"])
            try:
                api.post("/log", {"log": text, "truncated": truncated})
            except (ApiError, Unreachable) as exc:
                log.warning("could not send the log: %s", exc)
        if answer.get("command") and handle(answer["command"], api, log):
            return "restart"
        wait, pause = answer.get("wait_seconds", wait), answer.get("poll_seconds", pause)
        if not answer.get("command"):
            sleep(0 if wait else pause)
    return "stopped"


def main(argv=None):
    parser = argparse.ArgumentParser(description="SyncHub player")
    parser.add_argument("--config", default=os.environ.get("SYNCHUB_PLAYER_CONFIG"), help="the ini file with url, token and name")
    parser.add_argument("--check", action="store_true", help="show what this PC sees and whether SyncHub accepts the token, then stop")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config problem: {exc}", file=sys.stderr)
        return 2
    global GITHUB_TOKEN
    GITHUB_TOKEN = cfg["github_token"]
    log = build_logger(cfg["state_dir"])
    if args.check:
        caps = capabilities()
        print(f"player {cfg['name']} version {__version__} on {os_name()}")
        print(f"  mpv: {caps['mpv'] or 'NOT FOUND (sudo apt install mpv)'}")
        print(f"  graphics: {', '.join(caps['graphics']) or 'none found (no /dev/dri render device)'}")
        print(f"  screens: {', '.join(caps['screens']) or 'none connected (check the HDMI cable and that the TV is on)'}")
        try:
            Api(cfg).post("/register", {"protocol": PROTOCOL, "version": __version__, "os": os_name(), "hostname": socket.gethostname(), "capabilities": caps})
            print("  SyncHub: token accepted")
            return 0
        except ApiError as exc:
            print(f"  SyncHub: refused ({exc.status}) {exc}")
        except Unreachable as exc:
            print(f"  SyncHub: cannot be reached ({exc})")
        return 1
    log.info("synchub-player %s starting, SyncHub at %s", __version__, cfg["url"])
    serve(cfg, log)
    log.info("exiting so that the service manager starts the program again")
    return EXIT_RESTART


if __name__ == "__main__":
    sys.exit(main())
