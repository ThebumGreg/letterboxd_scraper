"""The Library's worked-out recommendations are kept until something they depend on changes (migration 041's counter), and the profile lookups happen once."""
from synchub import quality
from synchub.library import cache, profiles, recommend as rc, scan
from tests.v2 import library_helpers as h


def _calls(monkeypatch):
    calls = []
    real = rc._classify_all
    monkeypatch.setattr(rc, "_classify_all", lambda conn, cfg: calls.append(1) or real(conn, cfg))
    return calls


def test_classify_is_reused_until_a_file_or_setting_changes(v2, conn, monkeypatch):
    cache.clear()
    h.add(conn)
    calls = _calls(monkeypatch)
    with v2.connection() as c:
        cfg = scan.config(c)
        first = rc.classify(c, cfg)
        assert rc.classify(c, cfg) == first and len(calls) == 1
        assert [rec["id"] for rec, _ in rc.classify(c, cfg, "heat")] == [first[0][0]["id"]] and rc.classify(c, cfg, "nothing like it") == [] and len(calls) == 1
        c.execute("UPDATE library_file SET size_bytes = size_bytes + 1")
    with v2.connection() as c:
        rc.classify(c, scan.config(c))
    assert len(calls) == 2
    with v2.connection() as c:
        c.execute("INSERT INTO app_setting (key, value) VALUES ('naming_style', 'jellyfin') ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    with v2.connection() as c:
        rc.classify(c, scan.config(c))
    assert len(calls) == 3


def test_a_background_refresh_may_use_a_result_a_few_seconds_old_but_a_button_never_does(v2, conn, monkeypatch):
    cache.clear()
    h.add(conn)
    calls = _calls(monkeypatch)
    with v2.connection() as c:
        rc.classify(c, scan.config(c))
        c.execute("UPDATE library_file SET size_bytes = size_bytes + 1")
    with v2.connection() as c:
        rc.classify(c, scan.config(c), max_age=60)
        assert len(calls) == 1
        rc.classify(c, scan.config(c))
    assert len(calls) == 2


def test_quality_changes_move_the_held_list(v2, conn):
    cache.clear()
    file_id = h.add(conn, rel="Movies/Tiny.mkv", name="Tiny (2000).mkv")
    with v2.connection() as c:
        before = quality.held_ids(c)
        c.execute("UPDATE library_file SET resolution = '320x240' WHERE id = ?", (file_id,))
    with v2.connection() as c:
        assert file_id in quality.held_ids(c) and file_id not in before             # the cached list moved with the change
        assert quality.low_count(c) == len(quality.held_ids(c)) == len(quality.low_quality(c, 50))


def test_profiles_are_read_once_inside_lookups(v2):
    reads = []
    real = profiles._get
    with v2.connection() as c:
        profiles._get = lambda conn, pid: reads.append(pid) or real(conn, pid)
        try:
            with profiles.lookups():
                for _ in range(50):
                    profiles.effective(c, {})
            assert len(reads) == 1
            profiles.effective(c, {})
            profiles.effective(c, {})
            assert len(reads) == 3
        finally:
            profiles._get = real


def test_background_bookkeeping_settings_leave_the_counter_alone(v2, conn):
    from synchub import settings, webhooks
    with v2.connection() as c:
        before = cache.generation(c)
        for key in ("worker_host_seen:1", "webhook_last_jellyfin", "ddns_last_run", "pushover_quota", "notify_paused", "worker_update:2", "worker_updater_log:2"):
            settings.set_value(key, "a", conn=c)
            settings.set_value(key, "b", conn=c)
        assert cache.generation(c) == before
        settings.set_value("naming_style", "plex", conn=c)
        assert cache.generation(c) == before + 1


def test_requests_waiting_on_a_slow_compute_share_the_next_one_even_when_files_keep_changing(v2, conn, monkeypatch):
    """Every change moved the counter, so each waiting request found a result older than its own and worked it all out again, one after another.
    No sleeps decide the order: the first compute is held open until all five waiters have arrived (counted where a request first reads the counter)."""
    import threading
    import time
    cache.clear()
    h.add(conn)
    runs, results = [], []
    arrivals, all_here, lock = [], threading.Event(), threading.Lock()
    real_generation = cache.generation

    def counting_generation(c):
        value = real_generation(c)
        with lock:
            arrivals.append(1)
            if len(arrivals) == 6:                                   # the first request and the five that wait behind it
                all_here.set()
        return value

    monkeypatch.setattr(cache, "generation", counting_generation)

    def slow():
        runs.append(1)
        if len(runs) == 1:
            assert all_here.wait(10), "the waiters never arrived"
            time.sleep(0.2)                                          # they have read the counter; let them reach the lock before the first compute ends
        return len(runs)

    def ask():
        with v2.connection() as c:
            results.append(cache.get(c, "slow", slow))

    first = threading.Thread(target=ask)
    first.start()
    while not runs:                                                  # the first compute is under way (and held open)
        time.sleep(0.01)
    waiters = []
    for _ in range(5):
        conn.execute("UPDATE library_file SET size_bytes = size_bytes + 1")           # the counter moves for every request that arrives
        conn.commit()
        waiters.append(threading.Thread(target=ask))
        waiters[-1].start()
    for t in [first] + waiters:
        t.join(20)
    assert len(runs) == 2                      # the first, and one more that all five waiters share


def test_a_background_refresh_shows_the_last_result_instead_of_queueing_behind_a_slow_compute(v2, conn):
    import threading
    import time
    cache.clear()
    h.add(conn)
    with v2.connection() as c:
        cache.get(c, "slowpoll", lambda: "old")
    conn.execute("UPDATE library_file SET size_bytes = size_bytes + 1")
    conn.commit()
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.6)
        return "new"

    worker = threading.Thread(target=lambda: cache.get(v2.connect(), "slowpoll", slow))
    worker.start()
    started.wait(2)
    began = time.monotonic()
    with v2.connection() as c:
        assert cache.get(c, "slowpoll", lambda: "never", max_age=1) == "old"
    assert time.monotonic() - began < 0.3
    worker.join()
