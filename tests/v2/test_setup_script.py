"""agent/setup-worker.sh: the one script that sets up a worker PC.

It runs for real here, against a real SyncHub (the Flask app on a local port), a real git repository and real files, with only the system tools it
would call as root (apt-get, docker, mount, systemctl...) replaced by small stand-ins that write down what they were asked. Paths such as /etc/fstab
are redirected into a temporary folder through the script's SYNCHUB_* variables.
"""
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from synchub import storage, workers

SCRIPT = Path(__file__).resolve().parents[2] / "agent" / "setup-worker.sh"
AGENT = SCRIPT.parent


# ---------------------------------------------------------------- the pieces
@pytest.fixture
def server(app):
    """SyncHub itself, listening on a local port. Yields its address."""
    from werkzeug.serving import make_server
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(5)


@pytest.fixture
def origin(tmp_path):
    """A git repository standing in for GitHub, holding the agent files."""
    repo = tmp_path / "origin"
    (repo / "agent").mkdir(parents=True)
    for name in ("synchub_agent.py", "media_analysis.py", "synchub-agent.service", "Dockerfile"):
        shutil.copy(AGENT / name, repo / "agent" / name)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for command in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        subprocess.run(["git", "-C", str(repo), *command], check=True, env=env, capture_output=True)
    return repo


SHIMS = {
    "apt-get": 'echo "apt-get $*" >> "$SHIM_LOG"',
    "docker": 'echo "docker $*" >> "$SHIM_LOG"',
    "systemctl": 'echo "systemctl $*" >> "$SHIM_LOG"',
    "update-ca-certificates": "true",
    "mount.cifs": "true",
    "mount.nfs": "true",
    "findmnt": "echo shared",
    # `mount /some/folder` succeeds (and from then on it is a mount point) unless the folder is listed in $SHIM_FAIL_MOUNT
    "mount": 'echo "mount $*" >> "$SHIM_LOG"; mp="$1"\n'
             'if [ -n "${SHIM_FAIL_MOUNT:-}" ] && printf "%s" "$SHIM_FAIL_MOUNT" | grep -qxF "$mp"; then echo "mount error(13): Permission denied" >&2; exit 32; fi\n'
             'touch "$SHIM_STATE/$(printf "%s" "$mp" | tr / _)"',
    "mountpoint": '[ -f "$SHIM_STATE/$(printf "%s" "$2" | tr / _)" ]',
}


class Setup:
    """A fresh 'worker PC' in a temporary folder, and a way to run the script on it."""

    def __init__(self, tmp_path, server, origin, token, workers_id):
        self.tmp, self.server, self.origin, self.token = tmp_path, server, origin, token
        self.root = tmp_path / "pc"
        self.bin, self.state = self.root / "bin", self.root / "mounted"
        self.fstab, self.cred, self.log = self.root / "fstab", self.root / "synchub-smb.cred", self.root / "calls.log"
        self.dir, self.repo = self.root / "worker", self.root / "code"
        for folder in (self.bin, self.state):
            folder.mkdir(parents=True)
        for name, body in SHIMS.items():
            path = self.bin / name
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.fstab.write_text("UUID=abc / ext4 defaults 0 1\n//nas/other /mnt/other cifs credentials=/root/x 0 0\n")
        self.fail_mount = ""

    def mount_point(self, name):
        return str(self.root / "mnt" / name)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(self, *args, token=None, extra_env=None, use_defaults=True):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "SHIM_LOG": str(self.log), "SHIM_STATE": str(self.state), "SHIM_FAIL_MOUNT": self.fail_mount,
               "SYNCHUB_FSTAB": str(self.fstab), "SYNCHUB_CRED_FILE": str(self.cred), "SYNCHUB_INI_FILE": str(self.root / "synchub-agent.ini"),
               "SYNCHUB_AGENT_DIR": str(self.root / "agent-install"), "SYNCHUB_SYSTEMD_DIR": str(self.root / "systemd"),
               "SYNCHUB_SETUP_ALLOW_NONROOT": "1", "SYNCHUB_SETUP_ALLOW_LOCALHOST": "1", "HOME": str(self.root), **(extra_env or {})}
        for name in ("SYNCHUB_URL", "SYNCHUB_TOKEN", "SMB_USER", "SMB_PASSWORD", "GITHUB_TOKEN"):
            env.pop(name, None)
        command = ["bash", str(SCRIPT), "-y"]
        if use_defaults:
            command += ["--url", self.server, "--token", token or self.token, "--dir", str(self.dir), "--repo", str(self.origin), "--repo-dir", str(self.repo),
                        "--smb-user", "unraid-user", "--smb-password", 'p@ss"w$rd,x']
        done = subprocess.run(command + list(args), capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
        self.out = done.stdout + done.stderr
        return done


@pytest.fixture
def pc(v2, server, origin, tmp_path):
    """A worker is added on SyncHub, with an SMB share and an NFS share whose mount points are inside the temporary folder."""
    with v2.connection() as conn:
        worker_id, token = workers.create(conn, "encoder-1")
    setup = Setup(tmp_path, server, origin, token, worker_id)
    with v2.connection() as conn:
        storage.create(conn, {"name": "media", "protocol": "smb", "host": "192.168.1.10", "share": "Media", "mount_point": setup.mount_point("media")})
        storage.create(conn, {"name": "tfiles", "protocol": "nfs", "host": "tower", "share": "/mnt/user/TFiles", "mount_point": setup.mount_point("tfiles")})
    return setup


def block(text):
    """The lines inside the SyncHub block of an fstab."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# BEGIN synchub-worker"))
    end = lines.index("# END synchub-worker")
    return lines[start + 1:end]


# ---------------------------------------------------------------- the script itself
def test_the_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    assert SCRIPT.stat().st_mode & stat.S_IEXEC, "the script must be executable (git update-index --chmod=+x)"


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
def test_the_script_passes_shellcheck():
    done = subprocess.run(["shellcheck", "-x", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def test_help_lists_the_options_and_changes_nothing():
    done = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert done.returncode == 0
    for option in ("--url", "--token", "--mode", "--dry-run", "--no-mounts", "--branch", "--smb-user"):
        assert option in done.stdout


def test_an_unknown_option_is_refused():
    done = subprocess.run(["bash", str(SCRIPT), "--nonsense"], capture_output=True, text=True)
    assert done.returncode == 1 and "Unknown option" in done.stderr


# ---------------------------------------------------------------- a normal run
def test_a_first_run_installs_mounts_writes_the_env_file_and_starts_the_worker(pc):
    done = pc.run()
    assert done.returncode == 0, pc.out
    # the shared file systems: the user's own lines stay, SyncHub's go in one marked block, and the old file is backed up
    fstab = pc.fstab.read_text()
    assert fstab.startswith("UUID=abc / ext4 defaults 0 1\n//nas/other /mnt/other cifs credentials=/root/x 0 0\n")
    mine = block(fstab)
    assert mine[0].startswith("//192.168.1.10/Media  ") and f"{pc.mount_point('media')}  cifs  credentials={pc.cred}" not in mine[0]
    assert pc.mount_point("media") in mine[0] and "credentials=/etc/synchub-smb.cred" in mine[0] and "nofail" in mine[0]
    assert mine[1].startswith("tower:/mnt/user/TFiles  ") and "  nfs  " in mine[1]
    assert len(list(pc.root.glob("fstab.synchub-backup-*"))) == 1
    # the login lives in its own private file, not in .env and not in fstab
    assert pc.cred.read_text() == 'username=unraid-user\npassword=p@ss"w$rd,x\n' and stat.S_IMODE(pc.cred.stat().st_mode) == 0o600
    assert "p@ss" not in fstab and "p@ss" not in (pc.dir / ".env").read_text()
    # the .env file
    env = (pc.dir / ".env").read_text()
    assert f"SYNCHUB_TOKEN={pc.token}" in env and f"SYNCHUB_URL={pc.server}" in env and f"WORK_DIR={pc.dir}/work\n" in env and "SMB_PASSWORD" not in env
    assert stat.S_IMODE((pc.dir / ".env").stat().st_mode) == 0o600
    # the compose file builds from the pulled code and sees the shares the PC mounted
    compose = (pc.dir / "docker-compose.yml").read_text()
    assert f'build: "{pc.repo}/agent"' in compose and "propagation: rslave" in compose and f'source: "{pc.mount_point("media")}"' in compose and "cifs" not in compose
    # the code came from "GitHub"
    assert (pc.repo / "agent" / "synchub_agent.py").is_file()
    # the shares were mounted, the worker built and started, the shares marked and checked, in that order
    calls = pc.calls()
    mounts = [i for i, c in enumerate(calls) if c.startswith("mount ")]
    up = next(i for i, c in enumerate(calls) if c == "docker compose up -d --build")
    marks = next(i for i, c in enumerate(calls) if c == "docker compose run --rm worker --init-roots")
    check = next(i for i, c in enumerate(calls) if c == "docker compose run --rm worker --check")
    assert len(mounts) == 2 and max(mounts) < up < marks < check
    assert any(c.startswith("apt-get install") for c in calls) is False      # docker, curl, git and the mount tools were all "present"


def test_missing_packages_are_installed(pc):
    # a PC with only the basics: the real tools the script needs are linked in, but no docker, cifs-utils or nfs-common
    basics = pc.root / "basics"
    basics.mkdir()
    for tool in ("bash", "sh", "git", "curl", "sed", "awk", "grep", "cat", "cp", "mv", "cmp", "mkdir", "install", "date", "tr", "wc", "tail", "head", "base64",
                 "mktemp", "rm", "id", "stat", "dirname", "ls", "env", "chmod", "printf", "sort", "cut", "uname", "ssh"):
        found = shutil.which(tool)
        if found:
            (basics / tool).symlink_to(found)
    for name in ("docker", "mount.cifs", "mount.nfs"):
        (pc.bin / name).unlink()
    done = pc.run(extra_env={"PATH": f"{pc.bin}:{basics}"})
    installs = [c for c in pc.calls() if c.startswith("apt-get install")]
    assert any("cifs-utils" in c for c in installs) and any("nfs-common" in c for c in installs) and any("docker.io" in c for c in installs), (installs, pc.out)
    # the stand-in apt-get installs nothing, so the script must stop and say why rather than carry on without Docker
    assert done.returncode != 0 and "docker" in pc.out.lower()


def test_running_it_again_changes_nothing_and_keeps_one_block(pc):
    assert pc.run().returncode == 0
    before = {p.name: p.read_bytes() for p in (pc.fstab, pc.cred, pc.dir / ".env", pc.dir / "docker-compose.yml")}
    backups = len(list(pc.root.glob("fstab.synchub-backup-*")))
    again = pc.run()
    assert again.returncode == 0, pc.out
    assert {p.name: p.read_bytes() for p in (pc.fstab, pc.cred, pc.dir / ".env", pc.dir / "docker-compose.yml")} == before
    assert len(list(pc.root.glob("fstab.synchub-backup-*"))) == backups and pc.fstab.read_text().count("# BEGIN synchub-worker") == 1
    assert "already up to date" in pc.out and "already mounted" in pc.out


def test_a_second_run_needs_no_url_token_or_login(pc):
    assert pc.run().returncode == 0
    again = pc.run("--repo", str(pc.origin), "--dir", str(pc.dir), "--repo-dir", str(pc.repo), use_defaults=False)
    assert again.returncode == 0, pc.out
    assert pc.cred.read_text().startswith("username=unraid-user")                 # the earlier login is kept


def test_a_changed_share_list_replaces_the_block_instead_of_adding_to_it(pc, v2):
    assert pc.run().returncode == 0
    with v2.connection() as conn:
        storage.delete(conn, storage.list_all(conn)[1]["id"])             # the NFS share is removed on SyncHub's Storage page
    assert pc.run().returncode == 0, pc.out
    mine = block(pc.fstab.read_text())
    assert len(mine) == 1 and mine[0].startswith("//192.168.1.10/Media") and "tower:" not in pc.fstab.read_text()
    assert pc.fstab.read_text().count("# BEGIN synchub-worker") == 1


def test_the_latest_code_is_pulled_on_a_later_run(pc, origin):
    assert pc.run().returncode == 0
    (origin / "agent" / "NEW.txt").write_text("fresh\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for command in (["add", "."], ["commit", "-q", "-m", "second"]):
        subprocess.run(["git", "-C", str(origin), *command], check=True, env=env, capture_output=True)
    assert pc.run().returncode == 0, pc.out
    assert (pc.repo / "agent" / "NEW.txt").read_text() == "fresh\n"


# ---------------------------------------------------------------- when something is wrong
def test_a_wrong_token_stops_before_anything_is_changed(pc):
    done = pc.run(token="sw_not-a-real-token")
    assert done.returncode == 1 and "does not accept this token" in pc.out
    assert pc.fstab.read_text().count("synchub") == 0 and not pc.dir.exists() and not pc.cred.exists()


def test_an_unreachable_synchub_is_explained(pc):
    done = pc.run("--url", "http://127.0.0.1:9")
    assert done.returncode == 1 and "Cannot reach SyncHub" in pc.out


def test_localhost_is_refused_as_the_address_of_synchub(pc):
    done = pc.run("--url", "http://localhost:5001", extra_env={"SYNCHUB_SETUP_ALLOW_LOCALHOST": ""})
    assert done.returncode == 1 and "means 'this PC'" in pc.out


def test_a_missing_smb_login_is_asked_for_not_guessed(pc):
    done = pc.run("--smb-user", "", "--smb-password", "")
    assert done.returncode == 1 and "needs the Unraid user and password" in pc.out and not pc.cred.exists()


def test_a_share_that_will_not_mount_is_reported_and_is_not_marked(pc):
    pc.fail_mount = pc.mount_point("media")
    done = pc.run()
    assert done.returncode == 3, pc.out
    assert "Could not mount" in pc.out and "Permission denied" in pc.out and "marker files were not written" in pc.out
    assert "docker compose run --rm worker --init-roots" not in pc.calls() and "docker compose up -d --build" in pc.calls()


def test_a_line_the_user_already_has_for_a_mount_point_is_left_alone(pc):
    pc.fstab.write_text(f"//other/Media {pc.mount_point('media')} cifs defaults 0 0\n")
    done = pc.run()
    assert done.returncode == 3 and "already has its own line" in pc.out
    text = pc.fstab.read_text()
    assert text.startswith(f"//other/Media {pc.mount_point('media')} cifs defaults 0 0\n") and "//192.168.1.10/Media" not in text and "tower:/mnt/user/TFiles" in text


def test_no_shares_yet_is_a_warning_not_a_failure(pc, v2):
    with v2.connection() as conn:
        for root in storage.list_all(conn):
            storage.delete(conn, root["id"])
    done = pc.run()
    assert done.returncode == 3 and "no mountable shares" in pc.out and "BEGIN synchub-worker" not in pc.fstab.read_text()


def test_no_mounts_leaves_fstab_and_the_mounts_alone(pc):
    before = pc.fstab.read_text()
    done = pc.run("--no-mounts")
    assert done.returncode == 0, pc.out
    assert pc.fstab.read_text() == before and not pc.cred.exists() and not any(c.startswith("mount ") for c in pc.calls())


def test_a_dry_run_changes_nothing(pc):
    before = pc.fstab.read_text()
    done = pc.run("--dry-run")
    assert done.returncode == 0, pc.out
    assert "Dry run finished" in pc.out and "BEGIN synchub-worker" in pc.out
    assert pc.fstab.read_text() == before and not pc.dir.exists() and not pc.repo.exists() and not pc.cred.exists()
    assert pc.calls() == []                                                    # no apt-get, mount, systemctl or docker was run


# ---------------------------------------------------------------- without Docker
def test_the_native_mode_gives_the_service_user_the_cifs_mounts(pc):
    shims = {"id": 'if [ "$1" = "-u" ]; then echo 1234; elif [ "$1" = "-g" ]; then echo 5678; else exit 0; fi'}
    for name, body in shims.items():
        (pc.bin / name).write_text("#!/bin/sh\n" + body + "\n")
        (pc.bin / name).chmod(0o755)
    script = f'SYNCHUB_SETUP_SOURCED=1 source {SCRIPT}; MODE=native; TMP={pc.root}; printf "%s\\n" "//h/s /mnt/a cifs credentials=/c,vers=3.0 0 0" "h:/x /mnt/b nfs hard 0 0" > "$TMP/fstab.entries"; fstab_entries'
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "PATH": f"{pc.bin}:{os.environ['PATH']}"})
    lines = done.stdout.splitlines()
    assert "uid=1234,gid=5678" in lines[0] and "file_mode=0664" in lines[0] and lines[1] == "h:/x /mnt/b nfs hard 0 0", done.stderr


def test_the_native_mode_dry_run_names_what_it_would_do(pc):
    done = pc.run("--dry-run", "--mode", "native")
    assert done.returncode == 0, pc.out
    assert "useradd --system" in pc.out and "systemctl restart synchub-agent" in pc.out and "docker compose up" not in pc.out and pc.calls() == []


# ---------------------------------------------------------------- what SyncHub serves to the script
def test_synchub_serves_the_setup_files_only_to_the_worker_with_the_token(pc, app):
    client = app.test_client()
    url = "/api/worker/v1/setup/.env"
    assert client.post(url).status_code == 401 and client.post(url, headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.post(url + "?mounts=host", headers={"Authorization": f"Bearer {pc.token}"})
    assert ok.status_code == 200 and ok.mimetype == "text/plain" and f"SYNCHUB_TOKEN={pc.token}" in ok.get_data(as_text=True)
    assert "SMB_PASSWORD" not in ok.get_data(as_text=True)
    assert client.post("/api/worker/v1/setup/passwd", headers={"Authorization": f"Bearer {pc.token}"}).status_code == 404
    assert client.post("/api/worker/v1/setup/..%2f..%2fetc%2fpasswd", headers={"Authorization": f"Bearer {pc.token}"}).status_code == 404


def test_host_mounts_change_the_compose_file_and_env_but_not_the_default(pc, v2):
    with v2.connection() as conn:
        roots = storage.list_all(conn)
    plain = storage.setup_files("w", "http://x:5001", "sw_t", roots)
    host = storage.setup_files("w", "http://x:5001", "sw_t", roots, host_mounts=True)
    assert "driver: local" in plain["docker-compose.yml"] and "SMB_PASSWORD" in plain[".env"]
    assert "driver_opts" not in host["docker-compose.yml"] and "propagation: rslave" in host["docker-compose.yml"] and "volumes:\n" not in host["docker-compose.yml"].split("services:")[1].split("    volumes:")[0]
    assert "SMB_PASSWORD" not in host[".env"]
