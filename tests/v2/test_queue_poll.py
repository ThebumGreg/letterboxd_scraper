import json
import logging

import pytest

from synchub import settings, states, tasks
from synchub.clients import ConfigError

from .test_states import episode, link, movie, radarr, show, sonarr_ep


class QueueClient:
    """Only the queue, as the poller uses it."""
    def __init__(self, records=(), error=None):
        self.records, self.error, self.reads = list(records), error, 0

    def queue(self):
        from synchub import db
        db.assert_not_in_write_transaction("fake queue")
        self.reads += 1
        if self.error:
            raise self.error
        return list(self.records)


def download(queue_id=5, movie_id=1, left=600, size=1000, state="downloading"):
    return {"id": queue_id, "movieId": movie_id, "size": size, "sizeleft": left, "trackedDownloadState": state, "title": "Heat"}


@pytest.fixture(autouse=True)
def fresh_poller_state(monkeypatch):
    for memory in (tasks._LAST_POLL, tasks._POLL_FAILURES, tasks._UNLINKED):
        memory.clear()
    refreshed, heard = [], []
    monkeypatch.setattr(tasks, "refresh_soon", lambda service, job, **kw: refreshed.append(service))
    monkeypatch.setattr(tasks, "state_listeners", [lambda ids: heard.append(set(ids))])
    return refreshed, heard


@pytest.fixture
def refreshed(fresh_poller_state):
    return fresh_poller_state[0]


@pytest.fixture
def heard(fresh_poller_state):
    return fresh_poller_state[1]


def linked_movie(conn):
    mid = movie(conn)
    radarr(conn, "1")
    link(conn, "media", mid, "radarr", "1")
    conn.commit()
    return mid


def test_progress_is_followed_and_dashboards_are_told_only_when_something_changed(v2, conn, refreshed, heard):
    mid = linked_movie(conn)
    client = QueueClient([download(left=600)])
    result = tasks.poll_queue("radarr", client=client)
    assert result["in_queue"] == 1 and result["events"]["opened"] == 1 and result["media"] == {mid}
    assert dict(conn.execute("SELECT state_code, progress FROM item_state WHERE target_type = 'movie'").fetchone()) == {"state_code": "downloading", "progress": 0.4}
    client.records = [download(left=300)]
    tasks.poll_queue("radarr", client=client)
    assert conn.execute("SELECT progress FROM item_state WHERE target_type = 'movie'").fetchone()[0] == 0.7
    assert heard == [{mid}, {mid}]
    tasks.poll_queue("radarr", client=client)                                  # nothing moved: nobody is told
    assert len(heard) == 2 and refreshed == []                                 # and no library sync is needed while a download is just progressing


def test_a_download_that_leaves_the_queue_starts_a_library_sync(v2, conn, refreshed, heard):
    mid = linked_movie(conn)
    client = QueueClient([download()])
    tasks.poll_queue("radarr", client=client)
    client.records = []                                                         # imported (or removed): the library has to be read to know which
    result = tasks.poll_queue("radarr", client=client)
    assert result["events"]["closed"] == 1 and refreshed == ["radarr"] and heard[-1] == {mid}
    assert conn.execute("SELECT state_code FROM item_state WHERE target_type = 'movie'").fetchone()[0] == "queued"      # until that sync sees the file


def test_a_download_we_do_not_know_yet_starts_one_library_sync_not_one_per_poll(v2, conn, refreshed):
    client = QueueClient([download(movie_id=99)])
    tasks.poll_queue("radarr", client=client)
    assert refreshed == ["radarr"]
    tasks.poll_queue("radarr", client=client)
    assert refreshed == ["radarr"]                                              # still unknown, but not new
    client.records = [download(queue_id=5, movie_id=99), download(queue_id=6, movie_id=98)]
    tasks.poll_queue("radarr", client=client)
    assert refreshed == ["radarr", "radarr"]


def test_idle_services_are_only_looked_at_now_and_then(v2, conn, refreshed, monkeypatch):
    client = QueueClient([])
    assert tasks.poll_queue("radarr", client=client) is not None and client.reads == 1          # first look after start
    assert tasks.poll_queue("radarr", client=client) is None and client.reads == 1              # idle: skipped
    assert tasks.poll_queue("radarr", client=client, force=True) is not None and client.reads == 2    # a webhook can force a look
    tasks._LAST_POLL["radarr"] -= tasks.QUEUE_IDLE_SECONDS + 1
    assert tasks.poll_queue("radarr", client=client) is not None and client.reads == 3          # the idle check is due
    linked_movie(conn)
    client.records = [download()]
    tasks.poll_queue("radarr", client=client, force=True)
    for _ in range(3):                                                                           # something is downloading: every tick
        assert tasks.poll_queue("radarr", client=client) is not None
    assert client.reads == 7


def test_a_running_full_sync_is_not_raced(v2, conn):
    import threading
    lock = tasks._LOCKS["radarr"] = threading.Lock()
    lock.acquire()
    try:
        client = QueueClient([download()])
        assert tasks.poll_queue("radarr", client=client, force=True) is None and client.reads == 0
    finally:
        lock.release()
        tasks._LOCKS.pop("radarr")


def test_unreachable_services_are_logged_once_in_a_while_and_recovery_is_noted(v2, conn, caplog):
    caplog.set_level(logging.INFO, logger="synchub.tasks")
    client = QueueClient(error=RuntimeError("connection refused"))
    for _ in range(21):
        assert tasks.poll_queue("radarr", client=client, force=True) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "[queue]" in r.getMessage()]
    assert len(warnings) == 2 and "1 failure" in warnings[0].getMessage() and "20 failure" in warnings[1].getMessage()
    client.error = None
    tasks.poll_queue("radarr", client=client, force=True)
    assert any("can be read again" in r.getMessage() for r in caplog.records)


def test_unconfigured_services_are_ignored_quietly(v2, caplog):
    assert tasks.poll_queues(force=True) == {"radarr": None, "sonarr": None}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_sonarr_episode_downloads_are_reported_against_their_show(v2, conn, heard):
    sid = show(conn)
    eid = episode(conn, sid, 1, 1)
    sonarr_ep(conn, "501")
    link(conn, "episode", eid, "sonarr_episode", "501")
    conn.commit()
    client = QueueClient([{"id": 7, "episodeId": 501, "size": 100, "sizeleft": 50, "trackedDownloadState": "downloading", "title": "S01E01"}])
    result = tasks.poll_queue("sonarr", client=client)
    assert result["media"] == {sid} and heard == [{sid}]
    assert conn.execute("SELECT state_code, progress FROM item_state WHERE target_type = 'episode'").fetchone()[:] == ("downloading", 0.5)


def test_changes_too_big_to_announce_row_by_row_are_not(heard):
    tasks.notify_states_changed(set(range(tasks.MAX_ANNOUNCED + 1)))
    tasks.notify_states_changed(set())
    assert heard == []
    tasks.notify_states_changed({1, 2})
    assert heard == [{1, 2}]


def test_a_failing_listener_does_not_break_the_others(monkeypatch, caplog):
    got = []

    def broken(ids):
        raise RuntimeError("boom")
    monkeypatch.setattr(tasks, "state_listeners", [broken, got.append])
    tasks.notify_states_changed({3})
    assert got == [{3}] and "listener failed" in caplog.text


def test_scoped_state_computation_touches_only_what_it_is_given(v2, conn):
    a, b = movie(conn, "1"), movie(conn, "2")
    for mid, rid in ((a, "1"), (b, "2")):
        radarr(conn, rid, has_file=1)
        link(conn, "media", mid, "radarr", rid)
    conn.commit()
    changed = set()
    stats = states.compute(conn, only={("movie", a)}, changed=changed)
    assert stats["movies"] == 1 and changed == {("movie", a)}
    assert conn.execute("SELECT COUNT(*) FROM item_state").fetchone()[0] == 1          # b was not looked at
    changed.clear()
    states.compute(conn, changed=changed)
    assert changed == {("movie", b)}                                                    # a had not changed since; a full run adds b
    assert states.compute(conn, only=set())["movies"] == 0
    assert states.media_ids_for(conn, {("movie", a)}) == {a}


def test_the_poll_job_follows_its_setting(v2):
    from synchub import scheduler
    jobs = lambda: {j.id: j.trigger.interval.total_seconds() for j in scheduler.scheduler.get_jobs() if hasattr(j.trigger, "interval")}
    scheduler.configure()
    assert jobs()["job_queue_poll"] == 30
    for value, expected in (("0", None), ("3", 10), ("45", 45), ("abc", 30), ("-5", 30)):
        settings.set_value("queue_poll_seconds", value)
        scheduler.configure()
        assert jobs().get("job_queue_poll") == expected, value
