"""The Work drive panel: the agent measures its work folder and deletes job folders on request; SyncHub shows it and protects what must not go."""
import json

import pytest

from synchub import jobs, workdrive, workers

from .agent_module import agent
from .test_agent_live import rig, wait          # noqa: F401  (rig is a fixture)


def job_folder(rig, size, outcome=None, sent=True, age_days=0, title="A film"):
    """A job row (so SyncHub knows it) and its folder on the worker. Returns (job id, folder)."""
    import os
    import time
    status = {"done": "done", "failed": "failed", "review": "needs_review", None: "failed"}[outcome]
    with rig.db.connection() as conn:
        job_id = jobs.create(conn, "library", title=title, source=("media", "a.mkv"), dest=("tfiles", "out/a.mkv"), plan={"op": "copy"}, pinned_worker_id=rig.worker_id)
        conn.execute("UPDATE job SET status = ? WHERE id = ?", (status, job_id))
    folder = rig.work / "jobs" / f"{job_id}-{job_id:08x}"
    (folder / "output").mkdir(parents=True)
    (folder / "output" / "big.mkv").write_bytes(b"x" * size)
    manifest = {"version": agent.MANIFEST_VERSION, "title": title, "outcome": outcome, "state": "finished" if outcome else "running", "report": {"kind": "complete", "payload": {}, "sent": sent} if outcome else None,
                "keep_until": "2099-01-01T00:00:00+00:00" if outcome else None}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    old = time.time() - age_days * 86400
    os.utime(folder, (old, old))
    return job_id, folder


def known(rig):
    with rig.db.connection() as conn:
        view = workdrive.overview(conn, rig.worker_id)
    return view if view["known"] else None


def test_the_worker_measures_its_work_folder_and_names_each_job_folder(rig):
    ok_id, _ = job_folder(rig, 5000, "done")
    bad_id, _ = job_folder(rig, 9000, "failed", age_days=5)
    (rig.work / "stray.bin").write_bytes(b"y" * 700)                                  # not SyncHub's: counted as Other, never deleted
    rig.start()
    view = wait(lambda: known(rig), what="the first measurement")
    by_id = {j["job_id"]: j for j in view["jobs"]}
    assert by_id[bad_id]["bytes"] >= 9000 and by_id[bad_id]["state"] == "failed" and by_id[ok_id]["state"] == "finished" and by_id[bad_id]["age_days"] >= 4.9
    assert [j["job_id"] for j in view["jobs"]][0] == bad_id and view["other_bytes"] >= 700 and view["jobs_count"] == 2
    assert view["bulk"]["finished"]["count"] == 1 and view["bulk"]["failed_old"]["count"] == 1 and view["bulk"]["failed_old"]["bytes"] >= 9000


def test_a_requested_job_folder_is_deleted_and_bulk_actions_take_only_what_they_cover(rig):
    done_id, done = job_folder(rig, 5000, "done")
    _, old = job_folder(rig, 9000, "failed", age_days=5)
    _, fresh = job_folder(rig, 4000, "failed", age_days=1)
    _, waiting = job_folder(rig, 4000, "review", age_days=9)                       # waiting for a person: never in a bulk action
    rig.start()
    wait(lambda: known(rig), what="the first measurement")
    with rig.db.connection() as conn:
        assert workdrive.request(conn, rig.worker_id, which="failed_old")["count"] == 1
        conn.commit()
    wait(lambda: not old.exists(), what="the old failed folder to go")
    assert done.exists() and fresh.exists() and waiting.exists()
    with rig.db.connection() as conn:
        assert workdrive.request(conn, rig.worker_id, ids=[done_id])["count"] == 1
        conn.commit()
    wait(lambda: not done.exists(), what="the chosen folder to go")
    assert fresh.exists() and waiting.exists()
    wait(lambda: (known(rig) or {}).get("pending") == 0 and done_id not in [j["job_id"] for j in known(rig)["jobs"]], what="the list to follow")


def test_a_running_job_or_an_unreported_result_is_never_deleted(rig):
    mid_id, mid = job_folder(rig, 3000, "done", sent=False)
    rig.start()
    wait(lambda: known(rig), what="the first measurement")
    view = known(rig)
    item = next(j for j in view["jobs"] if j["job_id"] == mid_id)
    assert item["can_delete"] is False and "result" in item["why_not"]
    with rig.db.connection() as conn:
        with pytest.raises(workdrive.WorkDriveError):
            workdrive.request(conn, rig.worker_id, ids=[mid_id])
        with pytest.raises(workdrive.WorkDriveError):
            workdrive.request(conn, rig.worker_id, ids=[999])
        with pytest.raises(workdrive.WorkDriveError):
            workdrive.request(conn, rig.worker_id, which="everything")
    assert mid.exists()


def test_the_agent_itself_refuses_a_folder_that_is_running_or_unreported(rig):
    a, done = job_folder(rig, 100, "done", sent=False)
    b, sent = job_folder(rig, 100, "done")
    instance = rig.start()
    assert instance.purge_folder(a) is False and done.exists()
    instance.active[b] = object()
    assert instance.purge_folder(b) is False and sent.exists()
    del instance.active[b]
    assert instance.purge_folder(b) is True and not sent.exists()


def test_the_work_drive_page_data_and_purge_endpoint(rig, client, conn):
    one, _ = job_folder(rig, 2000, "done")
    rig.start()
    wait(lambda: known(rig), what="the first measurement")
    data = client.get(f"/api/workers/{rig.worker_id}/work").get_json()
    assert data["ok"] and data["known"] and data["jobs"][0]["job_id"] == one and data["bulk"]["finished"]["count"] == 1
    assert client.post(f"/api/workers/{rig.worker_id}/purge", json={"ids": []}).status_code == 400
    answer = client.post(f"/api/workers/{rig.worker_id}/purge", json={"which": "finished"}).get_json()
    assert answer["ok"] and answer["count"] == 1
    assert client.get("/api/workers/9999/work").status_code == 404
    wait(lambda: not list((rig.work / "jobs").glob(f"{one}-*")), what="the folder to go")
