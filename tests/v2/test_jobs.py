import pytest

from synchub import jobs, workers

T0 = "2026-01-01 12:00:00"


def later(seconds, base=T0):
    return jobs._later(base, seconds)


def worker(conn, name="w1", tags=(), slots=1):
    worker_id, _ = workers.create(conn, name, tags, slots)
    return workers.get(conn, worker_id)


def queue(conn, kind="library", **kw):
    return jobs.create(conn, kind, title=kw.pop("title", f"{kind} job"), now=T0, **kw)


def run_one(conn, w, now=T0, **kw):
    """Claims the single queued job for a worker and returns its id."""
    got = jobs.claim(conn, w, 1, now=now, **kw)
    assert len(got) == 1, got
    return got[0]["id"]


# ---------------------------------------------------------------- creating and payload
def test_create_validates_kind_and_defaults_priority_by_kind(conn):
    with pytest.raises(jobs.JobError, match="Unknown job kind"):
        jobs.create(conn, "bogus")
    ids = {k: queue(conn, k) for k in jobs.KINDS}
    assert {k: jobs.get(conn, i)["priority"] for k, i in ids.items()} == jobs.PRIORITY
    custom = jobs.create(conn, "library", priority=7, plan={"op": "remux"}, policy={"retention_days": 30}, required_tags=["b", "a", "a"], now=T0)
    got = jobs.get(conn, custom)
    assert (got["priority"], got["plan"], got["policy"], got["required_tags"], got["status"], got["attempts"]) == (7, {"op": "remux"}, {"retention_days": 30}, ["a", "b"], "queued", 0)


def test_what_a_worker_is_told_about_a_job(conn):
    w = worker(conn)
    jobs.create(conn, "library", title="Heat", source=("media", "Movies/Heat.avi"), dest=("jellyfin", "Movies/Heat (1995)/Heat (1995).mkv"),
                plan={"op": "convert"}, policy={"retention_days": 30}, now=T0)
    scan = jobs.create(conn, "scan", title="Scan", priority=999, now=T0)
    first, second = jobs.claim(conn, worker(conn, "w2", slots=2), 2, now=T0)
    assert first["id"] == scan and first["source"] is None and first["dest"] is None and first["attempt"] == 1
    assert second["source"] == {"root": "media", "rel": "Movies/Heat.avi"} and second["dest"] == {"root": "jellyfin", "rel": "Movies/Heat (1995)/Heat (1995).mkv"}
    assert (second["kind"], second["title"], second["plan"], second["policy"], second["decision"]) == ("library", "Heat", {"op": "convert"}, {"retention_days": 30}, None)


# ---------------------------------------------------------------- who gets what
def test_highest_priority_first_then_oldest(conn):
    w = worker(conn, slots=4)
    low, high_old, high_new, mid = queue(conn, "library"), queue(conn, "download"), queue(conn, "download"), queue(conn, "scan")
    assert [j["id"] for j in jobs.claim(conn, w, 4, now=T0)] == [high_old, high_new, mid, low]


def test_slots_are_limited_by_the_workers_own_count_and_what_it_says_is_free(conn):
    w = worker(conn, slots=2)
    for _ in range(5):
        queue(conn)
    assert len(jobs.claim(conn, w, 10, now=T0)) == 2                                           # its slot count caps it
    assert jobs.claim(conn, w, 10, now=T0) == []                                               # both slots busy
    other = worker(conn, "w2", slots=4)
    assert len(jobs.claim(conn, other, 1, now=T0)) == 1                                        # the worker's own free count caps it too
    assert jobs.claim(conn, other, 0, now=T0) == [] and jobs.claim(conn, other, None, now=T0) == []


def test_disabled_paused_and_draining_workers_get_nothing(conn):
    w = worker(conn)
    queue(conn)
    for flag in ("paused", "draining", "enabled"):
        workers.set_flag(conn, w["id"], flag, flag != "enabled")
        assert jobs.claim(conn, workers.get(conn, w["id"]), 1, now=T0) == [], flag
        workers.set_flag(conn, w["id"], flag, flag == "enabled")
    assert len(jobs.claim(conn, workers.get(conn, w["id"]), 1, now=T0)) == 1


def test_a_paused_worker_keeps_what_it_has(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    workers.set_flag(conn, w["id"], "paused", True)
    assert jobs.progress(conn, job_id, w["id"], stage="encode", progress=0.5, now=T0)["cancel"] is False
    assert jobs.get(conn, job_id)["status"] == "running"


def test_pinned_jobs_go_only_to_their_worker(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    pinned = queue(conn, pinned_worker_id=b["id"])
    assert jobs.claim(conn, a, 1, now=T0) == []
    assert [j["id"] for j in jobs.claim(conn, b, 1, now=T0)] == [pinned]


def test_required_tags_must_all_be_offered_by_the_worker(conn):
    plain, gpu, both = worker(conn, "plain"), worker(conn, "gpu", tags=["gpu"]), worker(conn, "both", tags=["gpu", "fast"])
    needs = queue(conn, required_tags=["gpu", "fast"])
    assert jobs.claim(conn, plain, 1, now=T0) == [] and jobs.claim(conn, gpu, 1, now=T0) == []
    assert [j["id"] for j in jobs.claim(conn, both, 1, now=T0)] == [needs]


def test_a_job_waits_for_a_worker_with_enough_free_space(conn):
    w = worker(conn)
    big = queue(conn, need_bytes=80 * 10 ** 9)
    assert jobs.claim(conn, w, 1, free_bytes=50 * 10 ** 9, now=T0) == []
    assert jobs.claim(conn, w, 1, free_bytes=None, now=T0)[0]["id"] == big                      # unknown free space does not block


def test_a_retry_waits_out_its_back_off(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    jobs.fail(conn, job_id, w["id"], "transient", "share offline", now=T0)
    assert jobs.get(conn, job_id)["not_before"] == later(60)
    assert jobs.claim(conn, w, 1, now=later(59)) == []
    assert jobs.claim(conn, w, 1, now=later(60))[0]["attempt"] == 2


def test_the_worker_that_holds_the_files_has_first_call(conn):
    holder, other = worker(conn, "holder"), worker(conn, "other")
    job_id = run_one(conn, queue(conn) and holder)
    jobs.fail(conn, job_id, holder["id"], "worker_lost", "restart", keep_state=True, now=T0)
    assert jobs.get(conn, job_id)["preferred_worker_id"] == holder["id"]
    assert jobs.claim(conn, other, 1, now=later(10)) == []                                      # still the holder's
    assert jobs.claim(conn, other, 1, now=later(jobs.AFFINITY_SECONDS + 1))[0]["id"] == job_id  # the holder never came back


def test_two_workers_never_get_the_same_job(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    only = queue(conn)
    assert [j["id"] for j in jobs.claim(conn, a, 1, now=T0)] == [only]
    assert jobs.claim(conn, b, 1, now=T0) == []
    assert conn.execute("SELECT COUNT(*) FROM job_attempt WHERE job_id = ?", (only,)).fetchone()[0] == 1


def test_a_claim_that_loses_the_race_is_skipped_not_double_booked(conn, monkeypatch):
    a, b = worker(conn, "a"), worker(conn, "b")
    first, second = queue(conn), queue(conn)
    real = jobs._next_attempt_no

    def rival_takes_the_first(c, job_id):
        if job_id == first:
            conn.execute("UPDATE job SET status = 'running', worker_id = ? WHERE id = ?", (b["id"], first))
        return real(c, job_id)
    monkeypatch.setattr(jobs, "_next_attempt_no", rival_takes_the_first)
    assert [j["id"] for j in jobs.claim(conn, a, 1, now=T0)] == [second]
    assert jobs.get(conn, first)["worker_id"] == b["id"]


# ---------------------------------------------------------------- following a job
def test_progress_keeps_the_lease_alive_and_clamps_nonsense(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.get(conn, job_id)["lease_until"] == later(jobs.LEASE_SECONDS)
    got = jobs.progress(conn, job_id, w["id"], stage="encode", progress=1.7, fps=23.9, speed=1.4, eta_seconds=600, message="x" * 500, now=later(30))
    assert got == {"cancel": False, "lease_seconds": jobs.LEASE_SECONDS}
    job = jobs.get(conn, job_id)
    assert (job["stage"], job["progress"], job["fps"], job["speed"], job["eta_seconds"], len(job["message"])) == ("encode", 1.0, 23.9, 1.4, 600, 300)
    assert job["lease_until"] == later(30 + jobs.LEASE_SECONDS)
    jobs.progress(conn, job_id, w["id"], progress=float("nan"), now=later(40))                  # not a number: ignored, not stored
    assert jobs.get(conn, job_id)["progress"] == 1.0
    jobs.progress(conn, job_id, w["id"], message="later", now=later(50))
    assert jobs.get(conn, job_id)["stage"] == "encode"                                          # fields not sent keep their value
    with pytest.raises(jobs.JobError, match="Unknown stage"):
        jobs.progress(conn, job_id, w["id"], stage="dancing")
    with pytest.raises(jobs.JobError, match="numbers"):
        jobs.progress(conn, job_id, w["id"], progress="lots")


def test_only_the_owner_of_a_running_job_may_report_on_it(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    job_id = run_one(conn, queue(conn) and a)
    for call in (lambda: jobs.progress(conn, job_id, b["id"]), lambda: jobs.complete(conn, job_id, b["id"]),
                 lambda: jobs.fail(conn, job_id, b["id"], "transient", "x"), lambda: jobs.needs_review(conn, job_id, b["id"], "x")):
        with pytest.raises(jobs.JobConflict, match="no longer yours"):
            call()
    jobs.complete(conn, job_id, a["id"], now=T0)
    with pytest.raises(jobs.JobConflict, match="it is done"):
        jobs.progress(conn, job_id, a["id"])
    with pytest.raises(jobs.JobConflict, match="no longer exists"):
        jobs.progress(conn, 999, a["id"])


def test_completing_a_job_records_the_result_and_closes_the_attempt(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    jobs.complete(conn, job_id, w["id"], {"size_before": 10, "size_after": 4, "verified": True}, command="ffmpeg -i x", now=later(120))
    job = jobs.detail(conn, job_id)
    assert (job["status"], job["stage"], job["progress"], job["lease_until"], job["finished_at"]) == ("done", "done", 1.0, None, later(120))
    assert job["result"] == {"size_before": 10, "size_after": 4, "verified": True} and job["worker_id"] == w["id"]
    attempt = job["attempt_history"][0]
    assert (attempt["outcome"], attempt["command"], attempt["finished_at"], attempt["worker_name"]) == ("done", "ffmpeg -i x", later(120), "w1")
    assert jobs.claim(conn, w, 1, now=T0) == [] and jobs.counts(conn) == {"done": 1}


# ---------------------------------------------------------------- failures
def test_transient_failures_retry_with_growing_back_off_then_fail(conn):
    w = worker(conn)
    job_id = queue(conn, max_attempts=5)
    expected = [60, 300, 900, 3600]
    now = T0
    for attempt, wait in enumerate(expected, 1):
        run_one(conn, w, now=now)
        assert jobs.fail(conn, job_id, w["id"], "transient", "share offline", log_tail="line", now=now) == "queued"
        job = jobs.get(conn, job_id)
        assert (job["attempts"], job["not_before"], job["worker_id"], job["status"]) == (attempt, later(wait, now), None, "queued")
        now = later(wait, now)
    run_one(conn, w, now=now)
    assert jobs.fail(conn, job_id, w["id"], "transient", "share offline", now=now) == "failed"
    final = jobs.detail(conn, job_id)
    assert (final["status"], final["error_class"], final["error"], final["finished_at"]) == ("failed", "transient", "share offline", now)
    assert [a["outcome"] for a in final["attempt_history"]] == ["failed"] * 5 and final["attempt_history"][0]["log_tail"] == "line"


@pytest.mark.parametrize("error_class,attempts_before_review", [("verification_failed", 2), ("source_changed", 2), ("size_target_missed", 2)])
def test_some_failures_get_one_retry_and_then_wait_for_a_person(conn, error_class, attempts_before_review):
    w = worker(conn)
    job_id = queue(conn, max_attempts=5)
    run_one(conn, w)
    assert jobs.fail(conn, job_id, w["id"], error_class, "check failed", now=T0) == "queued"
    assert jobs.get(conn, job_id)["not_before"] is None                                          # worth trying again straight away
    run_one(conn, w)
    assert jobs.fail(conn, job_id, w["id"], error_class, "check failed again", log_tail="tail", now=T0) == "needs_review"
    job = jobs.get(conn, job_id)
    assert job["status"] == "needs_review" and job["review_reason"] == f"{error_class}: check failed again" and job["worker_id"] is None
    assert job["review_detail"]["error_class"] == error_class and job["preferred_worker_id"] == w["id"]
    assert attempts_before_review == job["attempts"]


def test_a_failed_encode_waits_for_a_person_at_once(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.fail(conn, job_id, w["id"], "encode_failed", "ffmpeg exited 1", log_tail="Invalid data", now=T0) == "needs_review"
    assert jobs.get(conn, job_id)["review_detail"]["log_tail"] == "Invalid data"


def test_the_worker_can_say_a_retry_is_pointless(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.fail(conn, job_id, w["id"], "transient", "source file is gone", retryable=False, now=T0) == "failed"
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.fail(conn, job_id, w["id"], "verification_failed", "tracks wrong", retryable=False, now=T0) == "needs_review"
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.fail(conn, job_id, w["id"], "mystery", "odd", now=T0) == "failed"                # unknown class: not retried unless it says so
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.fail(conn, job_id, w["id"], "mystery", "odd", retryable=True, now=T0) == "queued"


def test_failing_without_keeping_files_leaves_no_preferred_worker(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    jobs.fail(conn, job_id, w["id"], "transient", "x", keep_state=False, now=T0)
    assert jobs.get(conn, job_id)["preferred_worker_id"] is None


def test_a_cancelled_job_that_then_fails_ends_cancelled_and_may_clean_up(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.cancel(conn, job_id, by="Alice", now=T0) == "cancelling"
    assert jobs.progress(conn, job_id, w["id"], now=T0)["cancel"] is True and jobs.cancels_for(conn, w["id"]) == [job_id]
    assert jobs.fail(conn, job_id, w["id"], "cancelled", "stopped on request", now=T0) == "cancelled"
    job = jobs.get(conn, job_id)
    assert (job["status"], job["cleanup"], job["decision"]["by"]) == ("cancelled", 1, "Alice") and jobs.cleanups_for(conn, w["id"]) == [job_id]
    jobs.cleaned(conn, job_id, w["id"])
    assert jobs.cleanups_for(conn, w["id"]) == [] and jobs.get(conn, job_id)["preferred_worker_id"] is None


# ---------------------------------------------------------------- waiting for a person
def test_a_worker_can_hand_a_job_to_a_person_and_its_slot_is_free_again(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    jobs.needs_review(conn, job_id, w["id"], "bits per pixel 0.03 is under the floor", {"bpp": 0.03, "floor": 0.05}, now=T0)
    job = jobs.get(conn, job_id)
    assert (job["status"], job["worker_id"], job["lease_until"], job["preferred_worker_id"]) == ("needs_review", None, None, w["id"])
    assert job["review_reason"].startswith("bits per pixel") and job["review_detail"] == {"bpp": 0.03, "floor": 0.05}
    assert jobs.detail(conn, job_id)["attempt_history"][0]["outcome"] == "needs_review"
    queue(conn)
    assert len(jobs.claim(conn, w, 1, now=T0)) == 1                                              # the slot is free for other work


@pytest.fixture
def waiting(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    jobs.needs_review(conn, job_id, w["id"], "low confidence", {}, now=T0)
    return job_id, w


def test_approving_puts_the_job_back_for_the_worker_that_holds_its_files(conn, waiting):
    job_id, holder = waiting
    other = worker(conn, "other")
    assert jobs.decide(conn, job_id, "approve", by="Alice", note=" looks fine ", now=later(100)) == "queued"
    job = jobs.get(conn, job_id)
    assert job["decision"] == {"action": "approve", "by": "Alice", "note": "looks fine", "changes": None, "at": later(100)}
    assert jobs.claim(conn, other, 1, now=later(200)) == []
    got = jobs.claim(conn, holder, 1, now=later(200))[0]
    assert got["id"] == job_id and got["attempt"] == 2 and got["decision"]["action"] == "approve"   # the worker is told what was decided


def test_approving_with_changes_needs_the_changes(conn, waiting):
    job_id, holder = waiting
    for bad in (None, {}, "x"):
        with pytest.raises(jobs.JobError, match="Say what to change"):
            jobs.decide(conn, job_id, "approve_changes", changes=bad)
    jobs.decide(conn, job_id, "approve_changes", by="Bob", changes={"target_gb": 30}, now=T0)
    assert jobs.get(conn, job_id)["decision"]["changes"] == {"target_gb": 30}
    assert jobs.claim(conn, holder, 1, now=T0)[0]["decision"]["changes"] == {"target_gb": 30}


def test_retry_gives_a_fresh_set_of_attempts_and_attempt_numbers_keep_counting(conn):
    w = worker(conn)
    job_id = queue(conn, max_attempts=2)
    for _ in range(2):
        run_one(conn, w)
        jobs.fail(conn, job_id, w["id"], "verification_failed", "boom", now=T0)
    assert jobs.get(conn, job_id)["status"] == "needs_review" and jobs.get(conn, job_id)["attempts"] == 2
    assert jobs.decide(conn, job_id, "retry", by="Alice", now=T0) == "queued"
    assert jobs.get(conn, job_id)["attempts"] == 0
    assert jobs.claim(conn, w, 1, now=T0)[0]["attempt"] == 3                                      # the history is not overwritten
    assert [a["attempt_no"] for a in jobs.detail(conn, job_id)["attempt_history"]] == [1, 2, 3]


def test_skip_and_reject_close_the_job_and_release_the_workers_files(conn, waiting):
    job_id, holder = waiting
    assert jobs.decide(conn, job_id, "skip", by="Alice", now=T0) == "skipped"
    job = jobs.get(conn, job_id)
    assert (job["status"], job["cleanup"], job["finished_at"], job["message"]) == ("skipped", 1, T0, "Skipped: the file is left as it is.")
    assert jobs.cleanups_for(conn, holder["id"]) == [job_id]
    other = run_one(conn, queue(conn) and holder)
    jobs.needs_review(conn, other, holder["id"], "low", {}, now=T0)
    assert jobs.decide(conn, other, "reject", now=T0) == "cancelled" and jobs.get(conn, other)["decision"]["by"] == "someone"


def test_only_a_waiting_job_can_be_decided(conn):
    w = worker(conn)
    queued = queue(conn)
    with pytest.raises(jobs.JobError, match="not waiting"):
        jobs.decide(conn, queued, "approve")
    with pytest.raises(jobs.JobError, match="no longer exists"):
        jobs.decide(conn, 999, "approve")
    job_id = run_one(conn, w)
    jobs.needs_review(conn, job_id, w["id"], "x", {}, now=T0)
    with pytest.raises(jobs.JobError, match="Unknown decision"):
        jobs.decide(conn, job_id, "delete")


# ---------------------------------------------------------------- cancelling and retrying
def test_cancel_by_state(conn, waiting):
    w = worker(conn, "w2")
    queued = queue(conn)
    assert jobs.cancel(conn, queued, by="Alice", now=T0) == "cancelled"
    assert jobs.get(conn, queued)["finished_at"] == T0 and jobs.claim(conn, w, 1, now=T0) == []
    job_id, holder = waiting
    assert jobs.cancel(conn, job_id, now=T0) == "cancelled" and jobs.get(conn, job_id)["cleanup"] == 1          # a waiting job's files can go
    with pytest.raises(jobs.JobError, match="already cancelled"):
        jobs.cancel(conn, job_id)
    with pytest.raises(jobs.JobError, match="no longer exists"):
        jobs.cancel(conn, 999)


def test_retrying_a_closed_job(conn):
    w = worker(conn)
    job_id = queue(conn, max_attempts=1)
    run_one(conn, w)
    assert jobs.fail(conn, job_id, w["id"], "transient", "gone", now=T0) == "failed"
    jobs.retry(conn, job_id, by="Alice", now=later(5))
    job = jobs.get(conn, job_id)
    assert (job["status"], job["attempts"], job["error"], job["finished_at"], job["decision"]["action"]) == ("queued", 0, None, None, "retry")
    assert jobs.claim(conn, w, 1, now=later(6))[0]["attempt"] == 2
    with pytest.raises(jobs.JobError, match="Only a failed, cancelled or skipped"):
        jobs.retry(conn, job_id)                                                                 # it is queued again now
    with pytest.raises(jobs.JobError, match="no longer exists"):
        jobs.retry(conn, 999)


# ---------------------------------------------------------------- lost workers
def test_a_silent_worker_loses_its_job_to_the_queue_and_keeps_first_call(conn):
    w, other = worker(conn), worker(conn, "other")
    job_id = run_one(conn, queue(conn) and w)
    assert jobs.reap(conn, now=later(jobs.LEASE_SECONDS)) == []                                   # the lease is not over yet
    assert jobs.reap(conn, now=later(jobs.LEASE_SECONDS + 1)) == [job_id]
    job = jobs.detail(conn, job_id)
    assert (job["status"], job["worker_id"], job["preferred_worker_id"], job["error_class"]) == ("queued", None, w["id"], "worker_lost")
    assert job["attempt_history"][0]["outcome"] == "worker_lost" and "no word" in job["attempt_history"][0]["error"]
    assert jobs.claim(conn, other, 1, now=later(jobs.LEASE_SECONDS + 2)) == []                    # the restarted worker gets to resume it
    assert jobs.claim(conn, w, 1, now=later(jobs.LEASE_SECONDS + 2))[0]["attempt"] == 2


def test_a_job_that_keeps_losing_its_worker_eventually_fails(conn):
    w = worker(conn)
    job_id = queue(conn, max_attempts=2)
    for n in range(2):
        run_one(conn, w, now=later(n * 1000))
        jobs.reap(conn, now=later(n * 1000 + 200))
    job = jobs.get(conn, job_id)
    assert (job["status"], job["error_class"], job["attempts"]) == ("failed", "worker_lost", 2)


def test_a_cancelled_job_whose_worker_vanished_is_simply_cancelled(conn):
    w = worker(conn)
    job_id = run_one(conn, queue(conn) and w)
    jobs.cancel(conn, job_id, now=T0)
    jobs.reap(conn, now=later(500))
    assert jobs.get(conn, job_id)["status"] == "cancelled"


def test_a_restarted_worker_that_does_not_list_a_job_loses_it(conn):
    w = worker(conn, slots=3)
    for _ in range(3):
        queue(conn)
    kept, forgotten, fresh = (j["id"] for j in jobs.claim(conn, w, 3, now=T0))
    assert jobs.reconcile(conn, w["id"], [kept, fresh], now=later(10)) == []                      # inside the grace period nothing is judged
    assert jobs.reconcile(conn, w["id"], [kept, fresh, "junk", None], now=later(31)) == [forgotten]
    job = jobs.get(conn, forgotten)
    assert (job["status"], job["preferred_worker_id"], job["error_class"]) == ("queued", None, "worker_lost")
    assert jobs.get(conn, kept)["status"] == "running" and jobs.reconcile(conn, w["id"], [kept, fresh], now=later(60)) == []
    assert sorted(jobs.reconcile(conn, w["id"], [], now=later(60))) == sorted([kept, fresh])        # an empty list: it holds nothing


# ---------------------------------------------------------------- pages
def test_recent_orders_running_then_waiting_then_queued_then_finished(conn):
    w = worker(conn)
    done, queued, waiting_job, running = queue(conn, title="done"), queue(conn, title="queued"), queue(conn, title="waiting"), queue(conn, title="running")
    for target, how in ((done, "complete"), (waiting_job, "review"), (running, None)):
        conn.execute("UPDATE job SET priority = 1000 WHERE id = ?", (target,))
        got = jobs.claim(conn, w, 1, now=T0)
        assert got[0]["id"] == target
        if how == "complete":
            jobs.complete(conn, target, w["id"], now=T0)
        elif how == "review":
            jobs.needs_review(conn, target, w["id"], "why", {}, now=T0)
        conn.execute("UPDATE job SET priority = 0 WHERE id = ?", (target,))
    listed = jobs.recent(conn)
    assert [j["title"] for j in listed] == ["running", "waiting", "queued", "done"]
    assert listed[0]["worker_name"] == "w1" and listed[1]["holder_name"] == "w1"
    assert [j["title"] for j in jobs.recent(conn, status="queued")] == ["queued"] and [j["kind"] for j in jobs.recent(conn, kind="scan")] == []
    assert len(jobs.recent(conn, limit=2)) == 2
    assert jobs.counts(conn) == {"done": 1, "needs_review": 1, "queued": 1, "running": 1}
    assert jobs.detail(conn, 999) is None
