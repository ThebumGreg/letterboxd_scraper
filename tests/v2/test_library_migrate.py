import json

import pytest

from synchub import jobs, settings, workers
from synchub.library import migrate, recommend as rc, store
from synchub.library import vocab as v

from .library_helpers import CFG, add, plan
from .test_library_findings import rec_of


def approve(conn, record_id, **kw):
    outcome = rc.apply_recommendation(conn, record_id, "Greg", cfg=CFG, **kw)
    conn.commit()
    return outcome


def job_of(conn, record_id):
    return conn.execute("SELECT id FROM job WHERE ref_type = 'library_file' AND ref_id = ? ORDER BY id DESC", (record_id,)).fetchone()[0]


def worker(conn):
    worker_id, _ = workers.create(conn, "Worker-1", [], 1)
    conn.commit()
    return workers.get(conn, worker_id)


def convert_record(conn):
    return add(conn, rel="Old/Heat.avi", name="Heat (1995).mkv", container="avi", video="mpeg4", action="remux_copy",
               proposals=[plan(convert={"remux": True}, keep_audio=(1,))])


def test_approving_a_plain_file_queues_a_copy_job_for_the_destination(conn):
    record_id = add(conn, rel="Movies/Heat.1995.mkv", name="Heat (1995).mkv", size=3 * 1024 ** 3)
    assert approve(conn, record_id) == "approved"
    job = jobs.get(conn, job_of(conn, record_id))
    assert (job["kind"], job["status"], job["ref_type"], job["ref_id"]) == ("library", "queued", "library_file", record_id)
    assert (job["source_root"], job["source_rel"]) == ("tfiles", "Movies/Heat.1995.mkv")
    assert (job["dest_root"], job["dest_rel"]) == ("media", "Movies/Heat (1995)/Heat (1995).mkv")
    assert job["plan"]["op"] == "copy" and job["plan"]["revision"] == 1 and job["need_bytes"] >= 3 * 1024 ** 3
    assert [e["event_type"] for e in store.events_for(conn, record_id)] == ["migration_queued", "approval_approved"]
    assert rec_of(conn, record_id)["migration_status"] == v.MIGRATION_PENDING


def test_a_conversion_is_queued_as_a_convert_job_with_the_approved_plan(conn):
    record_id = convert_record(conn)
    approve(conn, record_id)
    job = jobs.get(conn, job_of(conn, record_id))
    assert job["plan"]["op"] == "convert" and job["plan"]["action"] == "remux_copy" and job["plan"]["change"]["op"] == "convert"
    assert job["dest_rel"].endswith(".mkv")


def test_approving_again_does_not_queue_a_second_job(conn):
    record_id = add(conn)
    approve(conn, record_id)
    assert migrate.queue(conn, record_id, CFG) is None
    assert conn.execute("SELECT COUNT(*) FROM job WHERE ref_type = 'library_file'").fetchone()[0] == 1


def test_rejecting_or_undoing_takes_a_waiting_job_back_but_not_a_running_one(conn):
    first = add(conn, rel="a.mkv", name="A (2000).mkv")
    approve(conn, first)
    rc.undo(conn, first, "Greg")
    conn.commit()
    assert jobs.get(conn, job_of(conn, first))["status"] == "cancelled" and rec_of(conn, first)["approval_status"] == "pending"

    second = add(conn, rel="b.mkv", name="B (2000).mkv")
    approve(conn, second)
    w = worker(conn)
    assert len(jobs.claim(conn, w, 1)) == 1
    with pytest.raises(v.StateError):
        rc.reject(conn, second, "Greg")


def test_the_record_follows_the_job_and_is_completed_only_when_the_worker_is_done(conn):
    record_id = add(conn, size=5_000_000)
    approve(conn, record_id)
    w = worker(conn)
    job_id = job_of(conn, record_id)
    assert migrate.queue_summary(conn)["queued"] == 1

    assert [j["id"] for j in jobs.claim(conn, w, 1)] == [job_id]
    migrate.sync_active(conn)
    rec = rec_of(conn, record_id)
    assert rec["migration_status"] == v.MIGRATION_IN_PROGRESS and rec["migration_started_at"]
    assert (migrate.queue_summary(conn)["queued"], migrate.queue_summary(conn)["running"]) == (0, 1)

    jobs.progress(conn, job_id, w["id"], stage="copy_out", progress=0.5)
    assert migrate.sync(conn, job_id) is False and rec_of(conn, record_id)["migration_status"] == v.MIGRATION_IN_PROGRESS

    jobs.complete(conn, job_id, w["id"], {"op": "copy", "verified": "sha256", "bytes": 5_000_000}, command="ffmpeg -i x")
    assert migrate.sync(conn, job_id) is True
    rec = rec_of(conn, record_id)
    assert (rec["migration_status"], rec["migration_verification_status"], rec["migrated_source_revision"]) == (v.MIGRATION_COMPLETED, "verified", 1)
    assert rec["migration_destination_path"] == "media:Movies/Heat (1995)/Heat (1995).mkv" and rec["migration_attempt_count"] == 1
    attempt = conn.execute("SELECT status, job_id, worker, command FROM library_attempt WHERE media_file_id = ?", (record_id,)).fetchone()
    assert tuple(attempt) == ("completed", job_id, "Worker-1", "ffmpeg -i x")
    assert migrate.sync(conn, job_id) is False and migrate.sync_active(conn) == 0           # nothing more to do the second time
    assert migrate.queue_summary(conn)["completed"] == 1
    assert migrate.queue(conn, record_id, CFG) is None                                     # already migrated at this revision


def test_a_failed_job_marks_the_record_failed_and_a_retry_puts_it_back_in_the_queue(conn):
    record_id = add(conn)
    approve(conn, record_id)
    w = worker(conn)
    job_id = job_of(conn, record_id)
    conn.execute("UPDATE job SET max_attempts = 1 WHERE id = ?", (job_id,))
    jobs.claim(conn, w, 1)
    assert jobs.fail(conn, job_id, w["id"], "transient", "share offline") == "failed"
    migrate.sync(conn, job_id)
    rec = rec_of(conn, record_id)
    assert rec["migration_status"] == v.MIGRATION_FAILED and "share offline" in rec["migration_last_error"]
    assert migrate.queue_summary(conn)["failed"] == 1
    jobs.retry(conn, job_id, "Greg")
    migrate.sync_active(conn)
    assert rec_of(conn, record_id)["migration_status"] == v.MIGRATION_PENDING and migrate.queue_summary(conn)["queued"] == 1


def test_a_job_held_for_review_shows_as_manual_review(conn):
    record_id = add(conn)
    approve(conn, record_id)
    w = worker(conn)
    job_id = job_of(conn, record_id)
    jobs.claim(conn, w, 1)
    jobs.needs_review(conn, job_id, w["id"], "output is shorter than the source")
    migrate.sync(conn, job_id)
    assert rec_of(conn, record_id)["migration_status"] == v.MIGRATION_MANUAL and migrate.queue_summary(conn)["waiting"] == 1


def test_approved_files_without_a_job_are_counted_and_can_be_queued_together(conn):
    ids = [add(conn, rel=f"{n}.mkv", name=f"M{n} (2000).mkv") for n in range(3)]
    for record_id in ids:                                              # approved the way it was before approvals queued work
        store.set_approval(conn, record_id, v.APPROVAL_APPROVED, "Greg")
    conn.commit()
    assert migrate.queue_summary(conn)["unqueued"] == 3
    assert migrate.queue_approved(conn, CFG, "Greg") == (3, [])
    conn.commit()
    summary = migrate.queue_summary(conn)
    assert (summary["queued"], summary["unqueued"]) == (3, 0)
    assert migrate.queue_approved(conn, CFG, "Greg") == (0, [])


def test_a_changed_file_withdraws_its_waiting_job_and_ignores_the_old_outcome(conn):
    record_id = add(conn, mtime=1_700_000_000.0)
    approve(conn, record_id)
    job_id = job_of(conn, record_id)
    newer = add(conn, mtime=1_800_000_000.0, run="run-2")
    assert newer == record_id
    assert rec_of(conn, record_id)["approval_status"] == "pending" and jobs.get(conn, job_id)["status"] == "cancelled"
    assert migrate.sync(conn, job_id) is False and rec_of(conn, record_id)["migration_status"] == v.MIGRATION_PENDING


def test_jellyfin_is_asked_to_scan_once_a_file_has_arrived(v2, monkeypatch):
    calls = []
    from synchub.clients import jellyfin
    monkeypatch.setattr(jellyfin.JellyfinClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(jellyfin.JellyfinClient, "refresh_library", lambda self: calls.append("refresh"))
    assert migrate.tell_jellyfin() is True and calls == ["refresh"]
    monkeypatch.setattr(jellyfin.JellyfinClient, "refresh_library", lambda self: (_ for _ in ()).throw(RuntimeError("down")))
    assert migrate.tell_jellyfin() is False                            # best effort: the file is in place either way
    monkeypatch.setattr(settings, "get", lambda key, *a, **k: "1" if key == "disable_jellyfin_connection" else "")
    assert migrate.tell_jellyfin() is False


def test_the_library_page_shows_the_queue_and_the_button_for_unqueued_files(client, conn):
    first, second = add(conn, rel="a.mkv", name="A (2000).mkv"), add(conn, rel="b.mkv", name="B (2000).mkv")
    approve(conn, first)
    store.set_approval(conn, second, v.APPROVAL_APPROVED, "Greg")
    conn.commit()
    page = client.get("/library/fragment").get_data(as_text=True)
    assert "1 in queue" in page and "0 running" in page and "1 approved file has no job yet" in page and "data-queue-approved" in page and 'data-active="1"' in page
    r = client.post("/api/library/queue-approved", json={})
    assert r.get_json()["ok"] and r.get_json()["queued"] == 1
    assert "2 in queue" in client.get("/library/fragment").get_data(as_text=True)
