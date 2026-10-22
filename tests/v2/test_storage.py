import json
import shutil
import subprocess

import pytest

from synchub import jobs, storage, workers

SMB = {"name": "media", "protocol": "smb", "host": "192.168.1.10", "share": "Media", "mount_point": "/mnt/tower/Media"}
NFS = {"name": "tfiles", "protocol": "nfs", "host": "tower", "share": "/mnt/user/TFiles", "mount_point": "/mnt/tower/TFiles"}


def add(v2, *shares):
    with v2.connection() as conn:
        return [storage.create(conn, s) for s in shares]


# ---------------------------------------------------------------- what is accepted
def test_a_share_is_cleaned_and_gets_the_default_options_for_its_protocol():
    smb = storage.validate({**SMB, "name": " Media ", "label": "  Jellyfin   library "})
    assert smb["name"] == "media" and smb["label"] == "Jellyfin library" and smb["options"] == "vers=3.0" and smb["host"] == "192.168.1.10"
    assert storage.validate(NFS)["options"] == "nfsvers=4,hard"
    local = storage.validate({"name": "t", "protocol": "local", "share": "/mnt/x/", "mount_point": "/mnt/x", "options": "ignored", "host": "ignored"})
    assert local["options"] == "" and local["host"] is None and local["share"] == "/mnt/x"


@pytest.mark.parametrize("change, fragment", [
    ({"name": ""}, "name"), ({"name": "Has Space"}, "name"), ({"name": "-x"}, "name"), ({"name": "a" * 40}, "name"),
    ({"protocol": "ftp"}, "SMB"),
    ({"host": ""}, "address"), ({"host": "bad host"}, "address"), ({"host": "-x"}, "address"),
    ({"share": "Media/Sub"}, "share name"), ({"share": ""}, "share name"),
    ({"mount_point": "mnt/x"}, "mount point"), ({"mount_point": "/mnt/../etc"}, "mount point"), ({"mount_point": "/mnt/with space"}, "mount point"), ({"mount_point": "/"}, "mount point"),
    ({"options": "vers=3.0 ro"}, "options"), ({"options": "username=bob"}, "user name or password"), ({"options": "vers=3.0,password=x"}, "user name or password"),
    ({"options": "credentials=/etc/x"}, "user name or password"),
    ({"jellyfin_path": "media"}, "Jellyfin"),
])
def test_unusable_settings_say_what_is_wrong(change, fragment):
    with pytest.raises(storage.StorageError) as caught:
        storage.validate({**SMB, **change})
    assert fragment.lower() in str(caught.value).lower()


def test_nfs_needs_an_absolute_export_path_without_dots():
    for bad in ("mnt/user/x", "/mnt/../x", ""):
        with pytest.raises(storage.StorageError):
            storage.validate({**NFS, "share": bad})


def test_paths_inside_a_share_cannot_climb_out_or_be_empty():
    assert storage.clean_rel("/Movies/Heat (1995)/Heat.mkv/") == "Movies/Heat (1995)/Heat.mkv"
    assert storage.clean_rel("a\\b\\c.mkv") == "a/b/c.mkv"
    for bad in ("", "/", "..", "a/../b", "a//b", "./a", "a/\x00b", "a\nb"):
        with pytest.raises(storage.StorageError):
            storage.clean_rel(bad)


# ---------------------------------------------------------------- keeping them
def test_shares_are_unique_and_may_not_nest(v2):
    add(v2, SMB)
    with v2.connection() as conn:
        for bad in ({**NFS, "name": "media"}, {**NFS, "mount_point": "/mnt/tower/Media"}, {**NFS, "mount_point": "/mnt/tower/Media/sub"}, {**NFS, "mount_point": "/mnt/tower"}):
            with pytest.raises(storage.StorageError):
                storage.create(conn, bad)
        assert [r["name"] for r in storage.list_all(conn)] == ["media"]


def test_a_share_can_be_edited_and_keeps_its_unchanged_settings(v2):
    (root_id,) = add(v2, SMB)
    with v2.connection() as conn:
        storage.update(conn, root_id, {"host": "192.168.1.11", "radarr_path": "/data/media"})
        root = storage.get(conn, root_id)
        assert root["host"] == "192.168.1.11" and root["radarr_path"] == "/data/media" and root["share"] == "Media" and root["options"] == "vers=3.0"
        with pytest.raises(storage.StorageError):
            storage.update(conn, root_id, {"options": "password=x"})
        assert storage.get(conn, root_id)["options"] == "vers=3.0"
        with pytest.raises(storage.StorageError):
            storage.update(conn, 999, {})


def test_a_share_with_waiting_jobs_cannot_be_removed_or_renamed(v2):
    media_id, _ = add(v2, SMB, NFS)
    with v2.connection() as conn:
        job_id = jobs.create(conn, "library", source=("media", "a.mkv"), dest=("tfiles", "a.mkv"))
        with pytest.raises(storage.StorageError, match="waiting or running"):
            storage.delete(conn, media_id)
        with pytest.raises(storage.StorageError, match="Rename"):
            storage.update(conn, media_id, {"name": "movies"})
        jobs.cancel(conn, job_id)
        storage.delete(conn, media_id)
        assert [r["name"] for r in storage.list_all(conn)] == ["tfiles"]


def test_the_agent_is_told_the_shares_and_their_markers(v2):
    add(v2, SMB, NFS)
    with v2.connection() as conn:
        assert storage.for_agent(conn) == [{"name": "media", "mount_point": "/mnt/tower/Media", "marker": ".synchub-root", "marker_text": "media"},
                                           {"name": "tfiles", "mount_point": "/mnt/tower/TFiles", "marker": ".synchub-root", "marker_text": "tfiles"}]


# ---------------------------------------------------------------- the test copy
def test_a_test_copy_is_a_pinned_copy_job_between_two_shares(v2):
    add(v2, SMB, NFS)
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "enc")
        job_id = storage.queue_test_copy(conn, "media", "/Movies/Heat.mkv", "tfiles", "tests/Heat.mkv", worker_id)
        job = jobs.get(conn, job_id)
    assert job["kind"] == "library" and job["plan"] == {"op": "copy"} and job["pinned_worker_id"] == worker_id and job["title"] == "Test copy: Heat.mkv"
    assert (job["source_root"], job["source_rel"], job["dest_root"], job["dest_rel"]) == ("media", "Movies/Heat.mkv", "tfiles", "tests/Heat.mkv")


def test_a_test_copy_refuses_unknown_shares_bad_paths_and_overwriting_itself(v2):
    add(v2, SMB)
    with v2.connection() as conn:
        for args, fragment in ((("media", "a.mkv", "nowhere", "b.mkv"), "no share"), (("media", "../x", "media", "b.mkv"), "inside the share"),
                               (("media", "a.mkv", "media", "a.mkv"), "overwrite"), (("media", "a.mkv", "media", "b.mkv", 99), "no longer exists")):
            with pytest.raises(storage.StorageError, match=fragment):
                storage.queue_test_copy(conn, *args)
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


# ---------------------------------------------------------------- the files that set a worker up
def roots(*shares):
    return [storage.validate(s) for s in shares]


def test_the_compose_file_is_valid_yaml_and_mounts_each_share_with_dockers_own_driver():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(storage.compose_file("encoder-1", roots(SMB, NFS, {"name": "local", "protocol": "local", "share": "/srv/x", "mount_point": "/mnt/local"})))
    worker = doc["services"]["worker"]
    assert worker["restart"] == "unless-stopped" and worker["build"] == "./agent" and "env_file" not in worker
    assert set(worker["environment"]) == {"SYNCHUB_URL", "SYNCHUB_TOKEN", "SYNCHUB_NAME", "SYNCHUB_WORK"}               # the share login is not handed to the container
    assert "${WORK_DIR:-./work}:/work" in worker["volumes"] and "root_media:/mnt/tower/Media" in worker["volumes"]
    assert "root_tfiles:/mnt/tower/TFiles" in worker["volumes"] and "/srv/x:/mnt/local" in worker["volumes"]
    media, tfiles = doc["volumes"]["root_media"]["driver_opts"], doc["volumes"]["root_tfiles"]["driver_opts"]
    assert media == {"type": "cifs", "o": "addr=192.168.1.10,username=${SMB_USER},password=${SMB_PASSWORD},vers=3.0", "device": "//192.168.1.10/Media"}
    assert tfiles == {"type": "nfs", "o": "addr=tower,nfsvers=4,hard", "device": ":/mnt/user/TFiles"}
    assert set(doc["volumes"]) == {"root_media", "root_tfiles"}                                  # a local share needs no volume


def test_a_name_with_odd_characters_cannot_break_the_compose_file():
    yaml = pytest.importorskip("yaml")
    odd = storage.validate({**SMB, "share": "My Media - 2024", "options": "vers=3.0,ro"})
    doc = yaml.safe_load(storage.compose_file('o"dd: name', [odd]))
    assert doc["volumes"]["root_media"]["driver_opts"]["device"] == "//192.168.1.10/My Media - 2024"


def test_no_login_ever_appears_in_the_generated_files():
    files = storage.setup_files("encoder-1", "http://192.168.1.20:5001", "sw_secret", roots(SMB, NFS))
    assert "SMB_USER=\n" in files[".env"] and "SMB_PASSWORD=\n" in files[".env"]
    for name in ("docker-compose.yml", "fstab"):
        assert "sw_secret" not in files[name]
    assert "sw_secret" in files[".env"] and "sw_secret" in files["synchub-agent.ini"]
    assert "SMB_USER" not in storage.env_file("w", "http://x", "t", roots(NFS))                    # no SMB share, no login to ask for


def test_fstab_lines_for_workers_without_docker():
    text = storage.fstab_lines(roots({**SMB, "share": "My Media"}, NFS, {"name": "l", "protocol": "local", "share": "/srv", "mount_point": "/mnt/l"}))
    assert "//192.168.1.10/My\\040Media  /mnt/tower/Media  cifs  credentials=/etc/synchub-smb.cred,vers=3.0,_netdev,nofail  0  0" in text
    assert "tower:/mnt/user/TFiles  /mnt/tower/TFiles  nfs  nfsvers=4,hard,_netdev,nofail  0  0" in text
    assert "# l: /mnt/l is already mounted" in text and "chmod 600" in text


def test_setup_files_cover_a_worker_with_no_shares_yet():
    files = storage.setup_files("encoder-1", "http://x:5001", "sw_t", [])
    assert set(files) == set(storage.SETUP_ORDER) == {".env", "docker-compose.yml", "synchub-agent.ini", "fstab"}
    assert "volumes:" in files["docker-compose.yml"] and "root_" not in files["docker-compose.yml"]


def test_the_committed_example_files_are_exactly_what_synchub_generates():
    from pathlib import Path
    examples = Path(__file__).resolve().parents[2] / "agent" / "examples"
    sample = roots({"name": "media", "protocol": "smb", "host": "192.168.1.10", "share": "Media", "mount_point": "/mnt/tower/Media"},
                   {"name": "tfiles", "protocol": "smb", "host": "192.168.1.10", "share": "TFiles", "mount_point": "/mnt/tower/TFiles"})
    for name, text in storage.setup_files("encoder-1", "http://192.168.1.20:5001", "sw_replace-with-the-token-from-synchub", sample).items():
        shown = "env.example" if name == ".env" else name                      # a real .env is git-ignored, so the sample has another name
        assert (examples / shown).read_text() == text, f"agent/examples/{shown} is out of date: regenerate it from storage.setup_files"


def _compose_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _compose_available(), reason="docker compose is not installed")
def test_docker_compose_itself_accepts_the_generated_files_and_fills_in_the_login(tmp_path):
    files = storage.setup_files("encoder-1", "http://192.168.1.20:5001", "sw_token", roots(SMB, NFS))
    (tmp_path / "docker-compose.yml").write_text(files["docker-compose.yml"])
    (tmp_path / ".env").write_text(files[".env"].replace("SMB_USER=\n", "SMB_USER=bob\n").replace("SMB_PASSWORD=\n", "SMB_PASSWORD=p$$w\n"))
    done = subprocess.run(["docker", "compose", "--project-directory", str(tmp_path), "config", "--format", "json"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    config = json.loads(done.stdout)
    # compose prints a literal $ as $$ (it is written $$ in .env for the same reason), so p$$w here is the password p$w
    assert config["volumes"]["root_media"]["driver_opts"] == {"type": "cifs", "o": "addr=192.168.1.10,username=bob,password=p$$w,vers=3.0", "device": "//192.168.1.10/Media"}
    assert config["volumes"]["root_tfiles"]["driver_opts"] == {"type": "nfs", "o": "addr=tower,nfsvers=4,hard", "device": ":/mnt/user/TFiles"}
    environment = config["services"]["worker"]["environment"]
    assert environment["SYNCHUB_TOKEN"] == "sw_token" and "SMB_PASSWORD" not in environment                   # the share login stays out of the container
    (tmp_path / ".env").write_text(files[".env"].replace("SYNCHUB_TOKEN=sw_token", "SYNCHUB_TOKEN="))
    done = subprocess.run(["docker", "compose", "--project-directory", str(tmp_path), "config", "-q"], capture_output=True, text=True)
    assert done.returncode != 0 and "set SYNCHUB_TOKEN in .env" in done.stderr


def test_an_smb_share_named_by_host_name_gets_a_warning_in_the_compose_file_but_an_ip_address_does_not():
    named = storage.compose_file("w", roots({**SMB, "host": "tower"}))
    assert "use the server's IP address" in named and "addr=tower" in named
    assert "IP address" not in storage.compose_file("w", roots(SMB))
    assert "IP address" not in storage.compose_file("w", roots({**NFS, "host": "tower"}))                  # Docker does look names up for NFS
    pytest.importorskip("yaml").safe_load(named)
