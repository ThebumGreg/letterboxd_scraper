import sqlite3

import pytest

from synchub import db, settings


def test_migrate_creates_schema_and_is_idempotent(v2):
    latest = max(version for version, _ in db._migration_files())            # every numbered file applied, none twice
    assert v2.migrate() == latest
    assert v2.migrate() == latest
    with v2.connection() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == latest
    for table in ("profile", "media", "episode", "link", "match_review", "state_def", "processing_event", "item_state",
                  "src_jellyfin_item", "src_radarr_movie", "src_sonarr_series", "src_sonarr_episode", "src_arr_queue",
                  "src_letterboxd_entry", "src_serializd_entry", "link_attempt", "manual_request", "worker", "job", "job_attempt", "app_setting", "task_status", "api_payload_log", "ddns_history"):
        assert table in tables


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episode (media_id, season_number, episode_number) VALUES (999, 1, 1)")


def test_state_defs_are_seeded_and_extensible(conn):
    codes = [r["code"] for r in conn.execute("SELECT code FROM state_def ORDER BY rank")]
    assert codes == ["missing", "queued", "downloading", "downloaded", "transferring", "encoding", "in_library"]
    # A new state is just a row; no schema change.
    conn.execute("INSERT INTO state_def (code, label, rank) VALUES ('verifying', 'Verifying', 45)")
    conn.execute("INSERT INTO processing_event (target_type, target_id, provider, state_code) VALUES ('movie', 1, 'script', 'verifying')")


def test_profile_identities_must_be_unique(conn):
    conn.execute("INSERT INTO profile (name, letterboxd_username, serializd_username, jellyfin_user_id) VALUES ('A', 'alice', 'alice_sz', 'jf1')")
    for column, value in (("letterboxd_username", "ALICE"), ("serializd_username", "Alice_SZ"), ("jellyfin_user_id", "jf1"), ("name", "a")):
        row = {"name": "B", "letterboxd_username": None, "serializd_username": None, "jellyfin_user_id": None}
        row[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO profile (name, letterboxd_username, serializd_username, jellyfin_user_id) VALUES (:name, :letterboxd_username, :serializd_username, :jellyfin_user_id)", row)


def test_profiles_may_leave_identities_unset_but_not_blank(conn):
    conn.execute("INSERT INTO profile (name) VALUES ('NoIds1')")
    conn.execute("INSERT INTO profile (name) VALUES ('NoIds2')")   # several NULLs are fine
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO profile (name, letterboxd_username) VALUES ('Blank', '')")


def test_media_ids_are_unique_per_kind(conn):
    conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'A', '1')")
    conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'B', '1')")   # same TMDB number, different kind
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'C', '1')")
    conn.execute("INSERT INTO media (kind, title) VALUES ('movie', 'No id 1')")
    conn.execute("INSERT INTO media (kind, title) VALUES ('movie', 'No id 2')")


def test_episode_unique_per_show_season_number_and_cascades(conn):
    mid = conn.execute("INSERT INTO media (kind, title) VALUES ('show', 'S')").lastrowid
    conn.execute("INSERT INTO episode (media_id, season_number, episode_number) VALUES (?, 0, 1)", (mid,))   # a Special
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episode (media_id, season_number, episode_number) VALUES (?, 0, 1)", (mid,))
    conn.execute("DELETE FROM media WHERE id = ?", (mid,))
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 0


def test_one_link_per_source_record(conn):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', 1, 'jellyfin', 'abc', 'tmdb_id', 1.0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', 2, 'jellyfin', 'abc', 'tmdb_id', 1.0)")


def test_only_one_open_event_per_provider_reference(conn):
    insert = "INSERT INTO processing_event (target_type, target_id, provider, state_code, external_ref, ended_at) VALUES ('movie', 1, 'radarr_queue', 'downloading', 'q1', ?)"
    conn.execute(insert, (None,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (None,))
    conn.execute("UPDATE processing_event SET ended_at = '2026-01-01 00:00:00'")
    conn.execute(insert, (None,))   # allowed again once the first is closed


def test_migration_runner_rejects_gaps(tmp_path, monkeypatch):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x);")
    (tmp_path / "003_c.sql").write_text("CREATE TABLE c (x);")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "t.db"))
    with pytest.raises(ValueError, match="consecutive"):
        db.migrate()


def test_failed_migration_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x);")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE b (x); INSERT INTO nope VALUES (1);")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "t.db"))
    with pytest.raises(sqlite3.Error):
        db.migrate()
    conn = db.connect()
    try:
        assert db.current_version(conn) == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'b'").fetchone()   # half-applied table rolled back
    finally:
        conn.close()
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE b (x);")
    assert db.migrate() == 2


def test_settings_defaults_overrides_and_validation(v2):
    assert settings.get("scraper_interval_hours") == "24"
    assert settings.get("unknown_key") == ""
    assert settings.get("unknown_key", "x") == "x"
    settings.set_value("radarr_url", "http://radarr:7878")
    assert settings.get("radarr_url") == "http://radarr:7878"

    errors = settings.save_form({"sonarr_interval_hours": "abc", "radarr_interval_hours": "12", "tmdb_api_key": "  key123456 "})
    assert len(errors) == 1 and "sonarr_interval_hours" in errors[0]
    assert settings.get("radarr_interval_hours") == "12" and settings.get("sonarr_interval_hours") == "6"
    assert settings.get("tmdb_api_key") == "key123456"
    settings.save_form({"tmdb_api_key": ""})
    assert settings.get("tmdb_api_key") == "key123456"   # blank secret keeps the stored value
