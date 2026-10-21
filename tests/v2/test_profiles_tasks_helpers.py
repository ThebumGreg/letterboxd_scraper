import pytest

from synchub import profiles, tasks
from synchub.profiles import ProfileError

from .fakes import FakeTmdb
from .test_metadata import SEASONS, SHOW


def get(conn, name):
    return dict(conn.execute("SELECT * FROM profile WHERE name = ?", (name,)).fetchone())


def test_create_normalises_blanks_to_null(conn):
    pid = profiles.save(conn, None, "  Alice ", "alice", "  ", "")
    row = get(conn, "Alice")
    assert row["id"] == pid and (row["letterboxd_username"], row["serializd_username"], row["jellyfin_user_id"]) == ("alice", None, None)


def test_duplicate_identities_are_refused_with_a_clear_message(conn):
    profiles.save(conn, None, "Alice", "alice", "alice_sz", "jf1")
    with pytest.raises(ProfileError, match="Letterboxd username 'ALICE' already belongs to the profile 'Alice'"):
        profiles.save(conn, None, "Bob", "ALICE")
    with pytest.raises(ProfileError, match="Serializd username"):
        profiles.save(conn, None, "Bob", None, "alice_sz")
    with pytest.raises(ProfileError, match="Jellyfin user"):
        profiles.save(conn, None, "Bob", None, None, "jf1")
    with pytest.raises(ProfileError, match="profile name 'alice'"):
        profiles.save(conn, None, "alice")
    assert conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] == 1


def test_update_keeps_own_identities_and_can_clear_them(conn):
    pid = profiles.save(conn, None, "Alice", "alice", "alice_sz", "jf1")
    profiles.save(conn, pid, "Alice B", "alice", "", "jf1")             # same Letterboxd name is fine for the same profile
    row = get(conn, "Alice B")
    assert (row["letterboxd_username"], row["serializd_username"], row["jellyfin_user_id"]) == ("alice", None, "jf1")


def test_only_given_leaves_other_identities_alone(conn):
    pid = profiles.save(conn, None, "Alice", "alice", "alice_sz")
    profiles.save(conn, pid, "Alice", None, None, "jf9", only_given=True)
    row = get(conn, "Alice")
    assert (row["letterboxd_username"], row["serializd_username"], row["jellyfin_user_id"]) == ("alice", "alice_sz", "jf9")


def test_required_name_and_missing_profile(conn):
    with pytest.raises(ProfileError, match="name is required"):
        profiles.save(conn, None, "  ")
    with pytest.raises(ProfileError, match="no longer exists"):
        profiles.save(conn, 999, "X")


def test_delete_keeps_entries_and_recreating_reattaches_them(conn):
    pid = profiles.save(conn, None, "Alice", "alice")
    conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, first_seen, last_seen) VALUES ('alice', 'f', 'watchlist', 'a', 'a')")
    assert profiles.delete(conn, pid) and not profiles.delete(conn, pid)
    assert conn.execute("SELECT COUNT(*) FROM src_letterboxd_entry").fetchone()[0] == 1
    from synchub import catalog
    m = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'F', '1')").lastrowid
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'letterboxd', 'f', 'x', 1.0)", (m,))
    assert catalog.requesters_of(conn, m)["wanted_by"] == []
    profiles.save(conn, None, "Alice again", "alice")
    assert catalog.requesters_of(conn, m)["wanted_by"] == ["Alice again"]


def test_set_active(conn):
    pid = profiles.save(conn, None, "Alice", "alice")
    assert profiles.set_active(conn, pid, False) and get(conn, "Alice")["is_active"] == 0
    assert not profiles.set_active(conn, 999, True)


def test_all_statuses_lists_every_task(v2):
    statuses = tasks.all_statuses()
    assert set(statuses) == set(tasks.TASK_NAMES) and statuses["radarr"]["status"] == "idle"
    tasks.set_status("radarr", "completed", "done")
    assert tasks.all_statuses()["radarr"]["message"] == "done"


def test_refresh_soon_coalesces_a_burst_into_one_run_plus_one_followup(monkeypatch):
    monkeypatch.setattr(tasks, "_REFRESH_RUNNING", set())
    monkeypatch.setattr(tasks, "_REFRESH_PENDING", set())
    runs, spawned = [], []
    assert tasks.refresh_soon("radarr", lambda: runs.append(1), spawn=spawned.append)       # starts
    assert not tasks.refresh_soon("radarr", lambda: runs.append(1), spawn=spawned.append)   # a second event while running
    assert not tasks.refresh_soon("radarr", lambda: runs.append(1), spawn=spawned.append)   # and a third: still only one follow-up
    assert tasks.refresh_soon("sonarr", lambda: runs.append("s"), spawn=spawned.append)     # other services are independent
    spawned[0]()                                                                             # run the radarr loop to completion
    assert runs == [1, 1]
    assert tasks.refresh_soon("radarr", lambda: runs.append(1), spawn=spawned.append)       # free again afterwards


def test_refresh_soon_survives_a_failing_job(monkeypatch):
    monkeypatch.setattr(tasks, "_REFRESH_RUNNING", set())
    monkeypatch.setattr(tasks, "_REFRESH_PENDING", set())
    spawned = []

    def boom():
        raise RuntimeError("x")
    tasks.refresh_soon("radarr", boom, spawn=spawned.append)
    spawned[0]()
    assert tasks.refresh_soon("radarr", lambda: None, spawn=spawned.append)


def test_refresh_media_updates_episodes_and_states(v2):
    with v2.connection() as c:
        mid = c.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'S', '100')").lastrowid
    message = tasks.refresh_media(mid, FakeTmdb(shows={"100": SHOW}, seasons={"100": SEASONS[100]}))
    assert message == "Refreshed 4 episode(s) from TMDB."
    with v2.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM item_state WHERE target_type = 'episode'").fetchone()[0] == 4
    with pytest.raises(LookupError):
        tasks.refresh_media(999, FakeTmdb())
