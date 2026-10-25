"""The download pipeline's SyncHub side: an import webhook becomes a scan, a decision and a replace-in-place job, and ends with Radarr/Sonarr told to rescan."""
import json

import pytest

from synchub import downloads, jobs, settings, storage
from synchub.library import migrate, scan, store

from .library_helpers import add, plan
from .test_library_findings import rec_of

RADARR = {"eventType": "Download", "movie": {"id": 7, "title": "Heat", "folderPath": "/movies/Heat (1995)"},
          "movieFile": {"id": 1, "relativePath": "Heat.1995.avi", "path": "/movies/Heat (1995)/Heat.1995.avi", "size": 1000}}
SONARR = {"eventType": "Download", "series": {"id": 3, "title": "Show", "path": "/tv/Show"},
          "episodeFile": {"id": 5, "relativePath": "Season 01/Show - S01E01.mkv", "path": "/tv/Show/Season 01/Show - S01E01.mkv", "size": 500}}


@pytest.fixture
def pipe(conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "radarr_path": "/movies", "sonarr_path": "/tv"})
    settings.set_value("download_pipeline", "review", conn=conn)
    conn.commit()
    return conn


def intake(conn, payload=RADARR, service="radarr"):
    ids = downloads.record_event(conn, service, "Download", payload)
    conn.commit()
    return ids


def analysed(conn, rel="Heat (1995)/Heat.1995.avi", **kw):
    kw.setdefault("container", "avi")
    kw.setdefault("video", "mpeg4")
    kw.setdefault("action", "remux_copy")
    kw.setdefault("proposals", [plan(convert={"remux": True}, keep_audio=(1,))])
    return add(conn, root="media", rel=rel, name="Heat (1995).mkv", **kw)


def status(conn, intake_id):
    return downloads.get(conn, intake_id)["status"]


def test_nothing_is_recorded_while_the_pipeline_is_off(pipe):
    settings.set_value("download_pipeline", "off", conn=pipe)
    assert intake(pipe) == []
    assert pipe.execute("SELECT COUNT(*) FROM download_import").fetchone()[0] == 0


def test_an_import_is_mapped_to_a_share_with_the_path_the_service_sees(pipe):
    (first,) = intake(pipe)
    row = downloads.get(pipe, first)
    assert (row["root_name"], row["relative_path"], row["status"], row["service_item_id"]) == ("media", "Heat (1995)/Heat.1995.avi", "pending", 7)
    (second,) = intake(pipe, SONARR, "sonarr")
    assert downloads.get(pipe, second)["relative_path"] == "Show/Season 01/Show - S01E01.mkv"


def test_an_import_no_share_covers_is_blocked_with_the_reason(pipe):
    payload = {**RADARR, "movieFile": {**RADARR["movieFile"], "path": "/elsewhere/Heat.avi"}}
    (made,) = intake(pipe, payload)
    row = downloads.get(pipe, made)
    assert row["status"] == "blocked" and "Storage page" in row["message"]


def test_other_events_and_repeats_are_ignored(pipe):
    assert downloads.record_event(pipe, "radarr", "Grab", RADARR) == []
    assert len(intake(pipe)) == 1 and intake(pipe) == []              # the same file reported twice while it is being handled


def test_a_pending_import_starts_one_scan_of_its_folder_for_all_files_in_it(pipe):
    payload = {**RADARR, "movieFile": {**RADARR["movieFile"], "path": "/movies/Heat (1995)/Heat.1995.avi"}}
    first = intake(pipe, payload)[0]
    other = {**payload, "movieFile": {**payload["movieFile"], "path": "/movies/Heat (1995)/Heat.1995.extra.avi"}}
    second = intake(pipe, other)[0]
    downloads.process(pipe)
    pipe.commit()
    rows = [downloads.get(pipe, i) for i in (first, second)]
    assert {r["status"] for r in rows} == {"scanning"} and rows[0]["scan_run_id"] == rows[1]["scan_run_id"]
    run_jobs = scan.run_jobs(pipe, rows[0]["scan_run_id"])
    assert len(run_jobs) == 1 and run_jobs[0]["source_rel"] == "Heat (1995)"


def finish_scan(conn, intake_id):
    run = downloads.get(conn, intake_id)["scan_run_id"]
    for job in scan.run_jobs(conn, run):
        conn.execute("UPDATE job SET status = 'done' WHERE id = ?", (job["id"],))
    conn.commit()
    downloads.process(conn)
    conn.commit()


def test_review_mode_waits_for_approval_then_queues_an_in_place_job(pipe):
    (made,) = intake(pipe)
    downloads.process(pipe)
    record_id = analysed(pipe)
    finish_scan(pipe, made)
    row = downloads.get(pipe, made)
    assert row["status"] == "awaiting_approval" and row["record_id"] == record_id
    job_id = downloads.approve(pipe, made, "Greg")
    pipe.commit()
    job = jobs.get(pipe, job_id)
    assert job["kind"] == "download" and (job["source_root"], job["source_rel"]) == ("media", "Heat (1995)/Heat.1995.avi")
    assert job["dest_rel"] == "Heat (1995)/Heat.1995.mkv"                    # same base name, new container
    assert job["plan"]["replace"] is True and job["plan"]["op"] == "convert" and job["plan"]["originals_retention_days"] == 30
    assert job["plan"]["source_size"] == 1_000_000 and job["policy"] == {"originals_retention_days": 30}
    assert status(pipe, made) == "queued"


def test_auto_mode_queues_a_safe_plan_and_still_asks_about_a_lossy_one(pipe):
    settings.set_value("download_pipeline", "auto", conn=pipe)
    (safe,) = intake(pipe)
    downloads.process(pipe)
    analysed(pipe)
    finish_scan(pipe, safe)
    assert status(pipe, safe) == "queued"

    payload = {**RADARR, "movieFile": {**RADARR["movieFile"], "path": "/movies/Other (2000)/Other.avi"}}
    (lossy,) = intake(pipe, payload)
    downloads.process(pipe)
    analysed(pipe, rel="Other (2000)/Other.avi", proposals=[plan(convert={"remux": True, "video": "transcode"}, keep_audio=(1,))])
    finish_scan(pipe, lossy)
    row = downloads.get(pipe, lossy)
    assert row["status"] == "awaiting_approval" and "lossy" in row["message"]


def test_a_file_that_needs_nothing_is_left_alone(pipe):
    (made,) = intake(pipe)
    downloads.process(pipe)
    analysed(pipe, action="copy_rename", proposals=None, container="matroska,webm", video="h264")
    finish_scan(pipe, made)
    assert status(pipe, made) == "nothing_to_do"


def test_a_file_the_scan_could_not_read_is_blocked(pipe):
    (made,) = intake(pipe)
    downloads.process(pipe)
    analysed(pipe, status="failed", unreadable="cannot open")
    finish_scan(pipe, made)
    row = downloads.get(pipe, made)
    assert row["status"] == "blocked" and "cannot open" in row["message"]


def test_the_library_does_not_queue_a_competing_job_for_a_file_being_replaced(pipe):
    (made,) = intake(pipe)
    downloads.process(pipe)
    record_id = analysed(pipe)
    finish_scan(pipe, made)
    downloads.approve(pipe, made, "Greg")
    pipe.commit()
    from synchub.library import recommend as rc, vocab as v
    store.set_approval(pipe, record_id, v.APPROVAL_APPROVED, "Greg", action="remux_copy", accept_track_changes=True)
    assert migrate.queue(pipe, record_id, scan.config(pipe)) is None


def test_the_import_follows_its_job_to_replaced_and_the_service_is_asked_to_rescan(pipe, monkeypatch):
    (made,) = intake(pipe)
    downloads.process(pipe)
    analysed(pipe)
    finish_scan(pipe, made)
    job_id = downloads.approve(pipe, made, "Greg")
    pipe.execute("UPDATE job SET status = 'running' WHERE id = ?", (job_id,))
    assert downloads.sync(pipe, job_id) is False and status(pipe, made) == "converting"
    pipe.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?", (json.dumps({"bytes": 900, "replaced": True, "kept_original": True}), job_id))
    assert downloads.sync(pipe, job_id) is True
    pipe.commit()
    row = downloads.get(pipe, made)
    assert row["status"] == "replaced" and row["rescan_status"] == "due" and "original is kept" in row["message"]

    calls = []

    class Fake:
        def rescan_movie(self, movie_id):
            calls.append(movie_id)

    import synchub.clients.arr as arr
    monkeypatch.setattr(arr, "RadarrClient", Fake)
    assert downloads.rescan_due() == 1 and calls == [7]
    assert downloads.get(pipe, made)["rescan_status"] == "done" and downloads.rescan_due() == 0


def test_a_failed_rescan_is_recorded_and_can_be_asked_again(pipe, monkeypatch):
    (made,) = intake(pipe)
    pipe.execute("UPDATE download_import SET status = 'replaced', rescan_status = 'due' WHERE id = ?", (made,))
    pipe.commit()
    import synchub.clients.arr as arr

    class Broken:
        def rescan_movie(self, movie_id):
            raise RuntimeError("Radarr is down")

    monkeypatch.setattr(arr, "RadarrClient", Broken)
    downloads.rescan_due()
    row = downloads.get(pipe, made)
    assert row["rescan_status"] == "failed" and "down" in row["rescan_message"]
    downloads.ask_rescan_again(pipe, made)
    assert downloads.get(pipe, made)["rescan_status"] == "due"


def test_our_own_replacement_coming_back_as_an_import_is_not_processed_again(pipe):
    (made,) = intake(pipe)
    pipe.execute("UPDATE download_import SET status = 'replaced', result_json = ?, created_at = '2020-01-01 00:00:00' WHERE id = ?", (json.dumps({"bytes": 1000}), made))
    pipe.commit()
    assert intake(pipe) == []


def test_a_cancelled_or_failed_job_is_shown_and_can_be_looked_at_again(pipe):
    (made,) = intake(pipe)
    downloads.process(pipe)
    analysed(pipe)
    finish_scan(pipe, made)
    job_id = downloads.approve(pipe, made, "Greg")
    pipe.execute("UPDATE job SET status = 'failed', error = 'ffmpeg exploded' WHERE id = ?", (job_id,))
    downloads.sync(pipe, job_id)
    row = downloads.get(pipe, made)
    assert row["status"] == "failed" and "exploded" in row["message"]
    downloads.retry(pipe, made)
    assert status(pipe, made) == "pending"


def test_the_webhook_notes_the_import(app, client, conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "radarr_path": "/movies"})
    settings.set_value("download_pipeline", "review", conn=conn)
    conn.commit()
    assert client.post("/api/webhook/radarr", json=RADARR).status_code == 200
    assert conn.execute("SELECT status FROM download_import").fetchone()[0] == "pending"


def test_the_page_lists_imports_and_takes_decisions(app, client, conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "radarr_path": "/movies"})
    settings.set_value("download_pipeline", "review", conn=conn)
    (made,) = downloads.record_event(conn, "radarr", "Download", RADARR)
    conn.execute("UPDATE download_import SET status = 'awaiting_approval', message = 'Waiting for your approval.' WHERE id = ?", (made,))
    conn.commit()
    page = client.get("/library").get_data(as_text=True)
    assert "New downloads" in page and "Heat.1995.avi" in page and "Replace it" in page
    assert client.post(f"/api/downloads/{made}/skip").get_json()["ok"] is True
    assert client.post(f"/api/downloads/{made}/approve").get_json()["ok"] is False
    assert client.post("/api/library/settings", json={"download_pipeline": "auto", "originals_retention_days": "7"}).get_json()["ok"] is True
    assert (settings.get("download_pipeline"), settings.get("originals_retention_days")) == ("auto", "7")
    assert client.post("/api/library/settings", json={"download_pipeline": "bogus"}).get_json()["ok"] is False
