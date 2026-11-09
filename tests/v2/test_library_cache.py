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
