import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_db_sample as export  # noqa: E402

SECRET = "sup3r-secret-api-key-123"


def _md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def messy_db(db):
    with db.get_connection() as conn:
        for key, value in [("tmdb_api_key", SECRET), ("apprise_urls", "pover://user@token"), ("jellyfin_url", "http://192.168.1.50:8096"),
                           ("dynu_domain", "myhome.dynu.net"), ("scraper_interval_hours", "24")]:
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        conn.execute("INSERT INTO api_payload_logs (direction, service, headers_json) VALUES ('OUT','radarr', ?)", (json.dumps({"X-Api-Key": SECRET}),))
        conn.execute("INSERT INTO user_profile_mappings (profile_name, letterboxd_username) VALUES ('Alice', 'alice_lb')")
        # Show 1: well formed.  Show 2: TMDB only, episodes only carry the series name.  Show 3: duplicate of show 2 by name.
        conn.executemany("INSERT INTO media_catalog (tmdb_id, tvdb_id, movie_slug, title, release_year, media_type, requested_by_users, user_count) VALUES (?,?,?,?,?,'tv',?,1)", [
            ("100", "900", "good-show", "Good Show", 2010, "Alice"),
            ("200", None, "serializd-77", "Broken Show", 2015, "Alice"),
            (None, "555", "sonarr-9", "Broken Show", 2015, "Sonarr"),
        ])
        conn.executemany("INSERT INTO user_requests (username, movie_slug, film_title, tmdb_id, tvdb_id, media_type, letterboxd_lists, id_source) VALUES (?,?,?,?,?,'tv','Watchlist',?)", [
            ("alice_lb", "good-show", "Good Show", "100", "900", None),
            ("alice_lb", "serializd-77", "Broken Show", "200", None, "manual"),
        ])
        conn.execute("INSERT INTO service_cache (service, tvdb_id, title, media_type, external_item_id, status, in_library) VALUES ('sonarr','555','Broken Show','tv','9','monitored',1)")
        eps = [("900", "100", 1, 1, "Good Show - S01E01 (Pilot)"), ("900", "100", 1, 2, "Good Show - S01E02 (Two)"),
               (None, None, 1, 1, "Broken Show - S01E01 (Pilot)"), (None, None, 1, 2, "Broken Show - S01E02 (Two)"),
               ("901", "100", 1, 3, "Other - S01E03 (Wrong match)")]   # shares TMDB 100 but a different TVDB ID
        for i, (tvdb, tmdb, s, e, title) in enumerate(eps):
            conn.execute("INSERT INTO service_cache (service, tvdb_id, tmdb_id, title, media_type, season_number, episode_number, status, in_library, external_item_id) VALUES ('jellyfin',?,?,?,'episode',?,?,'in_library',1,?)", (tvdb, tmdb, title, s, e, f"jf{i}"))
        conn.execute("INSERT INTO tv_episodes (tvdb_id, tmdb_id, season_number, episode_number, title) VALUES ('NONE','100',1,1,'a'), ('900','100',1,1,'b')")
    return db


def _run(messy_db, tmp_path, *extra):
    out = tmp_path / "out"
    export.main(["--db", messy_db.DB_FILE, "--out", str(out), *extra])
    return out


def test_export_writes_expected_files_and_does_not_modify_db(messy_db, tmp_path):
    before = _md5(messy_db.DB_FILE)
    out = _run(messy_db, tmp_path)
    assert _md5(messy_db.DB_FILE) == before
    for name in ("schema.sql", "summary.json", "tv_shows.json", "samples/user_requests.csv", "samples/app_settings.csv"):
        assert (out / name).is_file(), name
    assert "CREATE TABLE" in (out / "schema.sql").read_text()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["tables"]["media_catalog"]["rows"] == 3
    assert summary["tables"]["media_catalog"]["columns"]["media_type"]["values"] == {"tv": 3}


def test_secrets_never_reach_the_output(messy_db, tmp_path):
    out = _run(messy_db, tmp_path)
    text = "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    for secret in (SECRET, "pover://user@token", "192.168.1.50", "myhome.dynu.net"):
        assert secret not in text
    assert "http://<host>:8096" in text
    assert not (out / "samples" / "api_payload_logs.csv").exists()
    assert json.loads((out / "summary.json").read_text())["tables"]["api_payload_logs"]["rows"] == 1


def test_tv_diagnostics_flag_the_known_problem_patterns(messy_db, tmp_path):
    out = _run(messy_db, tmp_path)
    summary = json.loads((out / "summary.json").read_text())
    dups = summary["diagnostics"]["media_catalog"]["duplicates"]["same_title_different_rows"]
    assert dups and dups[0]["normalized_title"] == "broken show"
    assert summary["diagnostics"]["service_cache"]["jellyfin_episodes"]["episodes_without_any_id"] == 2
    assert summary["diagnostics"]["service_cache"]["jellyfin_episodes"]["series_names_of_episodes_without_ids"] == {"Broken Show": 2}
    assert summary["diagnostics"]["tv_episodes_cache"]["totals"]["tvdb_none"] == 1
    assert summary["diagnostics"]["user_requests"]["manual_or_dismissed (not rebuildable from sources)"][0]["manual_links"] == 1

    shows = {s["catalog"]["movie_slug"]: s for s in json.loads((out / "tv_shows.json").read_text())["shows"]}
    good = shows["good-show"]
    assert good["jellyfin_episodes"]["matched_by_tvdb"] == 2
    assert good["jellyfin_episodes"]["rows_with_conflicting_ids"] == 1
    broken = shows["serializd-77"]
    assert broken["jellyfin_episodes"]["unlinked_but_title_starts_with_show_name"] == 2
    assert any("TMDB only" in f for f in broken["flags"])
    assert any("several catalog rows" in f for f in broken["flags"])
    assert broken["service_rows_matched_by_name_only"][0]["service"] == "sonarr"


def test_anonymize_users_and_forced_show(messy_db, tmp_path):
    out = _run(messy_db, tmp_path, "--anonymize-users", "--show", "good", "--shows", "1")
    text = "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert "alice_lb" not in text and "Alice" not in text
    assert "user1" in text
    shows = json.loads((out / "tv_shows.json").read_text())["shows"]
    assert [s["catalog"]["title"] for s in shows] == ["Good Show"]
    assert shows[0]["catalog"]["requested_by_users"] == "user1"
    assert "Sonarr" in (out / "samples" / "media_catalog.csv").read_text()      # system pseudo-users stay readable


def test_zip_option(messy_db, tmp_path):
    _run(messy_db, tmp_path, "--zip")
    assert (tmp_path / "out.zip").is_file()


def test_missing_database_exits_cleanly(tmp_path):
    with pytest.raises(SystemExit):
        export.main(["--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o")])


def test_works_on_empty_freshly_created_database(db, tmp_path):
    out = tmp_path / "empty"
    export.main(["--db", db.DB_FILE, "--out", str(out)])
    assert json.loads((out / "tv_shows.json").read_text())["total_tv_shows_in_catalog"] == 0
