import json
import re

import pytest

from synchub import jobs, storage, workers

SMB = {"name": "media", "protocol": "smb", "host": "192.168.1.10", "share": "Media", "mount_point": "/mnt/tower/Media"}
NFS = {"name": "tfiles", "protocol": "nfs", "host": "tower", "share": "/mnt/user/TFiles", "mount_point": "/mnt/tower/TFiles"}


def add_shares(v2, *shares):
    with v2.connection() as conn:
        return [storage.create(conn, s) for s in shares]


def report(v2, worker_id, status):
    with v2.connection() as conn:
        workers.touch(conn, worker_id, status)


def post(client, url, body=None):
    return client.post(url, json=body or {})


# ---------------------------------------------------------------- the Storage page
def test_the_storage_page_is_in_the_nav_and_explains_an_empty_list(client):
    page = client.get("/storage").get_data(as_text=True)
    assert 'href="/storage"' in page and "No shares yet" in page and "How a worker gets these shares" in page and "--init-roots" in page


def test_the_storage_page_lists_each_share_and_what_each_worker_says_about_it(client, v2):
    add_shares(v2, {**SMB, "jellyfin_path": "/media"}, NFS)
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "encoder-1")
    report(v2, worker_id, {"roots": {"media": {"ok": True, "state": "ok", "free_bytes": 2 * 1024 ** 4}, "tfiles": {"ok": False, "state": "not_mounted", "message": "Nothing is mounted there"}}})
    page = client.get("/storage").get_data(as_text=True)
    media = re.search(r'<tr data-share="\d+">(?:(?!</tr>).)*media.*?</tr>', page, re.S).group(0)
    assert "//192.168.1.10/Media" in media and "/mnt/tower/Media" in media and "Jellyfin: <code>/media</code>" in media
    tfiles = re.search(r'<tr data-share="\d+">(?:(?!</tr>).)*tfiles.*?</tr>', page, re.S).group(0)
    assert "tower:/mnt/user/TFiles" in tfiles and "NFS" in tfiles


def test_what_a_worker_reports_for_a_share_is_shown_with_its_reason(client, v2):
    add_shares(v2, SMB, NFS)
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "encoder-1")
        workers.register(conn, worker_id, {"protocol": workers.PROTOCOL})
    report(v2, worker_id, {"roots": {"media": {"ok": True, "state": "ok", "free_bytes": 2 * 1024 ** 4}, "tfiles": {"ok": False, "state": "not_mounted", "message": "Nothing is mounted there"}}})
    page = client.get("/storage").get_data(as_text=True)
    assert re.search(r'badge bg-success"[^>]*>ready</span> encoder-1 <span class="text-body-secondary">&middot; 2,048.0 GB free', page)
    assert re.search(r'badge bg-danger" title="Nothing is mounted there">not mounted</span> encoder-1', page)
    row = re.search(r'<tr data-worker="%d">.*?</tr>' % worker_id, client.get("/workers").get_data(as_text=True), re.S).group(0)
    assert "ready</span> media" in row and "not mounted</span> tfiles" in row and "Nothing is mounted there" in row


def test_a_worker_that_has_not_reported_a_share_is_marked_so(client, v2):
    add_shares(v2, SMB)
    with v2.connection() as conn:
        workers.create(conn, "encoder-1")
    assert "not reported" in client.get("/storage").get_data(as_text=True)


def test_shares_are_added_edited_and_removed_from_the_page(client, v2):
    assert post(client, "/api/storage", SMB).get_json() == {"ok": True, "message": "Share added."}
    with v2.connection() as conn:
        (root,) = storage.list_all(conn)
    assert root["options"] == "vers=3.0"
    assert post(client, f"/api/storage/{root['id']}/update", {"host": "192.168.1.11"}).get_json()["ok"]
    assert "192.168.1.11" in client.get("/storage").get_data(as_text=True)
    assert post(client, f"/api/storage/{root['id']}/delete").get_json()["ok"]
    assert "No shares yet" in client.get("/storage").get_data(as_text=True)


@pytest.mark.parametrize("change, fragment", [({"name": "Bad Name"}, "name"), ({"options": "password=x"}, "password"), ({"mount_point": "relative"}, "mount point")])
def test_a_share_that_cannot_be_used_is_explained(client, change, fragment):
    r = post(client, "/api/storage", {**SMB, **change})
    assert r.status_code == 400 and fragment in r.get_json()["message"].lower()


def test_removing_or_editing_a_share_that_no_longer_exists_is_an_error_not_a_crash(client):
    assert post(client, "/api/storage/99/update", {}).status_code == 400
    assert post(client, "/api/storage/99/delete").status_code == 400


def test_share_text_is_escaped(client, v2):
    add_shares(v2, {**SMB, "label": "<script>alert(1)</script>", "radarr_path": "/data/<b>"})
    page = client.get("/storage").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "/data/<b>" not in page


# ---------------------------------------------------------------- setup files
def test_a_new_worker_gets_setup_files_with_its_token_and_its_shares(client, v2):
    add_shares(v2, SMB, NFS)
    data = post(client, "/api/workers", {"name": "encoder-1", "slots": 1}).get_json()
    setup = data["setup"]
    assert setup["order"] == [".env", "docker-compose.yml", "synchub-agent.ini", "fstab"] and set(setup["order"]) == set(setup["files"])
    assert setup["has_token"] and setup["shares"] == 2 and setup["url"].startswith("http://localhost")
    assert f"SYNCHUB_TOKEN={data['token']}" in setup["files"][".env"] and f"token = {data['token']}" in setup["files"]["synchub-agent.ini"]
    assert "root_media" in setup["files"]["docker-compose.yml"] and "root_tfiles" in setup["files"]["docker-compose.yml"] and data["token"] not in setup["files"]["docker-compose.yml"]
    assert data["config"].startswith("[synchub]")                                      # the short form is still there


def test_setup_files_can_be_regenerated_later_without_the_token(client, v2):
    add_shares(v2, SMB)
    worker_id = post(client, "/api/workers", {"name": "encoder-1"}).get_json()["id"]
    data = client.get(f"/api/workers/{worker_id}/setup").get_json()
    assert data["ok"] and data["name"] == "encoder-1" and not data["setup"]["has_token"]
    assert "SYNCHUB_TOKEN=<the token shown when this worker was added>" in data["setup"]["files"][".env"] and "root_media" in data["setup"]["files"]["docker-compose.yml"]
    assert client.get("/api/workers/99/setup").status_code == 404


def test_a_new_token_comes_with_fresh_setup_files(client, v2):
    worker_id = post(client, "/api/workers", {"name": "encoder-1"}).get_json()["id"]
    data = post(client, f"/api/workers/{worker_id}/rotate").get_json()
    assert f"SYNCHUB_TOKEN={data['token']}" in data["setup"]["files"][".env"] and data["setup"]["has_token"]


def test_the_workers_page_offers_the_setup_files_and_a_test_copy(client, v2):
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "encoder-1")
    page = client.get("/workers").get_data(as_text=True)
    assert f'data-setup-worker="{worker_id}"' in page and 'id="testCopyBtn"' in page and 'id="copyModal"' in page and 'id="setupModal"' in page


# ---------------------------------------------------------------- the test copy
def test_a_test_copy_is_queued_from_the_page_and_shows_in_the_job_list(client, v2):
    add_shares(v2, SMB, NFS)
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "encoder-1")
    r = post(client, "/api/jobs/test-copy", {"worker_id": str(worker_id), "source_root": "media", "source_rel": "Movies/Heat.mkv", "dest_root": "tfiles", "dest_rel": "synchub-test/Heat.mkv"}).get_json()
    assert r["ok"] and "Queued test copy" in r["message"]
    with v2.connection() as conn:
        job = jobs.get(conn, r["id"])
    assert job["plan"] == {"op": "copy"} and job["pinned_worker_id"] == worker_id
    assert "Test copy: Heat.mkv" in client.get("/workers").get_data(as_text=True)


def test_a_test_copy_for_any_worker_leaves_the_worker_open(client, v2):
    add_shares(v2, SMB)
    r = post(client, "/api/jobs/test-copy", {"worker_id": "", "source_root": "media", "source_rel": "a.mkv", "dest_root": "media", "dest_rel": "b.mkv"}).get_json()
    with v2.connection() as conn:
        assert jobs.get(conn, r["id"])["pinned_worker_id"] is None


@pytest.mark.parametrize("body, fragment", [
    ({"source_root": "nope", "source_rel": "a", "dest_root": "media", "dest_rel": "b"}, "no share"),
    ({"source_root": "media", "source_rel": "../a", "dest_root": "media", "dest_rel": "b"}, "inside the share"),
    ({"source_root": "media", "source_rel": "a", "dest_root": "media", "dest_rel": "a"}, "overwrite"),
])
def test_a_bad_test_copy_is_explained_and_queues_nothing(client, v2, body, fragment):
    add_shares(v2, SMB)
    r = post(client, "/api/jobs/test-copy", body)
    assert r.status_code == 400 and fragment in r.get_json()["message"].lower()
    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


def test_the_job_list_says_when_no_worker_has_the_share_a_job_needs(client, v2):
    add_shares(v2, SMB, NFS)
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "encoder-1")
        workers.register(conn, worker_id, {"protocol": workers.PROTOCOL})
        jobs.create(conn, "library", title="Wants tfiles", source=("media", "a.mkv"), dest=("tfiles", "a.mkv"))
    report(v2, worker_id, {"roots": {"media": {"ok": True}, "tfiles": {"ok": False, "state": "not_mounted"}}})
    assert "No online worker has tfiles mounted and ready." in client.get("/workers").get_data(as_text=True)
    report(v2, worker_id, {"roots": {"media": {"ok": True}, "tfiles": {"ok": True}}})
    assert "mounted and ready" not in client.get("/workers").get_data(as_text=True)


def test_no_hint_while_no_worker_is_online_because_that_is_a_different_problem(client, v2):
    add_shares(v2, SMB)
    with v2.connection() as conn:
        jobs.create(conn, "library", title="Waiting", source=("media", "a.mkv"), dest=("media", "b.mkv"))
    assert "mounted and ready" not in client.get("/workers").get_data(as_text=True)


# ---------------------------------------------------------------- the full log
def running_job(v2):
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "encoder-1")
        job_id = jobs.create(conn, "library", title="A job")
        conn.execute("UPDATE job SET status = 'running', worker_id = ? WHERE id = ?", (worker_id, job_id))
    return worker_id, job_id


def test_asking_for_a_log_marks_the_request_and_names_the_worker(client, v2):
    worker_id, job_id = running_job(v2)
    before = client.get(f"/api/jobs/{job_id}/log").get_json()
    assert before["log"] is None and not before["pending"] and before["holder"] == "encoder-1" and not before["holder_online"]
    r = post(client, f"/api/jobs/{job_id}/log").get_json()
    assert r["ok"] and r["holder"] == "encoder-1" and "offline" in r["message"]
    assert client.get(f"/api/jobs/{job_id}/log").get_json()["pending"]
    with v2.connection() as conn:
        assert jobs.log_requests_for(conn, worker_id) == [job_id]


def test_an_online_worker_is_told_it_was_asked(client, v2):
    worker_id, job_id = running_job(v2)
    report(v2, worker_id, {})
    r = post(client, f"/api/jobs/{job_id}/log").get_json()
    assert r["holder_online"] and r["message"] == "Asked the worker for the log."


def test_the_uploaded_log_is_shown_with_who_sent_it(client, v2):
    worker_id, job_id = running_job(v2)
    with v2.connection() as conn:
        jobs.request_log(conn, job_id)
        jobs.store_log(conn, job_id, worker_id, "first line\n<b>second</b>\n")
    data = client.get(f"/api/jobs/{job_id}/log").get_json()
    assert not data["pending"] and data["log"]["content"] == "first line\n<b>second</b>\n" and data["log"]["worker_name"] == "encoder-1" and data["log"]["received"]


def test_a_log_cannot_be_asked_for_a_job_that_nobody_ran_or_that_is_gone(client, v2):
    with v2.connection() as conn:
        job_id = jobs.create(conn, "library", title="Never ran")
    r = post(client, f"/api/jobs/{job_id}/log")
    assert r.status_code == 400 and "No worker has run" in r.get_json()["message"]
    assert client.get("/api/jobs/99/log").status_code == 404
    assert post(client, "/api/jobs/99/log").status_code == 400
