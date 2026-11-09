"""Hiding the original of a copied film from Jellyfin, the Windows folder path, and the case-insensitive Radarr/Sonarr path match."""
import json

import pytest

from synchub import downloads, jobs, settings, storage
from synchub.library import hide

from .test_agent_live import make_avi, needs_ffmpeg, rig, wait          # noqa: F401  (rig is a fixture)
from .test_conversion_module import finished_copy
from .test_library_findings import rec_of
from .test_library_migrate import approve
from .library_helpers import add

FILM = "Movies/Scream (1996)/Scream.1996.2160p.mkv"


def hide_jobs(conn, record_id):
    return [dict(r) for r in conn.execute("SELECT * FROM job WHERE ref_type = 'library_hide' AND ref_id = ? ORDER BY id", (record_id,))]


def test_a_verified_copy_to_another_share_queues_the_hiding(conn):
    record_id, _ = finished_copy(conn, "0", rel=FILM, name="Scream (1996).mkv")
    queued = hide_jobs(conn, record_id)
    assert len(queued) == 1 and json.loads(queued[0]["plan"])["op"] == "hide_source" and queued[0]["source_rel"] == FILM


def test_nothing_is_hidden_when_the_setting_is_off_or_the_file_has_no_folder_of_its_own(conn):
    settings.set_value("hide_source", "0", conn=conn)
    off, _ = finished_copy(conn, "0", rel=FILM, name="Scream (1996).mkv")
    assert hide_jobs(conn, off) == []
    settings.set_value("hide_source", "1", conn=conn)
    loose, _ = finished_copy(conn, "0", rel="Movies/Heat.mkv", name="Heat (1995).mkv")
    assert hide_jobs(conn, loose) == []


def test_the_record_follows_the_hide_job_and_a_person_can_show_it_again(conn):
    record_id, _ = finished_copy(conn, "0", rel=FILM, name="Scream (1996).mkv")
    job_id = hide_jobs(conn, record_id)[0]["id"]
    conn.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?", (json.dumps({"op": "hide_source", "hidden": True}), job_id))
    assert hide.sync(conn, job_id) is True
    assert rec_of(conn, record_id)["source_hidden_at"] and "Hidden" in rec_of(conn, record_id)["source_hide_message"]
    with pytest.raises(ValueError):
        hide.request(conn, record_id)                                       # already hidden
    undo = hide.request(conn, record_id, undo=True)
    conn.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?", (json.dumps({"op": "unhide_source", "hidden": False}), undo))
    assert hide.sync(conn, undo) is True and rec_of(conn, record_id)["source_hidden_at"] is None


def test_a_skipped_hide_leaves_the_original_visible_and_says_why(conn):
    record_id, _ = finished_copy(conn, "0", rel=FILM, name="Scream (1996).mkv")
    job_id = hide_jobs(conn, record_id)[0]["id"]
    conn.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?", (json.dumps({"op": "hide_source", "hidden": False, "skipped": "The folder also holds Other.mkv"}), job_id))
    assert hide.sync(conn, job_id) is False
    rec = rec_of(conn, record_id)
    assert rec["source_hidden_at"] is None and "Other.mkv" in rec["source_hide_message"]


def test_the_backfill_queues_earlier_originals_once(conn):
    settings.set_value("hide_source", "0", conn=conn)
    record_id, _ = finished_copy(conn, "0", rel=FILM, name="Scream (1996).mkv")
    assert hide.backfill(conn) == 1 and hide.backfill(conn) == 0
    assert len(hide_jobs(conn, record_id)) == 1


def test_windows_path_is_built_from_the_smb_address_and_share():
    root = {"protocol": "smb", "host": "192.168.128.245", "share": "TFiles"}
    assert hide.windows_path(root, "Movies/Scream (1996)") == "\\\\192.168.128.245\\TFiles\\Movies\\Scream (1996)"
    assert hide.windows_path(root) == "\\\\192.168.128.245\\TFiles"
    assert hide.windows_path({"protocol": "nfs", "host": "h", "share": "/x"}, "a") is None


def test_radarr_and_sonarr_paths_match_whatever_the_case_and_the_error_lists_what_is_set(conn):
    storage.create(conn, {"name": "tfiles", "protocol": "local", "share": "/mnt/tfiles", "mount_point": "/mnt/tfiles", "radarr_path": "/Media/TFiles", "sonarr_path": "/data"})
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media"})
    conn.commit()
    assert downloads.locate(conn, "radarr", "/media/tfiles/Movies/8 Mile (2002)/8 Mile.mkv") == ("tfiles", "Movies/8 Mile (2002)/8 Mile.mkv")
    assert downloads.locate(conn, "sonarr", "/DATA/TV Shows/SNL/x.mkv") == ("tfiles", "TV Shows/SNL/x.mkv")
    assert downloads.locate(conn, "radarr", "/other/x.mkv") is None
    message = downloads.not_covered(conn, "sonarr", "/other/x.mkv")
    assert "tfiles = /data" in message and "media = (not set)" in message and "/other/x.mkv" in message


def test_the_jellyfin_other_mounts_match_ignores_case(conn):
    roots = [{"name": "tfiles", "jellyfin_path": None, "jellyfin_extra_paths": "/media2 = Movies"}]
    assert storage.from_app_path(roots, "/MEDIA2/Scream (1996)/x.mkv") == ("tfiles", "Movies/Scream (1996)/x.mkv")


@needs_ffmpeg
def test_a_worker_hides_the_original_with_an_ignore_file_and_deleting_it_leaves_no_folder(rig, conn):          # noqa: F811
    source = rig.roots["tfiles"] / FILM
    make_avi(source)
    stat = source.stat()
    record_id = add(conn, root="tfiles", rel=FILM, name="Scream (1996).mkv", size=stat.st_size, mtime=stat.st_mtime)
    approve(conn, record_id)
    rig.start()
    wait(lambda: (conn.commit() or True) and rec_of(conn, record_id)["source_hidden_at"], timeout=60, what="the hiding")
    ignore = source.parent / ".ignore"
    assert ignore.exists() and ignore.stat().st_size == 0 and source.exists()


def test_the_hide_buttons_send_the_csrf_token():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "synchub" / "web" / "templates"
    for name, url in (("_quality.html", "hideSource, { method"), ("_options_scripts.html", "hide-originals', { method")):
        line = next(l for l in (root / name).read_text().splitlines() if url in l)
        assert "X-CSRFToken" in line, name
