"""agent/mount-watchdog.sh: remounts a worker's shares after a network outage and gets the container to see them again.

It runs for real against temporary folders standing in for the mount points. Only the system tools (findmnt, mount, umount, docker, logger)
are replaced by small stand-ins that write down what they were asked; `ls`, `test`, `timeout` and `flock` are the real ones.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "agent" / "mount-watchdog.sh"
MARKER = ".synchub-root"

# $SHIM_STATE/net_<path with / as _> exists when a network file system is "mounted" there
SHIMS = {
    "logger": "true",
    # `ls` fails for a folder flagged stale (a stale handle or a hung share); umount clears the flag
    "ls": 'for a; do :; done; [ ! -f "$SHIM_STATE/stale_$(printf "%s" "$a" | tr / _)" ]',
    "findmnt": 'f="$SHIM_STATE/net_$(printf "%s" "$5" | tr / _)"; [ -f "$f" ] && echo cifs; true',
    "umount": 'echo "umount $*" >> "$SHIM_LOG"; for a; do :; done; k="$(printf "%s" "$a" | tr / _)"; rm -f "$SHIM_STATE/net_$k" "$SHIM_STATE/stale_$k"',
    "mount": 'echo "mount $*" >> "$SHIM_LOG"; mp="$2"\n'
             'if [ -n "${SHIM_FAIL_MOUNT:-}" ]; then echo "mount error(101): Network is unreachable" >&2; exit 32; fi\n'
             'touch "$SHIM_STATE/net_$(printf "%s" "$mp" | tr / _)"; touch "$mp/.synchub-root"',
    "docker": 'echo "docker $*" >> "$SHIM_LOG"\n'
              'case "$1 $2" in\n'
              '  "inspect -f") case "$3" in *Status*) echo "${SHIM_STATUS:-running}";; *Error*) echo "${SHIM_ERROR:-}";; esac ;;\n'
              '  "exec "*) [ -z "${SHIM_BLIND:-}" ] ;;\n'
              'esac',
}


@pytest.fixture
def box(tmp_path):
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    for name, body in SHIMS.items():
        shim = bin_dir / name
        shim.write_text(f"#!/bin/sh\n{body}\n")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    shares = {name: tmp_path / "mnt" / name for name in ("Media", "TFiles")}
    for path in shares.values():
        path.mkdir(parents=True)
    log = tmp_path / "shim.log"
    log.write_text("")

    def run(*args, **env_extra):
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SHIM_STATE": str(state), "SHIM_LOG": str(log),
               "SYNCHUB_WATCHDOG_CONF": str(tmp_path / "none.conf"), "SYNCHUB_MOUNT_PATHS": " ".join(str(p) for p in shares.values()),
               "SYNCHUB_WATCHDOG_LOCK": str(tmp_path / "lock"), "SYNCHUB_WATCHDOG_STAMP": str(tmp_path / "stamp"),
               "SYNCHUB_SETTLE_SECONDS": "2", **env_extra}
        done = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
        return done.returncode, done.stdout + done.stderr, log.read_text()

    def mounted(path, marker=True):
        (state / f"net_{str(path).replace('/', '_')}").touch()
        if marker:
            (path / MARKER).touch()

    run.shares, run.mounted, run.state = shares, mounted, state
    return run


def test_healthy_shares_are_left_alone(box):
    for path in box.shares.values():
        box.mounted(path)
    code, out, calls = box()
    assert code == 0
    assert "mount " not in calls and "umount" not in calls
    assert "docker restart" not in calls


def test_an_unmounted_folder_is_mounted(box):
    box.mounted(box.shares["Media"])
    code, out, calls = box()
    assert code == 0, out
    assert f"mount -- {box.shares['TFiles']}" in calls
    assert f"mount -- {box.shares['Media']}" not in calls
    assert (box.shares["TFiles"] / MARKER).exists()


def test_a_stale_mount_is_unmounted_then_mounted(box):
    media = box.shares["Media"]
    box.mounted(box.shares["TFiles"])
    box.mounted(media)
    (box.state / f"stale_{str(media).replace('/', '_')}").touch()
    code, out, calls = box()
    assert code == 0, out
    assert f"umount -f -l -- {media}" in calls and f"mount -- {media}" in calls
    assert f"umount -f -l -- {box.shares['TFiles']}" not in calls


def test_a_mounted_share_without_a_marker_is_never_touched_or_marked(box):
    box.mounted(box.shares["Media"], marker=False)
    box.mounted(box.shares["TFiles"])
    code, out, calls = box()
    assert code == 1
    assert "has no .synchub-root file: not touching it" in out
    assert "mount" not in calls
    assert not (box.shares["Media"] / MARKER).exists()


def test_a_failing_mount_is_reported_and_the_container_is_left_alone(box):
    code, out, calls = box(SHIM_FAIL_MOUNT="1")
    assert code == 1
    assert "Network is unreachable" in out
    assert "docker" not in calls


def test_a_container_that_cannot_see_the_markers_is_restarted_once(box):
    for path in box.shares.values():
        box.mounted(path)
    code, out, calls = box(SHIM_BLIND="1")
    assert code == 0 and calls.count("docker restart synchub-worker") == 1
    code, out, calls = box(SHIM_BLIND="1")
    assert calls.count("docker restart synchub-worker") == 1, "a second restart within ten minutes"
    assert "not again" in out


def test_a_container_that_failed_to_start_is_started(box):
    for path in box.shares.values():
        box.mounted(path)
    code, out, calls = box(SHIM_STATUS="exited", SHIM_ERROR="bind source path does not exist")
    assert "docker start synchub-worker" in calls
    code, out, calls = box(SHIM_STATUS="exited")
    assert calls.count("docker start") == 1, "a container stopped on purpose must stay stopped"


def test_install_dry_run_writes_nothing(box, tmp_path):
    code, out, _ = box("--install", "--dry-run", SYNCHUB_UNIT_DIR=str(tmp_path / "units"), SYNCHUB_WATCHDOG_BIN=str(tmp_path / "wd"))
    assert code == 0
    assert "OnUnitActiveSec=1min" in out and "SYNCHUB_MOUNT_PATHS=" in out
    assert not (tmp_path / "units").exists() and not (tmp_path / "wd").exists()
