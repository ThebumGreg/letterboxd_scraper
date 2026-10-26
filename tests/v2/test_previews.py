"""Preview clips: when a compression waits for a person to watch one, how the clips are stored and followed, and what each answer does."""
import io
import json
import os

import pytest

from synchub import downloads, jobs, previews, settings, storage, workers
from synchub.library import size_policy
from synchub.library import vocab as v

from .library_helpers import add

GIB = 1024 ** 3
API = "/api/worker/v1"


@pytest.fixture
def pipe(conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "radarr_path": "/movies"})
    for key, value in (("download_pipeline", "auto"), ("size_policy", "1")):
        settings.set_value(key, value, conn=conn)
    conn.commit()
    return conn


def big_download(conn, name="Heat", size_gb=45, **extra):
    """An imported 45 GB film, scanned and analysed, with the pipeline's decision made. Returns (import id, record id)."""
    payload = {"eventType": "Download", "movie": {"id": 7, "title": name, "folderPath": f"/movies/{name} (1995)"},
               "movieFile": {"path": f"/movies/{name} (1995)/{name}.1995.mkv", "size": size_gb * GIB}}
    (made,) = downloads.record_event(conn, "radarr", "Download", payload)
    downloads.process(conn)
    record_id = add(conn, root="media", rel=f"{name} (1995)/{name}.1995.mkv", name=f"{name} (1995).mkv", size=size_gb * GIB, action="copy_rename", **extra)
    conn.execute("UPDATE library_file SET duration_seconds = 7200, media_type = 'Movie/Unknown' WHERE id = ?", (record_id,))
    run = downloads.get(conn, made)["scan_run_id"]
    for job in conn.execute("SELECT id FROM job WHERE ref_type = ?", ("library_run:" + run,)).fetchall():
        conn.execute("UPDATE job SET status = 'done' WHERE id = ?", (job["id"],))
    conn.commit()
    downloads.process(conn)
    conn.commit()
    return made, record_id


def run_preview(conn, preview_id, result=None, clips=("encoded", "source")):
    """A worker takes the preview job, uploads the clips and completes it."""
    row = previews.get(conn, preview_id)
    worker_id, _ = workers.create(conn, f"w{preview_id}")
    conn.execute("UPDATE job SET status = 'running', worker_id = ? WHERE id = ?", (worker_id, row["job_id"]))
    for name in clips:
        previews.store_clip(conn, row["job_id"], worker_id, name, io.BytesIO(b"x" * 2048))
    conn.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?",
                 (json.dumps(result or {"start": 600.0, "length": 45.0, "encoded_video_kbps": 12000, "source_video_kbps": 40000, "encoded_bytes": 2048, "source_clip_bytes": 2048, "crf": 22}), row["job_id"]))
    previews.sync(conn, row["job_id"])
    conn.commit()


# ---------------------------------------------------------------- when a person has to watch
def test_a_compression_waits_for_a_preview_by_default_and_one_is_made_for_it(pipe):
    made, record_id = big_download(pipe)
    row = downloads.get(pipe, made)
    assert row["status"] == "awaiting_approval" and "preview" in row["message"].lower()
    preview = previews.latest_for(pipe, "download_import", made)
    assert preview["status"] == "queued" and preview["record_id"] == record_id and preview["signature"] == "1080p|h264|sdr|8bit|40-50GB"
    job = jobs.get(pipe, preview["job_id"])
    assert job["kind"] == "preview" and job["plan"]["op"] == "preview" and "preview" in job["plan"]["needs"] and "profiles" in job["plan"]["needs"]
    assert job["plan"]["profile"]["video_codec"] == "hevc" and job["plan"]["seconds"] == 45 and job["plan"]["change"]["video"]["action"] == "transcode"
    assert (job["source_root"], job["source_rel"]) == ("media", "Heat (1995)/Heat.1995.mkv") and job["dest_root"] is None


def test_never_compresses_without_asking_and_the_preview_can_be_switched_off(pipe):
    settings.set_value("review_first", "never", conn=pipe)
    pipe.commit()
    made, _ = big_download(pipe)
    assert downloads.get(pipe, made)["status"] == "queued" and previews.latest_for(pipe, "download_import", made) is None


def test_no_automatic_preview_when_that_is_switched_off_but_the_file_still_waits(pipe):
    settings.set_value("preview_auto", "0", conn=pipe)
    pipe.commit()
    made, _ = big_download(pipe)
    assert downloads.get(pipe, made)["status"] == "awaiting_approval" and previews.latest_for(pipe, "download_import", made) is None
    preview_id = previews.create(pipe, "download_import", made, "greg")                    # still available on demand
    assert previews.get(pipe, preview_id)["status"] == "queued"


def test_until_trusted_stops_asking_after_k_similar_approvals(pipe):
    settings.set_value("review_first", "until_trusted", conn=pipe)
    settings.set_value("trust_after", "2", conn=pipe)
    pipe.commit()
    for name in ("Heat", "Ronin"):
        made, _ = big_download(pipe, name)
        assert downloads.get(pipe, made)["status"] == "awaiting_approval"
        preview = previews.latest_for(pipe, "download_import", made)
        run_preview(pipe, preview["id"])
        previews.decide(pipe, preview["id"], "approve", "greg")
        pipe.commit()
    third, _ = big_download(pipe, "Alien")
    assert downloads.get(pipe, third)["status"] == "queued"                                 # two like it were approved: no more questions
    other, _ = big_download(pipe, "Dune", size_gb=80)
    assert downloads.get(pipe, other)["status"] == "awaiting_approval"                      # a different size band is a different kind of file


def test_low_confidence_only_asks_for_hdr_and_files_close_to_the_quality_floor(pipe):
    settings.set_value("review_first", "low_confidence_only", conn=pipe)
    pipe.commit()
    plain, _ = big_download(pipe, "Heat")
    assert downloads.get(pipe, plain)["status"] == "queued"
    hdr = json.dumps({"hdr": "hdr10", "bit_depth": 10, "transfer": "smpte2084", "width": 3840, "height": 2160})
    hdr_made, record_id = big_download(pipe, "Dune")
    assert downloads.get(pipe, hdr_made)["status"] in ("queued", "awaiting_approval")
    rec = {"video_info_json": hdr, "size_bytes": 60 * GIB, "duration_seconds": 7200, "media_type": "Movie/Unknown", "streams_json": "[]", "resolution": "3840x2160", "frame_rate": 24.0}
    decision = size_policy.decide(rec, [], size_policy.load(pipe))
    assert decision.profile and decision.profile.get("hdr") and previews.must_review_first(pipe, rec, decision)


# ---------------------------------------------------------------- making one
def test_a_preview_is_refused_for_a_file_the_size_rules_leave_alone(pipe):
    small = add(pipe, root="media", rel="Small/Small.mkv", name="Small (2000).mkv", size=2 * GIB)
    with pytest.raises(previews.PreviewError, match="nothing to preview"):
        previews.create(pipe, "library_file", small, "greg")


def test_only_one_preview_is_made_at_a_time_for_a_file(pipe):
    big = add(pipe, root="media", rel="Big/Big.mkv", name="Big (2001).mkv", size=45 * GIB)
    pipe.execute("UPDATE library_file SET duration_seconds = 7200, media_type = 'Movie/Unknown' WHERE id = ?", (big,))
    previews.create(pipe, "library_file", big, "greg")
    with pytest.raises(previews.PreviewError, match="already being made"):
        previews.create(pipe, "library_file", big, "greg")


def test_the_preview_follows_its_job_and_needs_both_clips(pipe):
    made, _ = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    run_preview(pipe, preview["id"], clips=("encoded",))
    assert previews.get(pipe, preview["id"])["status"] == "failed"                          # the source clip never arrived
    previews.decide(pipe, preview["id"], "another", "greg")
    pipe.commit()
    newest = previews.latest_for(pipe, "download_import", made)
    assert newest["id"] != preview["id"] and newest["mode"] == "worst"
    run_preview(pipe, newest["id"])
    ready = previews.describe(previews.get(pipe, newest["id"]))
    assert ready["status"] == "ready" and ready["expires_at"] and ready["files"] == {"encoded": True, "source": True}
    assert ready["predicted_gb"] and ready["ratio"] == 0.3


def test_a_clip_is_only_accepted_from_the_worker_running_the_job_and_within_the_limit(pipe, monkeypatch):
    made, _ = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    worker_id, _ = workers.create(pipe, "w1")
    other_id, _ = workers.create(pipe, "w2")
    pipe.execute("UPDATE job SET status = 'running', worker_id = ? WHERE id = ?", (worker_id, preview["job_id"]))
    with pytest.raises(jobs.JobConflict):
        previews.store_clip(pipe, preview["job_id"], other_id, "encoded", io.BytesIO(b"x"))
    with pytest.raises(jobs.JobError, match="Unknown clip"):
        previews.store_clip(pipe, preview["job_id"], worker_id, "../escape", io.BytesIO(b"x"))
    monkeypatch.setattr(previews, "MAX_CLIP_BYTES", 1000)
    with pytest.raises(jobs.JobError, match="limit"):
        previews.store_clip(pipe, preview["job_id"], worker_id, "encoded", io.BytesIO(b"x" * 5000))
    assert not os.path.exists(previews.clip_path(preview["id"], "encoded") + ".part") and not os.path.exists(previews.clip_path(preview["id"], "encoded"))


# ---------------------------------------------------------------- what each answer does
def test_approving_queues_the_compression_and_counts_toward_trust(pipe):
    made, _ = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    with pytest.raises(previews.PreviewError, match="not ready"):
        previews.decide(pipe, preview["id"], "approve", "greg")
    run_preview(pipe, preview["id"])
    previews.decide(pipe, preview["id"], "approve", "greg")
    pipe.commit()
    row = downloads.get(pipe, made)
    assert row["status"] == "queued" and jobs.get(pipe, row["job_id"])["plan"]["profile"]["video_codec"] == "hevc"
    assert previews.approved_count(pipe, preview["signature"]) == 1
    with pytest.raises(previews.PreviewError, match="already answered"):
        previews.decide(pipe, preview["id"], "approve", "greg")


def test_a_different_target_is_used_for_that_film_only_and_does_not_count_toward_trust(pipe):
    made, record_id = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    run_preview(pipe, preview["id"])
    previews.decide(pipe, preview["id"], "approve", "greg", target_gb=30)
    pipe.commit()
    job = jobs.get(pipe, downloads.get(pipe, made)["job_id"])
    assert job["plan"]["size_policy"]["target_gb"] == 30 and job["plan"]["profile"]["target_bytes"] == 30 * GIB
    assert previews.approved_count(pipe, preview["signature"]) == 0
    assert pipe.execute("SELECT size_target_gb FROM library_file WHERE id = ?", (record_id,)).fetchone()[0] == 30


def test_trying_a_target_makes_a_new_clip_at_that_target(pipe):
    made, record_id = big_download(pipe)
    first = previews.latest_for(pipe, "download_import", made)
    run_preview(pipe, first["id"])
    previews.decide(pipe, first["id"], "retarget", "greg", target_gb=35)
    pipe.commit()
    second = previews.latest_for(pipe, "download_import", made)
    assert second["id"] != first["id"] and second["target_gb"] == 35
    assert jobs.get(pipe, second["job_id"])["plan"]["profile"]["target_bytes"] == 35 * GIB
    assert previews.get(pipe, first["id"])["decision"] == "retarget" and downloads.get(pipe, made)["status"] == "awaiting_approval"
    with pytest.raises(previews.PreviewError):
        previews.decide(pipe, second["id"], "retarget", "greg", target_gb="lots")


def test_skip_and_reject_leave_the_file_and_reject_forgets_the_trust_earned(pipe):
    made, _ = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    previews._trust(pipe, preview["signature"], delta=2)
    run_preview(pipe, preview["id"])
    previews.decide(pipe, preview["id"], "reject", "greg")
    assert downloads.get(pipe, made)["status"] == "skipped" and previews.approved_count(pipe, preview["signature"]) == 0
    other, _ = big_download(pipe, "Ronin")
    second = previews.latest_for(pipe, "download_import", other)
    previews.decide(pipe, second["id"], "skip", "greg")
    assert downloads.get(pipe, other)["status"] == "skipped" and previews.get(pipe, second["id"])["decision"] == "skip"


def test_a_changed_file_forgets_the_target_chosen_for_it(pipe):
    from synchub.library import store
    _, record_id = big_download(pipe)
    previews.set_target(pipe, record_id, 20)
    assert store._RESET_APPROVAL["size_target_gb"] is None


def test_signatures_group_files_by_kind():
    base = {"video_codec": "h264", "resolution": "1920x1080", "size_bytes": 45 * GIB, "video_info_json": None}
    assert previews.signature(base) == "1080p|h264|sdr|8bit|40-50GB"
    assert previews.signature({**base, "video_codec": "h265", "size_bytes": 55 * GIB, "video_info_json": json.dumps({"hdr": "hdr10", "bit_depth": 10, "height": 2160})}) == "4k|hevc|hdr10|10bit|50-60GB"


# ---------------------------------------------------------------- tidying
def test_old_and_answered_previews_lose_their_clips_and_stray_folders_go(pipe):
    made, _ = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    run_preview(pipe, preview["id"])
    stray = os.path.join(previews.root_folder(), "999")
    os.makedirs(stray)
    pipe.execute("UPDATE preview SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (preview["id"],))
    pipe.commit()
    assert previews.purge() == 1
    assert previews.get(pipe, preview["id"])["status"] == "expired" and not os.path.exists(previews.folder(preview["id"])) and not os.path.exists(stray)


# ---------------------------------------------------------------- the worker API and the pages
def test_the_worker_uploads_clips_over_http_and_completing_makes_the_preview_ready(pipe, app, client):
    made, _ = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    worker_id, token = workers.create(pipe, "encoder-1")
    pipe.execute("UPDATE job SET status = 'running', worker_id = ? WHERE id = ?", (worker_id, preview["job_id"]))
    pipe.commit()
    headers = {"Authorization": f"Bearer {token}"}
    http = app.test_client()
    assert http.post(f"{API}/jobs/{preview['job_id']}/preview/encoded", data=b"E" * 3000, headers=headers, content_type="video/mp4").get_json()["bytes"] == 3000
    assert http.post(f"{API}/jobs/{preview['job_id']}/preview/source", data=b"S" * 4000, headers=headers, content_type="video/mp4").status_code == 200
    assert http.post(f"{API}/jobs/{preview['job_id']}/preview/other", data=b"x", headers=headers).status_code == 400
    assert http.post(f"{API}/jobs/{preview['job_id']}/preview/encoded", data=b"x").status_code == 401
    done = http.post(f"{API}/jobs/{preview['job_id']}/complete", json={"result": {"start": 100.0, "length": 45, "encoded_video_kbps": 9000, "source_video_kbps": 30000}}, headers=headers)
    assert done.status_code == 200
    page = client.get(f"/library/previews/{preview['id']}")
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "Original" in text and "Compressed" in text and "9000 kbps" in text and "Looks good" in text
    clip = client.get(f"/library/previews/{preview['id']}/clip/encoded", headers={"Range": "bytes=0-9"})
    assert clip.status_code == 206 and clip.data == b"E" * 10 and clip.mimetype == "video/mp4"
    assert client.get(f"/library/previews/{preview['id']}/clip/nope").status_code == 404


def test_the_pages_and_buttons_work(pipe, client):
    made, record_id = big_download(pipe)
    preview = previews.latest_for(pipe, "download_import", made)
    library = client.get("/library").get_data(as_text=True)
    assert f"/library/previews/{preview['id']}" in library
    assert "Preview clips before compressing" in library and 'id="setReviewFirst"' in library
    saved = client.post("/api/library/settings", json={"review_first": "until_trusted", "trust_after": "5", "preview_seconds": "40", "preview_auto": "0"}).get_json()
    assert saved["ok"] and previews.review_first() == "until_trusted" and previews.trust_after() == 5 and previews.clip_seconds() == 40 and not previews.auto_make()
    assert client.post("/api/library/settings", json={"preview_seconds": "300"}).get_json()["ok"] is False
    assert client.post("/api/library/settings", json={"review_first": "sometimes"}).get_json()["ok"] is False
    record = client.get(f"/library/record/{record_id}").get_data(as_text=True)
    assert "Preview the compression" in record
    waiting = client.get(f"/library/previews/{preview['id']}").get_data(as_text=True)
    assert "Waiting for a worker" in waiting
    reply = client.post("/api/library/previews", json={"subject_type": "library_file", "subject_id": record_id}).get_json()
    assert reply["ok"] and reply["url"].startswith("/library/previews/")
    assert client.post("/api/library/previews", json={"subject_type": "library_file", "subject_id": record_id}).get_json()["message"].startswith("A preview of this file is already")
    assert client.post("/api/library/previews", json={"subject_id": "x"}).get_json()["ok"] is False
    assert client.post(f"/api/library/previews/{preview['id']}/explode").status_code == 404
    assert client.post(f"/api/library/previews/{preview['id']}/approve", json={}).get_json()["ok"] is False          # clips not ready
    assert client.get("/library/previews/99999").status_code == 302
