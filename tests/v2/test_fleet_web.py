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
    assert "#1 Heat" in page and "queued" in page and "<small class=\"text-muted\">1 queued</small>" in page
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
    assert "Online" in row and "enc1" in row and "Ubuntu 24.04" in row and "agent 1.2" in row and "libx265, libx264" in row
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
