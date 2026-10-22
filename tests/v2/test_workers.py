import json

import pytest

from synchub import jobs, workers

T0 = "2026-01-01 12:00:00"


def add(conn, name="w1", tags=(), slots=1):
    worker_id, token = workers.create(conn, name, tags, slots)
    return workers.get(conn, worker_id), token


# ---------------------------------------------------------------- tokens and identity
def test_a_token_is_shown_once_and_only_its_hash_is_stored(conn):
    worker, token = add(conn)
    assert token.startswith("sw_") and len(token) == 51
    stored = conn.execute("SELECT * FROM worker").fetchone()
    assert stored["token_hash"] == workers.hash_token(token) and token not in " ".join(str(v) for v in dict(stored).values())
    assert stored["token_hint"] == token[-4:] and worker["token_hash"] is True            # views never carry the hash itself
    found, problem = workers.authenticate(conn, token)
    assert problem is None and found["id"] == worker["id"]


@pytest.mark.parametrize("token,problem", [("", "missing"), (None, "missing"), ("   ", "missing"), ("sw_nope", "unknown")])
def test_bad_tokens_are_refused(conn, token, problem):
    add(conn)
    assert workers.authenticate(conn, token) == (None, problem)


def test_a_disabled_worker_is_refused_and_a_revoked_one_is_unknown(conn):
    worker, token = add(conn)
    workers.set_flag(conn, worker["id"], "enabled", False)
    assert workers.authenticate(conn, token) == (None, "disabled")
    workers.set_flag(conn, worker["id"], "enabled", True)
    assert workers.authenticate(conn, token)[1] is None
    workers.revoke(conn, worker["id"])
    assert workers.authenticate(conn, token) == (None, "unknown")
    got = workers.get(conn, worker["id"])
    assert got["revoked"] and got["token_hash"] is False and got["name"] == "w1"           # the worker itself stays


def test_rotating_replaces_the_token_at_once_and_un_revokes(conn):
    worker, old = add(conn)
    new = workers.rotate_token(conn, worker["id"])
    assert new != old and workers.authenticate(conn, old)[0] is None and workers.authenticate(conn, new)[0]["id"] == worker["id"]
    workers.revoke(conn, worker["id"])
    again = workers.rotate_token(conn, worker["id"])
    assert workers.authenticate(conn, again)[1] is None and not workers.get(conn, worker["id"])["revoked"]


def test_two_workers_never_share_a_token(conn):
    (_, a), (_, b) = add(conn, "a"), add(conn, "b")
    assert a != b and workers.authenticate(conn, a)[0]["name"] == "a" and workers.authenticate(conn, b)[0]["name"] == "b"


# ---------------------------------------------------------------- creating and editing
def test_names_tags_and_slots_are_validated(conn):
    with pytest.raises(workers.WorkerError, match="needs a name"):
        workers.create(conn, "  ")
    with pytest.raises(workers.WorkerError, match="at most 60"):
        workers.create(conn, "x" * 61)
    add(conn, "Taken")
    with pytest.raises(workers.WorkerError, match="already a worker called 'Taken'"):
        workers.create(conn, "Taken")
    for bad in (0, 17, "x", None):
        with pytest.raises(workers.WorkerError, match="Slots"):
            workers.create(conn, f"slots-{bad}", slots=bad)
    with pytest.raises(workers.WorkerError, match="not a usable tag"):
        workers.create(conn, "t", tags=["ok", "bad tag!"])
    w, _ = add(conn, "tagged", tags="GPU, fast  gpu")
    assert w["tags"] == ["gpu", "fast"]                                                      # lower-cased, de-duplicated, split on commas and spaces


def test_update_renames_retags_and_resizes(conn):
    w, _ = add(conn, "a")
    add(conn, "b")
    workers.update(conn, w["id"], name="  Renamed ", tags=["x"], slots=3)
    got = workers.get(conn, w["id"])
    assert (got["name"], got["tags"], got["slots"]) == ("Renamed", ["x"], 3)
    with pytest.raises(workers.WorkerError, match="already a worker"):
        workers.update(conn, w["id"], name="b")
    workers.update(conn, w["id"], slots=2)                                                   # leaving the rest alone
    assert workers.get(conn, w["id"])["tags"] == ["x"]
    with pytest.raises(workers.WorkerError, match="no longer exists"):
        workers.update(conn, 999, name="z")


def test_flags_and_deleting(conn):
    w, token = add(conn)
    for flag in ("paused", "draining", "enabled"):
        workers.set_flag(conn, w["id"], flag, True)
    with pytest.raises(workers.WorkerError, match="Unknown setting"):
        workers.set_flag(conn, w["id"], "slots", 1)
    with pytest.raises(workers.WorkerError, match="no longer exists"):
        workers.set_flag(conn, 999, "paused", True)
    job_id = jobs.create(conn, "library")
    workers.set_flag(conn, w["id"], "draining", False)
    workers.set_flag(conn, w["id"], "paused", False)
    jobs.claim(conn, workers.get(conn, w["id"]), 1, now=T0)
    with pytest.raises(workers.WorkerError, match="running a job"):
        workers.delete(conn, w["id"])
    jobs.cancel(conn, job_id)
    jobs.progress(conn, job_id, w["id"], now=T0)
    jobs.fail(conn, job_id, w["id"], "transient", "stopped", now=T0)
    workers.delete(conn, w["id"])
    assert conn.execute("SELECT COUNT(*) FROM worker").fetchone()[0] == 0
    assert jobs.get(conn, job_id)["status"] == "cancelled"                                   # its history stays


# ---------------------------------------------------------------- registering and being seen
def test_register_checks_the_protocol_and_keeps_what_the_agent_says(conn):
    w, _ = add(conn)
    info = {"protocol": workers.PROTOCOL, "version": "1.2", "os": "Ubuntu 24.04", "hostname": "enc1", "slots": 2,
            "capabilities": {"ffmpeg": "6.1", "encoders": ["libx265", "libx264"], "cores": 16}}
    workers.register(conn, w["id"], info, now=T0)
    got = workers.get(conn, w["id"], now=T0)
    assert (got["version"], got["os"], got["hostname"], got["protocol"], got["last_seen"]) == ("1.2", "Ubuntu 24.04", "enc1", workers.PROTOCOL, T0)
    assert got["capabilities"]["encoders"] == ["libx265", "libx264"] and got["capabilities"]["agent_slots"] == 2 and got["slots"] == 1      # the central slot count wins
    for bad in ({}, {"protocol": 99}, None, {"protocol": "1"}):
        with pytest.raises(workers.ProtocolError, match="protocol"):
            workers.register(conn, w["id"], bad)


def test_online_means_seen_within_the_last_minute(conn):
    w, _ = add(conn)
    assert workers.get(conn, w["id"], now=T0)["online"] is False                              # never seen
    workers.touch(conn, w["id"], status={"disk_free_gb": 120}, now=T0)
    got = workers.get(conn, w["id"], now="2026-01-01 12:00:59")
    assert got["online"] and got["status"] == {"disk_free_gb": 120}
    assert not workers.get(conn, w["id"], now="2026-01-01 12:01:00")["online"]
    workers.touch(conn, w["id"], now="2026-01-01 12:05:00")                                   # no status: the old summary stays
    assert workers.get(conn, w["id"], now="2026-01-01 12:05:00")["status"] == {"disk_free_gb": 120}
    workers.touch(conn, w["id"], status={"junk": "x" * 20000}, now="2026-01-01 12:06:00")      # too big to keep
    assert workers.get(conn, w["id"])["status"] == {"disk_free_gb": 120}


def test_the_list_counts_each_workers_running_jobs(conn):
    a, _ = add(conn, "a", slots=2)
    add(conn, "b")
    jobs.create(conn, "library")
    jobs.create(conn, "library")
    jobs.claim(conn, a, 2, now=T0)
    listed = {w["name"]: w for w in workers.list_all(conn, now=T0)}
    assert listed["a"]["running"] == 2 and listed["b"]["running"] == 0
    assert all("token_hash" in w and w["token_hash"] in (True, False) for w in listed.values())
