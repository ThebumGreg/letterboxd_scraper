import logging

import pytest

from synchub import jobs, workers

API = "/api/worker/v1"


@pytest.fixture
def fleet(v2, app):
    """A worker with a token, its API client, and the means to queue jobs."""
    with v2.connection() as conn:
        worker_id, token = workers.create(conn, "encoder-1", tags=["fast"], slots=2)
    client = app.test_client()

    class Fleet:
        id, name = worker_id, "encoder-1"
        headers = {"Authorization": f"Bearer {token}"}
        raw_token = token

        def call(self, path, body=None, headers=None):
            return client.post(f"{API}{path}", json=body if body is not None else {}, headers=self.headers if headers is None else headers)

        def queue(self, kind="library", **kw):
            with v2.connection() as conn:
                return jobs.create(conn, kind, title=kw.pop("title", "A job"), **kw)

        def job(self, job_id):
            with v2.connection() as conn:
                return jobs.get(conn, job_id)

        def poll(self, **body):
            return self.call("/poll", {"free_slots": 2, **body}).get_json()

    return Fleet()


# ---------------------------------------------------------------- who may call
def test_every_call_needs_a_valid_token(fleet):
    for headers in ({}, {"Authorization": "Bearer"}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic " + fleet.raw_token}, {"Authorization": fleet.raw_token}):
        for path in ("/register", "/poll", "/jobs/1/progress", "/jobs/1/complete", "/jobs/1/fail", "/jobs/1/needs_review", "/jobs/1/cleaned"):
            r = fleet.call(path, headers=headers)
            assert r.status_code == 401 and r.get_json() == {"ok": False, "message": "The worker token was not accepted."}, (path, headers)


def test_a_disabled_worker_is_told_so_and_a_revoked_token_is_unknown(fleet, v2):
    with v2.connection() as conn:
        workers.set_flag(conn, fleet.id, "enabled", False)
    r = fleet.call("/poll")
    assert r.status_code == 403 and "disabled" in r.get_json()["message"]
    with v2.connection() as conn:
        workers.set_flag(conn, fleet.id, "enabled", True)
        workers.revoke(conn, fleet.id)
    assert fleet.call("/poll").status_code == 401


def test_the_api_is_not_behind_the_profile_chooser_and_only_accepts_post(fleet, v2, app):
    with v2.connection() as conn:
        conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    stranger = app.test_client()                                                    # has not chosen a profile
    assert stranger.get("/").status_code == 302
    assert stranger.post(f"{API}/poll", json={"free_slots": 1}, headers=fleet.headers).status_code == 200
    assert stranger.get(f"{API}/poll", headers=fleet.headers).status_code == 405


# ---------------------------------------------------------------- register and poll
def test_register_returns_the_settings_an_agent_needs(fleet, v2):
    info = {"protocol": workers.PROTOCOL, "version": "1.0", "os": "Ubuntu 24.04", "hostname": "enc1", "slots": 2, "capabilities": {"encoders": ["libx265"]}}
    r = fleet.call("/register", info).get_json()
    assert r == {"ok": True, "worker_id": fleet.id, "name": "encoder-1", "protocol": workers.PROTOCOL, "poll_seconds": 5, "lease_seconds": jobs.LEASE_SECONDS, "roots": []}
    with v2.connection() as conn:
        assert workers.get(conn, fleet.id)["capabilities"]["encoders"] == ["libx265"] and workers.get(conn, fleet.id)["online"]


def test_an_agent_with_the_wrong_protocol_is_told_to_update(fleet):
    r = fleet.call("/register", {"protocol": 99})
    assert r.status_code == 426 and "Update the agent" in r.get_json()["message"] and r.get_json()["protocol"] == workers.PROTOCOL


def test_poll_hands_out_jobs_up_to_the_free_slots_and_stores_the_status(fleet, v2):
    ids = [fleet.queue(source=("media", f"Movies/{n}.avi"), dest=("jellyfin", f"Movies/{n}.mkv"), plan={"op": "remux"}) for n in range(3)]
    got = fleet.poll(free_slots=1, status={"work_free_bytes": 5 * 1024 ** 3, "originals_count": 2})
    assert got["ok"] and [j["id"] for j in got["jobs"]] == ids[:1] and got["jobs"][0]["source"] == {"root": "media", "rel": "Movies/0.avi"}
    assert (got["paused"], got["draining"], got["poll_seconds"], got["lease_seconds"], got["cancel"], got["cleanup"]) == (False, False, 5, jobs.LEASE_SECONDS, [], [])
    with v2.connection() as conn:
        assert workers.get(conn, fleet.id)["status"] == {"work_free_bytes": 5 * 1024 ** 3, "originals_count": 2}
    assert [j["id"] for j in fleet.poll(free_slots=5)["jobs"]] == ids[1:2]               # the worker's own two slots cap it, one is busy
    assert fleet.poll(free_slots=0)["jobs"] == []


def test_poll_without_saying_what_is_free_gets_nothing(fleet):
    fleet.queue()
    assert fleet.call("/poll", {}).get_json()["jobs"] == []


def test_a_job_waits_for_enough_free_space(fleet):
    fleet.queue(need_bytes=100 * 1024 ** 3)
    assert fleet.poll(free_bytes=10 * 1024 ** 3)["jobs"] == []
    assert fleet.poll(free_bytes="lots")["jobs"] != []                                  # not a number: treated as unknown, not as zero


def test_poll_tells_a_paused_or_draining_worker_so_and_gives_it_no_work(fleet, v2):
    fleet.queue()
    with v2.connection() as conn:
        workers.set_flag(conn, fleet.id, "paused", True)
        workers.set_flag(conn, fleet.id, "draining", True)
    got = fleet.poll()
    assert got["paused"] is True and got["draining"] is True and got["jobs"] == []


def test_poll_brings_cancel_and_cleanup_requests(fleet, v2):
    first, second = fleet.queue(), fleet.queue()
    assert [j["id"] for j in fleet.poll()["jobs"]] == [first, second]
    with v2.connection() as conn:
        jobs.cancel(conn, first, "Alice")
        jobs.needs_review(conn, second, fleet.id, "unsure", {})
        jobs.decide(conn, second, "reject", "Alice")
    got = fleet.poll(free_slots=0)
    assert got["cancel"] == [first] and got["cleanup"] == [second]
    assert fleet.call(f"/jobs/{second}/cleaned").get_json() == {"ok": True}
    assert fleet.poll(free_slots=0)["cleanup"] == []


def test_a_restarted_agent_that_does_not_hold_a_job_loses_it_to_the_queue(fleet, v2):
    kept, forgotten = fleet.queue(), fleet.queue()
    assert [j["id"] for j in fleet.poll()["jobs"]] == [kept, forgotten]
    with v2.connection() as conn:
        conn.execute("UPDATE job SET claimed_at = '2000-01-01 00:00:00'")             # handed out long ago
    got = fleet.poll(free_slots=0, holding=[kept])
    assert got["jobs"] == [] and fleet.job(kept)["status"] == "running"
    assert fleet.job(forgotten)["status"] == "queued" and fleet.job(forgotten)["error_class"] == "worker_lost"
    assert fleet.poll(free_slots=0)["jobs"] == []                                      # no list given: nothing is judged


# ---------------------------------------------------------------- following a job
def test_a_job_from_claim_to_completion(fleet):
    job_id = fleet.queue()
    assert fleet.poll()["jobs"][0]["id"] == job_id
    r = fleet.call(f"/jobs/{job_id}/progress", {"stage": "encode", "progress": 0.25, "fps": 30, "speed": 1.2, "eta_seconds": 900, "message": "pass 1"})
    assert r.get_json() == {"ok": True, "cancel": False, "lease_seconds": jobs.LEASE_SECONDS}
    job = fleet.job(job_id)
    assert (job["stage"], job["progress"], job["fps"], job["eta_seconds"], job["message"]) == ("encode", 0.25, 30.0, 900, "pass 1")
    assert fleet.call(f"/jobs/{job_id}/complete", {"result": {"size_after": 123}, "command": "ffmpeg -i in out"}).get_json() == {"ok": True}
    job = fleet.job(job_id)
    assert (job["status"], job["result"]) == ("done", {"size_after": 123})


def test_progress_after_cancel_says_stop(fleet, v2):
    job_id = fleet.queue()
    fleet.poll()
    with v2.connection() as conn:
        jobs.cancel(conn, job_id, "Alice")
    assert fleet.call(f"/jobs/{job_id}/progress", {"stage": "encode"}).get_json()["cancel"] is True
    assert fleet.call(f"/jobs/{job_id}/fail", {"error_class": "cancelled", "message": "stopped"}).get_json() == {"ok": True, "outcome": "cancelled"}


def test_fail_applies_the_retry_policy_and_says_what_became_of_the_job(fleet):
    job_id = fleet.queue(max_attempts=2)
    fleet.poll()
    r = fleet.call(f"/jobs/{job_id}/fail", {"error_class": "transient", "message": "share offline", "log_tail": "mount: timeout", "command": "ffmpeg x"}).get_json()
    assert r == {"ok": True, "outcome": "queued"}
    assert fleet.job(job_id)["status"] == "queued"
    fleet.call("/poll", {"free_slots": 1})                                             # (back-off keeps it queued for a minute)
    assert fleet.job(job_id)["not_before"]


def test_a_worker_can_stop_for_a_decision_and_the_answer_comes_back_with_the_job(fleet, v2):
    job_id = fleet.queue()
    fleet.poll()
    r = fleet.call(f"/jobs/{job_id}/needs_review", {"reason": "bits per pixel too low", "detail": {"bpp": 0.03}})
    assert r.get_json() == {"ok": True}
    job = fleet.job(job_id)
    assert (job["status"], job["review_reason"], job["review_detail"]) == ("needs_review", "bits per pixel too low", {"bpp": 0.03})
    assert fleet.poll()["jobs"] == []
    with v2.connection() as conn:
        jobs.decide(conn, job_id, "approve", "Alice", "fine")
    again = fleet.poll()["jobs"][0]
    assert again["id"] == job_id and again["decision"]["action"] == "approve" and again["decision"]["note"] == "fine"


def test_reports_about_someone_elses_or_a_finished_job_are_refused_with_stop(fleet, v2, app):
    with v2.connection() as conn:
        _, other_token = workers.create(conn, "other")
    job_id = fleet.queue()
    fleet.poll()
    other = {"Authorization": f"Bearer {other_token}"}
    for path in ("progress", "complete", "fail", "needs_review"):
        r = fleet.call(f"/jobs/{job_id}/{path}", {"error_class": "transient", "message": "x"}, headers=other)
        assert r.status_code == 409 and r.get_json()["stop"] is True and "no longer yours" in r.get_json()["message"], path
    fleet.call(f"/jobs/{job_id}/complete")
    r = fleet.call(f"/jobs/{job_id}/progress", {"stage": "encode"})
    assert r.status_code == 409 and "it is done" in r.get_json()["message"]
    assert fleet.call("/jobs/9999/progress").status_code == 409


def test_bad_input_is_a_clear_400_not_a_crash(fleet):
    job_id = fleet.queue()
    fleet.poll()
    assert fleet.call(f"/jobs/{job_id}/progress", {"stage": "dancing"}).status_code == 400
    assert fleet.call(f"/jobs/{job_id}/progress", {"progress": "lots"}).status_code == 400
    assert fleet.call(f"/jobs/{job_id}/progress", None).status_code == 200                # an empty body is just a heartbeat
    assert fleet.call(f"/jobs/{job_id}/fail", {"message": "x" * 5000}).status_code == 200  # long text is cut, not refused


def test_the_log_records_what_happened_to_jobs(fleet, caplog):
    caplog.set_level(logging.INFO)
    job_id = fleet.queue(title="Heat")
    fleet.poll()
    fleet.call(f"/jobs/{job_id}/fail", {"error_class": "transient", "message": "share offline"})
    text = caplog.text
    assert f"job {job_id} (library: Heat) went to encoder-1, attempt 1" in text and "failed on encoder-1 (transient): share offline -> queued" in text


# ---------------------------------------------------------------- phase 2: shares, resuming and logs
def add_shares(v2, *names):
    from synchub import storage
    folders = {"media": "Media", "tfiles": "TFiles"}
    with v2.connection() as conn:
        for name in names:
            storage.create(conn, {"name": name, "protocol": "smb", "host": "tower", "share": folders[name], "mount_point": f"/mnt/tower/{folders[name]}"})


def test_register_tells_the_agent_which_shares_to_find_and_how_to_recognise_them(fleet, v2):
    add_shares(v2, "media", "tfiles")
    info = {"protocol": workers.PROTOCOL, "slots": 1}
    roots = fleet.call("/register", info).get_json()["roots"]
    assert roots == [{"name": "media", "mount_point": "/mnt/tower/Media", "marker": ".synchub-root", "marker_text": "media"},
                     {"name": "tfiles", "mount_point": "/mnt/tower/TFiles", "marker": ".synchub-root", "marker_text": "tfiles"}]


def test_a_restarted_agent_is_handed_back_the_jobs_it_was_running(fleet):
    job_id = fleet.queue(plan={"op": "copy"})
    first = fleet.poll(active=[])
    assert [j["id"] for j in first["jobs"]] == [job_id] and first["resume"] == []             # new work is new work, not a resume
    again = fleet.poll(active=[])                                                             # the agent has no thread for it: it restarted
    assert [j["id"] for j in again["resume"]] == [job_id] and again["resume"][0]["plan"] == {"op": "copy"} and again["resume"][0]["attempt"] == 1
    assert fleet.poll(active=[job_id])["resume"] == []                                         # it is working on it
    assert fleet.poll()["resume"] == []                                                       # an agent that does not say gets nothing extra


def test_only_this_workers_running_jobs_are_resumed(fleet, v2, app):
    job_id = fleet.queue()
    fleet.poll(active=[])
    with v2.connection() as conn:
        _, token = workers.create(conn, "encoder-2")
    r = app.test_client().post(f"{API}/poll", json={"free_slots": 1, "active": []}, headers={"Authorization": f"Bearer {token}"}).get_json()
    assert r["resume"] == [] and r["jobs"] == []
    assert fleet.job(job_id)["status"] == "running"


def test_a_job_is_only_given_to_a_worker_whose_shares_are_ready(fleet, v2):
    add_shares(v2, "media", "tfiles")
    job_id = fleet.queue(source=("media", "a.mkv"), dest=("tfiles", "a.mkv"))
    ready = {"ok": True, "state": "ok"}
    assert [j["id"] for j in fleet.poll()["jobs"]] == [job_id]                                 # shares not reported yet: trusted
    with v2.connection() as conn:
        conn.execute("UPDATE job SET status = 'queued', worker_id = NULL WHERE id = ?", (job_id,))
    assert fleet.poll(status={"roots": {"media": ready, "tfiles": {"ok": False, "state": "not_mounted"}}})["jobs"] == []
    assert fleet.poll(status={"roots": {"media": ready}})["jobs"] == []                          # a share it does not mention is not ready
    assert [j["id"] for j in fleet.poll(status={"roots": {"media": ready, "tfiles": ready}})["jobs"]] == [job_id]


def test_a_job_with_no_shares_ignores_what_the_worker_says_about_them(fleet, v2):
    job_id = fleet.queue()
    assert [j["id"] for j in fleet.poll(status={"roots": {"media": {"ok": False, "state": "not_mounted"}}})["jobs"]] == [job_id]


def test_the_full_log_is_asked_for_and_uploaded(fleet, v2):
    job_id = fleet.queue()
    fleet.poll()
    with v2.connection() as conn:
        assert jobs.request_log(conn, job_id) == fleet.id
    assert fleet.poll()["log_requests"] == [job_id]
    assert fleet.call(f"/jobs/{job_id}/log", {"text": "line one\nline two\n"}).get_json() == {"ok": True}
    assert fleet.poll()["log_requests"] == []
    with v2.connection() as conn:
        log = jobs.stored_log(conn, job_id)
    assert log["content"] == "line one\nline two\n" and log["worker_name"] == "encoder-1" and not log["truncated"]
    fleet.call(f"/jobs/{job_id}/log", {"text": "newer"})
    with v2.connection() as conn:
        assert jobs.stored_log(conn, job_id)["content"] == "newer"                              # the latest replaces the last


def test_a_log_can_be_fetched_for_a_finished_job_from_the_worker_that_ran_it(fleet, v2):
    job_id = fleet.queue()
    fleet.poll()
    fleet.call(f"/jobs/{job_id}/complete", {"result": {}})
    with v2.connection() as conn:
        assert jobs.request_log(conn, job_id) == fleet.id
    assert fleet.poll()["log_requests"] == [job_id]


def test_a_log_is_refused_from_a_worker_that_never_had_the_job_and_for_a_job_nobody_ran(fleet, v2):
    job_id = fleet.queue()
    with v2.connection() as conn:
        with pytest.raises(jobs.JobError, match="No worker has run"):
            jobs.request_log(conn, job_id)
        with pytest.raises(jobs.JobError, match="no longer exists"):
            jobs.request_log(conn, 999)
        _, token = workers.create(conn, "encoder-2")
    r = fleet.call(f"/jobs/{job_id}/log", {"text": "x"})
    assert r.status_code == 400 and "never been this worker's" in r.get_json()["message"]
    assert fleet.call("/jobs/999/log", {"text": "x"}).status_code == 400


def test_a_huge_log_keeps_its_end(fleet, v2):
    job_id = fleet.queue()
    fleet.poll()
    fleet.call(f"/jobs/{job_id}/log", {"text": "a" * 1_500_000 + "THE END"})
    with v2.connection() as conn:
        log = jobs.stored_log(conn, job_id)
    assert log["truncated"] and log["content"].endswith("THE END") and len(log["content"]) < 1_100_000 and log["content"].startswith("[earlier lines left out]")


def test_the_log_endpoint_needs_the_token(fleet):
    assert fleet.call("/jobs/1/log", {"text": "x"}, headers={}).status_code == 401
