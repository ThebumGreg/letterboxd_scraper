import json
import re

import pytest

from synchub import jobs, workers


def make_worker(v2, name="encoder-1", **kw):
    with v2.connection() as conn:
        return workers.create(conn, name, **kw)


def make_job(v2, **kw):
    with v2.connection() as conn:
        return jobs.create(conn, kw.pop("kind", "library"), title=kw.pop("title", "A job"), **kw)


def row_for(page, worker_id):
    return re.search(r'<tr data-worker="%d">.*?</tr>' % worker_id, page, re.S).group(0)


# ---------------------------------------------------------------- the page
def test_workers_page_lists_workers_and_jobs_and_is_in_the_nav(client, v2):
    worker_id, _ = make_worker(v2, tags=["gpu"], slots=2)
    make_job(v2, title="Heat", kind="library")
    page = client.get("/workers").get_data(as_text=True)
    assert 'href="/workers"' in page and 'id="fleet"' in page and "Add worker" in page
    row = row_for(page, worker_id)
    assert "encoder-1" in row and ">gpu<" in row and "0 / 2" in row and "Offline" in row and "never" in row
    assert "#1 Heat" in page and "queued" in page and "1 queued" in page
    assert "No workers yet" not in page and "No jobs yet" not in page
    assert client.get("/workers/fragment").get_data(as_text=True).count('id="workerTable"') == 1


def test_an_empty_fleet_says_what_to_do(client, v2):
    page = client.get("/workers").get_data(as_text=True)
    assert "No workers yet. Add one" in page and "No jobs yet" in page


def test_a_worker_shows_its_state_and_its_own_report(client, v2):
    worker_id, token = make_worker(v2, slots=2)
    status = {"work_free_bytes": 120 * 1024 ** 3, "originals_count": 3, "originals_bytes": 40 * 1024 ** 3}
    with v2.connection() as conn:
        workers.register(conn, worker_id, {"protocol": workers.PROTOCOL, "version": "1.2", "os": "Ubuntu 24.04", "hostname": "enc1", "slots": 2,
                                           "capabilities": {"encoders": ["libx265", "libx264"]}})
        workers.touch(conn, worker_id, status)
    row = row_for(client.get("/workers").get_data(as_text=True), worker_id)
    assert "Online" in row and "enc1" in row and "Ubuntu 24.04" in row and "1.2" in row and "libx265, libx264" in row
    assert "120.0 GB free" in row and "3 held, 40.0 GB" in row and "just now" in row
    for flag, label in (("paused", "Paused"), ("draining", "Draining"), ("enabled", "Disabled")):
        with v2.connection() as conn:
            workers.set_flag(conn, worker_id, flag, flag != "enabled")
        assert label in row_for(client.get("/workers").get_data(as_text=True), worker_id)
        with v2.connection() as conn:
            workers.set_flag(conn, worker_id, flag, flag == "enabled")
    with v2.connection() as conn:
        workers.revoke(conn, worker_id)
    assert "Revoked" in row_for(client.get("/workers").get_data(as_text=True), worker_id)


def test_names_titles_and_reasons_are_escaped(client, v2):
    make_worker(v2, '<b>bold</b> "q"')
    job_id = make_job(v2, title="<script>alert(1)</script>")
    with v2.connection() as conn:
        conn.execute("UPDATE job SET status = 'needs_review', review_reason = ? WHERE id = ?", ("<img src=x onerror=alert(1)>", job_id))
    page = client.get("/workers").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "<b>bold</b>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;img src=x onerror=alert(1)&gt;" in page


def test_a_running_job_shows_stage_percent_and_eta(client, v2):
    worker_id, _ = make_worker(v2)
    job_id = make_job(v2, title="Big film")
    with v2.connection() as conn:
        jobs.claim(conn, workers.get(conn, worker_id), 1)
        jobs.progress(conn, job_id, worker_id, stage="encode", progress=0.42, eta_seconds=5400)
    page = client.get("/workers").get_data(as_text=True)
    row = re.search(r'<tr data-job="%d">.*?</tr>' % job_id, page, re.S).group(0)
    assert "encode 42%" in row and "1 h 30 min left" in row and 'style="width: 42%"' in row and "encoder-1" in row and "1/3" in row


# ---------------------------------------------------------------- adding and changing workers
def test_adding_a_worker_shows_its_token_once_and_the_token_works(client, v2, app):
    r = client.post("/api/workers", json={"name": "  encoder-2 ", "tags": "gpu fast", "slots": 2}).get_json()
    assert r["ok"] and r["token"].startswith("sw_") and "Copy its token now" in r["message"]
    assert r["config"] == f"[synchub]\nurl = http://localhost\ntoken = {r['token']}\nname = encoder-2\n"
    with v2.connection() as conn:
        worker = workers.get(conn, r["id"])
    assert (worker["tags"], worker["slots"]) == (["gpu", "fast"], 2)
    api = app.test_client()
    assert api.post("/api/worker/v1/poll", json={"free_slots": 1}, headers={"Authorization": f"Bearer {r['token']}"}).status_code == 200
    for text in (client.get("/workers").get_data(as_text=True), json.dumps(client.get("/api/workers").get_json())):
        assert r["token"] not in text                                                  # never shown again, in any view
    assert "...%s" % r["token"][-4:] in client.get("/workers").get_data(as_text=True)    # only the last characters, to tell tokens apart


@pytest.mark.parametrize("body,message", [
    ({"name": ""}, "needs a name"), ({"name": "x", "slots": 99}, "Slots"), ({"name": "x", "tags": "bad!"}, "not a usable tag"),
    ({"name": "encoder-1"}, "already a worker called"), ({"name": "x" * 70}, "at most 60"),
])
def test_adding_a_worker_explains_what_is_wrong(client, v2, body, message):
    make_worker(v2)
    r = client.post("/api/workers", json=body)
    assert r.status_code == 400 and message in r.get_json()["message"]


def test_editing_a_worker(client, v2):
    worker_id, _ = make_worker(v2)
    assert client.post(f"/api/workers/{worker_id}/update", json={"name": "renamed", "tags": "a b", "slots": 4}).get_json()["ok"]
    with v2.connection() as conn:
        w = workers.get(conn, worker_id)
    assert (w["name"], w["tags"], w["slots"]) == ("renamed", ["a", "b"], 4)
    assert client.post(f"/api/workers/{worker_id}/update", json={"slots": 0}).status_code == 400
    assert client.post("/api/workers/999/update", json={"name": "x"}).status_code == 400


def test_worker_buttons(client, v2):
    worker_id, token = make_worker(v2)

    def flags():
        with v2.connection() as conn:
            w = workers.get(conn, worker_id)
        return {k: w[k] for k in ("paused", "draining", "enabled")}

    for action, expected in (("pause", {"paused": 1}), ("resume", {"paused": 0}), ("drain", {"draining": 1}), ("undrain", {"draining": 0}),
                             ("disable", {"enabled": 0}), ("enable", {"enabled": 1})):
        assert client.post(f"/api/workers/{worker_id}/{action}").get_json()["ok"], action
        assert all(flags()[k] == v for k, v in expected.items()), action
    assert client.post(f"/api/workers/{worker_id}/bogus").status_code == 404
    assert client.post("/api/workers/999/pause").status_code == 400


def test_new_token_replaces_the_old_and_revoke_ends_it(client, v2, app):
    worker_id, old = make_worker(v2)
    api = app.test_client()
    ask = lambda t: api.post("/api/worker/v1/poll", json={"free_slots": 0}, headers={"Authorization": f"Bearer {t}"}).status_code
    r = client.post(f"/api/workers/{worker_id}/rotate").get_json()
    assert r["ok"] and r["token"] != old and "name = encoder-1" in r["config"] and ask(old) == 401 and ask(r["token"]) == 200
    assert client.post(f"/api/workers/{worker_id}/revoke").get_json()["ok"] and ask(r["token"]) == 401
    assert client.post(f"/api/workers/{worker_id}/rotate").get_json()["ok"]              # a revoked worker can be let back in


def test_a_worker_with_a_running_job_cannot_be_removed(client, v2):
    worker_id, _ = make_worker(v2)
    job_id = make_job(v2)
    with v2.connection() as conn:
        jobs.claim(conn, workers.get(conn, worker_id), 1)
    r = client.post(f"/api/workers/{worker_id}/delete")
    assert r.status_code == 400 and "running a job" in r.get_json()["message"]
    with v2.connection() as conn:
        jobs.cancel(conn, job_id)
        jobs.fail(conn, job_id, worker_id, "cancelled", "stopped")
    assert client.post(f"/api/workers/{worker_id}/delete").get_json()["ok"]
    assert "No workers yet" in client.get("/workers").get_data(as_text=True) and "#1 A job" in client.get("/workers").get_data(as_text=True)


# ---------------------------------------------------------------- jobs
def test_job_buttons_follow_the_jobs_state(client, v2):
    worker_id, _ = make_worker(v2)
    queued = make_job(v2)
    assert "Cancelled." in client.post(f"/api/jobs/{queued}/cancel").get_json()["message"]
    assert client.post(f"/api/jobs/{queued}/cancel").status_code == 400                    # already cancelled
    assert client.post(f"/api/jobs/{queued}/retry").get_json()["ok"]
    with v2.connection() as conn:
        jobs.claim(conn, workers.get(conn, worker_id), 1)
    assert "stops at its next report" in client.post(f"/api/jobs/{queued}/cancel").get_json()["message"]
    assert client.post("/api/jobs/999/cancel").status_code == 400
    assert client.post(f"/api/jobs/{queued}/bogus").status_code == 404
    page = client.get("/workers").get_data(as_text=True)
    assert 'data-job-action="cancel"' in page


def waiting_job(v2, worker_id):
    """A job that a worker has handed to a person."""
    job_id = make_job(v2)
    with v2.connection() as conn:
        jobs.claim(conn, workers.get(conn, worker_id), 1)
        jobs.needs_review(conn, job_id, worker_id, "low confidence", {})
    return job_id


def test_review_decisions_from_the_page_record_who_decided(client, v2):
    worker_id, _ = make_worker(v2)
    with v2.connection() as conn:
        alice = conn.execute("INSERT INTO profile (name) VALUES ('Alice')").lastrowid
    approve, retry, skip, reject = waiting = [waiting_job(v2, worker_id) for _ in range(4)]
    page = client.get("/workers").get_data(as_text=True)
    assert page.count('data-review-job=') == 4 and 'data-reason="low confidence"' in page
    client.set_cookie("synchub_profile", str(alice))
    assert client.post(f"/api/jobs/{approve}/approve", json={"note": "looks fine"}).get_json()["message"] == "Approved: it carries on."
    assert client.post(f"/api/jobs/{retry}/retry", json={}).get_json()["ok"]
    assert "left as it is" in client.post(f"/api/jobs/{skip}/skip").get_json()["message"]
    assert client.post(f"/api/jobs/{reject}/reject").get_json()["message"] == "Rejected."
    with v2.connection() as conn:
        got = {i: jobs.get(conn, i) for i in waiting}
    assert [got[i]["status"] for i in waiting] == ["queued", "queued", "skipped", "cancelled"]
    assert got[approve]["decision"]["by"] == "Alice" and got[approve]["decision"]["note"] == "looks fine" and got[retry]["decision"]["action"] == "retry"
    assert client.post(f"/api/jobs/{approve}/approve").status_code == 400                    # no longer waiting


def test_a_guest_decision_is_recorded_as_guest(client, v2):
    worker_id, _ = make_worker(v2)
    job_id = waiting_job(v2, worker_id)
    client.post(f"/api/jobs/{job_id}/skip")                                                 # the test client is a guest
    with v2.connection() as conn:
        assert jobs.get(conn, job_id)["decision"]["by"] == "guest"


def test_job_details_include_every_attempt(client, v2):
    worker_id, _ = make_worker(v2)
    job_id = make_job(v2, source=("media", "Movies/Heat.avi"), dest=("jellyfin", "Movies/Heat (1995)/Heat (1995).mkv"))
    with v2.connection() as conn:
        jobs.claim(conn, workers.get(conn, worker_id), 1)
        jobs.fail(conn, job_id, worker_id, "transient", "share offline", log_tail="mount: timeout", command="ffmpeg -i in out")
    data = client.get(f"/api/jobs/{job_id}").get_json()
    job = data["job"]
    assert data["ok"] and job["source_root"] == "media" and job["attempts"] == 1 and job["error"] == "share offline"
    assert [(a["attempt_no"], a["worker_name"], a["outcome"], a["error_class"], a["log_tail"], a["command"]) for a in job["attempt_history"]] == \
        [(1, "encoder-1", "failed", "transient", "mount: timeout", "ffmpeg -i in out")]
    assert client.get("/api/jobs/999").status_code == 404


def test_the_json_listing_has_workers_jobs_and_counts(client, v2):
    make_worker(v2)
    make_job(v2)
    data = client.get("/api/workers").get_json()
    assert data["ok"] and [w["name"] for w in data["workers"]] == ["encoder-1"] and data["counts"] == {"queued": 1} and len(data["jobs"]) == 1
    assert data["workers"][0]["token_hash"] is True and "capabilities" in data["workers"][0]


# ---------------------------------------------------------------- the worker's own log
def test_the_workers_page_has_a_logs_button_and_a_viewer(client, v2):
    worker_id, _ = make_worker(v2)
    page = client.get("/workers").get_data(as_text=True)
    assert f'data-worker-logs="{worker_id}"' in row_for(page, worker_id) and 'id="workerLogModal"' in page and 'id="wlRefresh"' in page
    with v2.connection() as conn:
        workers.revoke(conn, worker_id)
    assert "data-worker-logs" not in row_for(client.get("/workers").get_data(as_text=True), worker_id)              # a revoked worker cannot answer


def test_asking_for_the_log_marks_the_request_and_says_whether_the_worker_is_there(client, v2):
    worker_id, _ = make_worker(v2)
    before = client.get(f"/api/workers/{worker_id}/log").get_json()
    assert before["ok"] and before["log"] is None and not before["pending"] and before["name"] == "encoder-1" and not before["online"]
    offline = client.post(f"/api/workers/{worker_id}/log", json={}).get_json()
    assert offline["ok"] and not offline["online"] and "offline" in offline["message"]
    assert client.get(f"/api/workers/{worker_id}/log").get_json()["pending"]
    with v2.connection() as conn:
        workers.touch(conn, worker_id, {})
    online = client.post(f"/api/workers/{worker_id}/log", json={}).get_json()
    assert online["online"] and online["message"] == "Asked the worker for its log."


def test_the_uploaded_log_is_shown_with_its_age_and_closes_the_request(client, v2):
    worker_id, _ = make_worker(v2)
    with v2.connection() as conn:
        workers.request_log(conn, worker_id)
        workers.store_log(conn, worker_id, '{"msg": "<b>hello</b>"}\n', truncated=True)
    data = client.get(f"/api/workers/{worker_id}/log").get_json()
    assert not data["pending"] and data["log"]["content"] == '{"msg": "<b>hello</b>"}\n' and data["log"]["truncated"] == 1 and data["log"]["received"]


def test_only_the_latest_log_is_kept_and_it_goes_with_the_worker(client, v2):
    worker_id, _ = make_worker(v2)
    with v2.connection() as conn:
        workers.store_log(conn, worker_id, "first")
        workers.store_log(conn, worker_id, "second")
        assert conn.execute("SELECT COUNT(*) FROM worker_log").fetchone()[0] == 1 and workers.stored_log(conn, worker_id)["content"] == "second"
        workers.delete(conn, worker_id)
        assert conn.execute("SELECT COUNT(*) FROM worker_log").fetchone()[0] == 0


def test_a_huge_log_is_cut_to_its_end(v2):
    worker_id, _ = make_worker(v2)
    with v2.connection() as conn:
        workers.store_log(conn, worker_id, "x" * (workers.MAX_LOG_CHARS + 500) + "END")
        stored = workers.stored_log(conn, worker_id)
    assert stored["truncated"] == 1 and stored["content"].startswith("[earlier lines left out]") and stored["content"].endswith("END") and len(stored["content"]) < workers.MAX_LOG_CHARS + 100


def test_a_log_cannot_be_asked_of_a_missing_or_revoked_worker(client, v2):
    assert client.post("/api/workers/99/log", json={}).status_code == 400 and client.get("/api/workers/99/log").status_code == 404
    worker_id, _ = make_worker(v2)
    with v2.connection() as conn:
        workers.revoke(conn, worker_id)
    r = client.post(f"/api/workers/{worker_id}/log", json={})
    assert r.status_code == 400 and "revoked" in r.get_json()["message"]


# ---------------------------------------------------------------- the Worker Status tab
HOST = {"arch": "x86_64", "kernel": "6.8.0-45-generic", "container": "docker", "memory_bytes": 32 * 1024 ** 3, "render_devices": ["renderD128"],
        "cpu": {"model": "Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz", "threads": 12, "cores": 6, "mhz": 4300, "flags": ["avx2", "aes"]},
        "gpus": [{"name": "NVIDIA GeForce GTX 1660", "driver": "550.54", "memory_mb": 6144, "source": "nvidia-smi"}],
        "ffmpeg": {"hwaccels": ["vaapi", "cuda"], "encoders_built": ["libx264", "libx265", "hevc_nvenc", "libaom-av1"]}}


def status_pane(page, worker_id):
    return re.search(r'<div class="panel[^"]*" data-status-worker="%d">.*?(?=<div class="panel[^"]*" data-status-worker=|\Z)' % worker_id, page, re.S).group(0)


def reporting_worker(v2, name="encoder-1", host=HOST, **status):
    worker_id, token = make_worker(v2, name)
    with v2.connection() as conn:
        workers.register(conn, worker_id, {"protocol": workers.PROTOCOL, "version": "0.4.0", "os": "Ubuntu 24.04", "hostname": "enc1", "slots": 2,
                                           "capabilities": {"cores": 12, "encoders": ["libx264", "libx265"], "ffmpeg": "ffmpeg version 6.1", "ffprobe": "ffprobe version 6.1",
                                                            "vmaf": True, "python": "3.12.3", "ops": ["copy", "scan"], "host": host}})
        workers.touch(conn, worker_id, {"uptime_seconds": 7200, "work_free_bytes": 500 * 1024 ** 3,
                                        "host": {"load": [0.5, 0.4, 0.3], "cpu_percent": 12.4, "mem_available_bytes": 20 * 1024 ** 3, "host_uptime_seconds": 90000}, **status})
    return worker_id


def test_the_status_tab_is_one_of_the_tabs_and_shows_the_hardware(client, v2):
    worker_id = reporting_worker(v2)
    page = client.get("/workers").get_data(as_text=True)
    assert 'data-pane="status"' not in page and "<h4 class=\"mb-3\">Machines" in page      # the hardware sits under the workers list, not in a tab of its own
    pane = status_pane(page, worker_id)
    for text in ("Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz", "6 cores, 12 threads", "4.30 GHz", "x86_64", "12%", "0.50 0.40 0.30", ">avx2<", "32.0 GB", "(20.0 GB free)", "500.0 GB free",
                 "1 d 1 h", "2 h 0 min", "NVIDIA GeForce GTX 1660", "6.0 GB", "driver 550.54", "renderD128", "6.8.0-45-generic", "docker container", "3.12.3", "copy, scan",
                 "ffmpeg version 6.1", "available", "vaapi", "cuda"):
        assert text in pane, text


def test_each_encoder_says_whether_it_works_failed_or_was_not_tested(client, v2):
    worker_id = reporting_worker(v2)
    pane = status_pane(client.get("/workers").get_data(as_text=True), worker_id)
    row = lambda name: re.search(r"<tr><td><code>%s</code>.*?</tr>" % re.escape(name), pane, re.S).group(0)
    assert "works" in row("libx264") and "H.264" in row("libx264") and "Software (CPU)" in row("libx264")
    assert "built in, but failed" in row("hevc_nvenc") and "NVIDIA NVENC" in row("hevc_nvenc") and "GPU" in row("hevc_nvenc")        # built into ffmpeg, but the test frame failed
    assert "built in, not tested" in row("libaom-av1")
    assert "<code>h264_vaapi</code>" not in pane                                                                    # not in this ffmpeg at all: not listed
    assert pane.index("<code>libx264</code>") < pane.index("<code>hevc_nvenc</code>")                                  # working encoders first


def test_an_older_agent_is_told_to_update_and_still_shows_its_encoders(client, v2):
    worker_id, _ = make_worker(v2)
    with v2.connection() as conn:
        workers.register(conn, worker_id, {"protocol": workers.PROTOCOL, "version": "0.3.0", "slots": 1, "capabilities": {"encoders": ["libx264"]}})
    pane = status_pane(client.get("/workers").get_data(as_text=True), worker_id)
    assert "has not reported its hardware" in pane and "setup-worker.sh" in pane and "Encoders that work: libx264" in pane


def test_a_worker_that_never_connected_says_so(client, v2):
    worker_id, _ = make_worker(v2)
    assert "has not connected yet" in status_pane(client.get("/workers").get_data(as_text=True), worker_id)


def test_an_offline_worker_keeps_its_last_known_figures_marked_as_old(client, v2):
    worker_id = reporting_worker(v2)
    with v2.connection() as conn:
        conn.execute("UPDATE worker SET last_seen = '2020-01-01 00:00:00' WHERE id = ?", (worker_id,))
    pane = status_pane(client.get("/workers").get_data(as_text=True), worker_id)
    assert "Offline" in pane and "the figures below are from then" in pane and "Intel(R) Core" in pane and "opacity-75" in pane


def test_host_text_from_a_worker_is_escaped(client, v2):
    host = {**HOST, "cpu": {**HOST["cpu"], "model": "<script>alert(1)</script>"}, "gpus": [{"name": "<img src=x onerror=alert(1)>"}]}
    worker_id = reporting_worker(v2, host=host)
    page = client.get("/workers").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<img src=x" not in page


def test_nonsense_in_the_report_does_not_break_the_page(client, v2):
    host = {"cpu": "fast", "gpus": "none", "ffmpeg": [1, 2], "memory_bytes": "lots", "render_devices": 3}
    worker_id = reporting_worker(v2, host=host)
    r = client.get("/workers")
    assert r.status_code == 200 and f'data-status-worker="{worker_id}"' in r.get_data(as_text=True)
    api = client.get("/api/workers").get_json()
    assert api["ok"] and api["workers"][0]["host"]["known"]


def test_a_gpu_free_container_explains_why_no_card_is_shown(client, v2):
    worker_id = reporting_worker(v2, host={**HOST, "gpus": [], "render_devices": []})
    pane = status_pane(client.get("/workers").get_data(as_text=True), worker_id)
    assert "No graphics card is visible to the agent" in pane and "docker container" in pane
