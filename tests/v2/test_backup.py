"""Moving and protecting the database: consistent copies, checking one before use, restoring, the copy before an upgrade, and the Docker settings."""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from synchub import backup, db, scheduler, settings, tasks
from synchub.web import create_app

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """Backups go in a temporary folder of their own (not beside the test database, not in the project)."""
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(backup, "BACKUP_DIR", out / "backups")
    return out


def rows(path, sql="SELECT name FROM profile ORDER BY name"):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute(sql)]
    finally:
        conn.close()


def old_database(path, upto):
    """A database as an older SyncHub left it: only the first `upto` migrations applied."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    for version, sql_path in db._migration_files():
        if version <= upto:
            conn.executescript(f"BEGIN;\n{sql_path.read_text(encoding='utf-8')}\nINSERT INTO schema_version VALUES ({version}, '{sql_path.stem}', 'x');\nCOMMIT;")
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- making a copy
def test_a_copy_holds_everything_even_what_is_still_in_the_wal_and_is_one_file(v2, folder, conn):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    assert Path(db.DB_FILE + "-wal").exists() and Path(db.DB_FILE + "-wal").stat().st_size > 0        # the new row is in the -wal, not in the main file
    copy = backup.create(folder / "copy.db")
    assert rows(copy) == ["Alice"]
    assert not Path(str(copy) + "-wal").exists() and sqlite3.connect(copy).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not list(folder.glob("*.partial"))
    plain = folder / "plain-copy.db"
    plain.write_bytes(Path(db.DB_FILE).read_bytes())                                                    # copying the file by hand misses it: this is why the tool exists
    assert rows(plain) == []


def test_a_copy_is_named_by_the_time_goes_in_the_backup_folder_and_never_overwrites(v2, folder):
    first = backup.create(now=datetime(2026, 1, 2, 3, 4, 5))
    assert first == folder / "backups" / "synchub-20260102-030405.db" and first.exists()
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.create(first)


def test_there_is_nothing_to_copy_when_there_is_no_database(folder, tmp_path):
    with pytest.raises(backup.BackupError, match="no database"):
        backup.create(folder / "x.db", source=tmp_path / "missing.db")
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(backup.BackupError, match="no database"):
        backup.create(folder / "x.db", source=empty)


def test_a_damaged_database_is_reported_and_leaves_nothing_behind(folder, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database, " * 200)
    with pytest.raises(backup.BackupError, match="could not be copied"):
        backup.create(folder / "x.db", source=bad)
    assert not (folder / "x.db").exists() and not list(folder.glob("*.partial"))


def test_a_copy_that_fails_the_integrity_check_is_removed_and_reported(v2, folder, monkeypatch):
    real = sqlite3.connect

    class Corrupt(sqlite3.Connection):
        def execute(self, sql, *a):
            if "integrity_check" in sql.lower():
                return super().execute("SELECT 'row 3 missing from index ix_profile'")                   # what SQLite says about a damaged file
            return super().execute(sql, *a)
    calls = []

    def connect(path, *a, **k):
        calls.append(path)
        return real(path, *a, **({**k, "factory": Corrupt} if len(calls) == 2 else k))                   # the second connection is the copy
    monkeypatch.setattr(backup.sqlite3, "connect", connect)
    with pytest.raises(backup.BackupError, match=r"did not pass SQLite's integrity check \(row 3 missing"):
        backup.create(folder / "x.db")
    assert not (folder / "x.db").exists() and not list(folder.glob("*.partial"))


# ---------------------------------------------------------------- checking one
def test_a_check_says_what_is_in_it_without_changing_it(v2, folder, conn):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice'), ('Bob')")
    settings.set_value("jellyfin_url", "http://192.168.1.10:8096", conn=conn)
    conn.commit()
    copy = backup.create(folder / "c.db")
    before = (copy.stat().st_size, copy.stat().st_mtime_ns)
    report = backup.check(copy)
    assert report["ok"] and report["integrity"] == "ok" and report["schema"] == report["app_schema"] == backup.latest_version()
    assert report["counts"]["profiles"] == 2 and report["counts"]["titles"] == 0 and report["local_addresses"] == [] and not report["newer_than_app"] and not report["wal_beside"]
    assert (copy.stat().st_size, copy.stat().st_mtime_ns) == before and sorted(p.name for p in folder.iterdir() if p.is_file()) == ["c.db"]      # no -wal or -shm appeared


def test_addresses_that_mean_this_machine_are_found_before_they_break_in_a_container(v2, folder, conn):
    for key, value in (("jellyfin_url", "http://localhost:8096"), ("radarr_url", "http://127.0.0.1:7878"), ("sonarr_url", "http://tower:8989"), ("synchub_url", "http://[::1]:5001")):
        settings.set_value(key, value, conn=conn)
    conn.commit()
    report = backup.check(backup.create(folder / "c.db"))
    assert report["local_addresses"] == ["jellyfin_url", "radarr_url", "synchub_url"]
    assert backup.addresses_on_this_machine({"jellyfin_url": "http://0.0.0.0:1", "radarr_url": "", "sonarr_url": None}) == ["jellyfin_url"]
    assert backup.addresses_on_this_machine({"jellyfin_url": "http://LocalHost:8096/"}) == ["jellyfin_url"]
    assert backup.addresses_on_this_machine({"jellyfin_url": "http://jellyfin.local:8096"}) == []


def test_a_database_newer_than_this_version_or_from_something_else_is_not_ok(v2, folder, conn, tmp_path):
    newer = backup.create(folder / "newer.db")
    c = sqlite3.connect(newer)
    c.execute("INSERT INTO schema_version VALUES (99, 'future', 'x')")
    c.commit()
    c.close()
    report = backup.check(newer)
    assert report["newer_than_app"] and not report["ok"] and report["schema"] == 99
    other = tmp_path / "other.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE notes (x)")
    c.commit()
    c.close()
    report = backup.check(other)
    assert report["schema"] is None and not report["ok"] and report["integrity"] == "ok"


def test_a_file_that_is_not_a_database_or_is_missing_is_refused(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"x" * 5000)
    with pytest.raises(backup.BackupError, match="not a usable SQLite database"):
        backup.check(junk)
    with pytest.raises(backup.BackupError, match="missing or empty"):
        backup.check(tmp_path / "nothing.db")


def test_a_wal_file_left_beside_a_copy_is_pointed_out(v2, folder):
    copy = backup.create(folder / "c.db")
    Path(str(copy) + "-wal").write_bytes(b"recent changes")
    assert backup.check(copy)["wal_beside"] is True
    assert "non-empty -wal file" in backup.describe(backup.check(copy))


def test_the_report_reads_in_plain_words(v2, folder, conn):
    settings.set_value("jellyfin_url", "http://localhost:8096", conn=conn)
    conn.commit()
    text = backup.describe(backup.check(backup.create(folder / "c.db")))
    assert "integrity: ok" in text and "FIX AFTER MOVING: jellyfin_url is http://localhost:8096" in text and "holds:" in text


# ---------------------------------------------------------------- restoring
def test_a_restore_puts_the_copy_in_place_and_keeps_what_was_there(v2, folder, conn):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    copy = backup.create(folder / "alice.db")
    conn.execute("INSERT INTO profile (name) VALUES ('Later')")
    conn.commit()
    target, kept = backup.restore(copy, now=datetime(2026, 5, 6, 7, 8, 9))
    assert target == Path(db.DB_FILE) and rows(target) == ["Alice"]
    assert kept.name == "synchub_test.db.before-restore-20260506-070809" and rows(kept) == ["Alice", "Later"]
    assert not Path(db.DB_FILE + "-wal").exists() and not Path(db.DB_FILE + "-shm").exists()
    assert Path(str(kept) + "-wal").exists()                                        # the old database's unsaved changes went with it, not into the bin
    assert db.migrate() == backup.latest_version()                                  # and the app can use the restored one


def test_a_restore_onto_nothing_just_copies_and_clears_orphaned_sidecars(folder, tmp_path, v2):
    copy = backup.create(folder / "c.db")
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "synchub.db-wal").write_bytes(b"orphan")
    target, kept = backup.restore(copy, dest=tmp_path / "new" / "synchub.db")
    assert kept is None and target.exists() and not (tmp_path / "new" / "synchub.db-wal").exists()


def test_a_restore_refuses_what_it_should_not_use_and_changes_nothing(v2, folder, conn, tmp_path):
    good = backup.create(folder / "good.db")
    newer = folder / "newer.db"
    newer.write_bytes(good.read_bytes())
    c = sqlite3.connect(newer)
    c.execute("INSERT INTO schema_version VALUES (99, 'future', 'x')")
    c.commit()
    c.close()
    other = tmp_path / "other.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE notes (x)")
    c.commit()
    c.close()
    before = Path(db.DB_FILE).read_bytes()
    for bad, message in ((newer, "only knows up to"), (other, "not a SyncHub one")):
        with pytest.raises(backup.BackupError, match=message):
            backup.restore(bad)
    assert Path(db.DB_FILE).read_bytes() == before and not list(Path(db.DB_FILE).parent.glob("*.before-restore-*"))


# ---------------------------------------------------------------- keeping a few
def test_only_the_newest_nightly_copies_are_kept_and_the_copies_before_upgrades_are_not_touched(v2, folder):
    folder_ = folder / "backups"
    folder_.mkdir()
    for day in range(1, 6):
        (folder_ / f"synchub-2026010{day}-033000.db").write_bytes(b"x")
    (folder_ / "premigrate-v6-20250101-000000.db").write_bytes(b"x")
    (folder_ / "notes.txt").write_text("keep")
    assert backup.prune(3) == ["synchub-20260101-033000.db", "synchub-20260102-033000.db"]
    assert sorted(p.name for p in folder_.iterdir()) == ["notes.txt", "premigrate-v6-20250101-000000.db", "synchub-20260103-033000.db", "synchub-20260104-033000.db", "synchub-20260105-033000.db"]
    assert backup.prune(0) == [] and backup.prune(10) == []
    settings.set_value("backup_keep", "garbage")
    assert backup.keep_count() == 7
    settings.set_value("backup_keep", "-2")
    assert backup.keep_count() == 0


def test_the_newest_copy_of_either_kind_is_found(folder):
    assert backup.latest() is None
    folder_ = folder / "backups"
    folder_.mkdir()
    old, new = folder_ / "synchub-1.db", folder_ / "premigrate-v6-2.db"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    assert backup.latest()[0] == new


# ---------------------------------------------------------------- before an upgrade
def test_an_older_database_is_saved_as_it_was_before_starting_changes_it(v2, folder, monkeypatch):
    Path(db.DB_FILE).unlink()
    old_database(db.DB_FILE, upto=6)
    saved = backup.copy_before_upgrade(now=datetime(2026, 2, 3, 4, 5, 6))
    assert saved == folder / "backups" / "premigrate-v6-20260203-040506.db"
    assert rows(saved) == ["Alice"] and backup.check(saved)["schema"] == 6                    # as it was: version 6, not upgraded


def test_starting_the_app_on_an_older_database_upgrades_it_and_keeps_a_copy_and_the_data(v2, folder):
    Path(db.DB_FILE).unlink()
    old_database(db.DB_FILE, upto=6)
    app = create_app(csrf=False)
    [copy] = list((folder / "backups").glob("premigrate-v6-*.db"))
    assert backup.check(copy)["schema"] == 6 and rows(copy) == ["Alice"]
    assert rows(db.DB_FILE) == ["Alice"] and backup.check(db.DB_FILE)["schema"] == backup.latest_version()
    assert app.test_client().get("/healthz").get_json()["schema"] == backup.latest_version()
    create_app(csrf=False)                                                                    # starting again does not make another
    assert len(list((folder / "backups").glob("premigrate-*.db"))) == 1


def test_no_copy_is_made_for_a_new_current_or_foreign_database(v2, folder, tmp_path):
    assert backup.copy_before_upgrade() is None                                              # the fixture's database is current
    Path(db.DB_FILE).unlink()
    assert backup.copy_before_upgrade() is None                                              # none at all
    Path(db.DB_FILE).write_bytes(b"")
    assert backup.copy_before_upgrade() is None                                              # empty
    c = sqlite3.connect(db.DB_FILE)
    c.execute("CREATE TABLE notes (x)")
    c.commit()
    c.close()
    assert backup.copy_before_upgrade() is None                                              # not a SyncHub database
    Path(db.DB_FILE).write_bytes(b"garbage " * 500)
    assert backup.copy_before_upgrade() is None                                              # unreadable: left for migrate to report


def test_a_failed_safety_copy_does_not_stop_the_app_starting(v2, folder, monkeypatch, caplog):
    monkeypatch.setattr(backup, "copy_before_upgrade", lambda: (_ for _ in ()).throw(RuntimeError("disk full")))
    create_app(csrf=False)
    assert "Could not save a copy of the database before upgrading it: disk full" in caplog.text


# ---------------------------------------------------------------- the nightly job and the button
def test_the_nightly_task_saves_a_copy_and_removes_the_surplus(v2, folder):
    settings.set_value("backup_keep", "2")
    for day in (1, 2, 3):
        backup.create(now=datetime(2026, 1, day, 3, 30))
    tasks.backup_database()
    status = tasks.all_statuses()["backup"]
    assert status["status"] == "completed" and "Saved synchub-" in status["message"] and "Removed 2 older copies" in status["message"]
    assert len(list((folder / "backups").glob("synchub-*.db"))) == 2


def test_the_nightly_task_does_nothing_when_switched_off(v2, folder):
    settings.set_value("backup_keep", "0")
    tasks.backup_database()
    assert "turned off" in tasks.all_statuses()["backup"]["message"] and not (folder / "backups").exists()


def test_the_nightly_job_is_scheduled_at_half_past_three_unless_switched_off(v2):
    scheduler.configure()
    job = scheduler.scheduler.get_job("job_backup")
    assert job is not None and str(job.trigger) == "cron[hour='3', minute='30']"
    settings.set_value("backup_keep", "0")
    scheduler.configure()
    assert scheduler.scheduler.get_job("job_backup") is None
    scheduler.scheduler.remove_all_jobs()


def test_the_tools_button_runs_a_backup_and_settings_shows_the_newest(client, v2, folder, monkeypatch):
    monkeypatch.setattr(tasks, "start_async", lambda fn: fn())
    assert "Database backup" in client.get("/tools").get_data(as_text=True)
    page = client.post("/tools/trigger/backup", follow_redirects=True).get_data(as_text=True)
    assert "Database backup started" in page
    settings_page = client.get("/settings").get_data(as_text=True)
    [made] = (folder / "backups").glob("synchub-*.db")
    assert 'id="databaseBackups"' in settings_page and 'name="backup_keep" value="7"' in settings_page and made.name in settings_page


def test_the_backup_setting_is_validated(v2):
    from settings_validation import validate_setting
    assert validate_setting("backup_keep", "0") == (True, "0") and validate_setting("backup_keep", "365")[0]
    assert not validate_setting("backup_keep", "-1")[0] and not validate_setting("backup_keep", "366")[0] and not validate_setting("backup_keep", "x")[0]


# ---------------------------------------------------------------- Docker: the warning, the paths, the health check
def test_inside_docker_the_settings_page_warns_about_addresses_that_mean_the_container(client, v2, monkeypatch):
    settings.set_value("jellyfin_url", "http://localhost:8096")
    settings.set_value("radarr_url", "http://192.168.1.10:7878")
    assert 'id="localAddressWarning"' not in client.get("/settings").get_data(as_text=True)               # on a machine that runs everything it is fine
    monkeypatch.setenv("SYNCHUB_IN_DOCKER", "1")
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="localAddressWarning"' in page and "<code>jellyfin_url</code>" in page and "<code>radarr_url</code>" not in page
    settings.set_value("jellyfin_url", "http://192.168.1.10:8096")
    assert 'id="localAddressWarning"' not in client.get("/settings").get_data(as_text=True)


def test_one_folder_holds_everything_when_a_data_folder_is_named(tmp_path):
    code = "import config; print(config.DATA_DIR, config.LOGS_DIR, config.POSTER_DIR, config.BACKUP_DIR, sep='|')"
    env = {**os.environ, "SYNCHUB_DATA_DIR": str(tmp_path / "data")}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, check=True).stdout.strip().split("|")
    root = str(tmp_path / "data")
    assert out == [root, root + "/logs", root + "/posters", root + "/backups"]
    assert all((tmp_path / "data" / name).is_dir() for name in ("logs", "posters", "backups"))
    env.pop("SYNCHUB_DATA_DIR")
    plain = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, check=True).stdout.strip().split("|")
    assert plain == [str(REPO / "data"), str(REPO / "logs"), str(REPO / "static" / "posters"), str(REPO / "data" / "backups")]       # a checkout works as before


def test_the_health_check_answers_without_asking_who_you_are(app, v2, conn, monkeypatch):
    import synchub.web as web
    conn.execute("INSERT INTO profile (name, pin_hash, permissions) VALUES ('Locked', 'x', '')")
    conn.commit()
    monkeypatch.setattr(web, "_profile_rows", lambda c: pytest.fail("the health check must not look anyone up"))
    answer = app.test_client().get("/healthz")
    assert answer.status_code == 200 and answer.get_json() == {"ok": True, "schema": backup.latest_version()}


def test_the_health_check_fails_when_the_database_does_not_answer(app, monkeypatch):
    import synchub.web.pages as pages
    class Broken:
        def connection(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(pages, "db", Broken())
    answer = app.test_client().get("/healthz")
    assert answer.status_code == 503 and answer.get_json() == {"ok": False}


# ---------------------------------------------------------------- the command line
def test_the_command_line_creates_checks_lists_and_restores(v2, folder, conn, capsys):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    assert backup.main(["create"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Saved ") and "synchub-" in out
    [made] = (folder / "backups").glob("synchub-*.db")
    assert backup.main(["list"]) == 0 and made.name in capsys.readouterr().out
    assert backup.main(["check", str(made)]) == 0 and "integrity: ok" in capsys.readouterr().out
    assert backup.main(["create", str(folder / "named.db")]) == 0 and (folder / "named.db").exists()
    assert backup.main(["restore", str(made)]) == 0 and "Restored" in capsys.readouterr().out and rows(db.DB_FILE) == ["Alice"]


def test_the_command_line_explains_itself_and_reports_problems_with_a_failing_exit_code(v2, folder, tmp_path, capsys):
    for argv in ([], ["bogus"], ["check"], ["restore"], ["create", "a", "b"]):
        assert backup.main(argv) == 2 and "usage:" in capsys.readouterr().out
    assert backup.main(["check", str(tmp_path / "missing.db")]) == 1 and "missing or empty" in capsys.readouterr().out
    assert backup.main(["restore", str(tmp_path / "missing.db")]) == 1
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"x" * 5000)
    assert backup.main(["check", str(junk)]) == 1


def test_it_runs_as_a_module(v2, folder, tmp_path):
    env = {**os.environ, "SYNCHUB_DATA_DIR": str(tmp_path / "data")}
    run = subprocess.run([sys.executable, "-m", "synchub.backup", "list"], cwd=REPO, env=env, capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout == ""
    assert subprocess.run([sys.executable, "-m", "synchub.backup"], cwd=REPO, env=env, capture_output=True, text=True).returncode == 2


# ---------------------------------------------------------------- the TV settings travel with a copy
@pytest.fixture
def tv_folder(v2):
    """The TV pages' folder, which sits beside the (test) database: settings, a pairing identity, and a call log that is not worth keeping."""
    tv = Path(db.DB_FILE).parent / "tv"
    (tv / "certs").mkdir(parents=True)
    (tv / "prefs.json").write_text('{"devices": [{"id": "den"}]}')
    (tv / "devices.json").write_text('{"devices": []}')
    (tv / "certs" / "client-cert.pem").write_text("CERT")
    (tv / "certs" / "client-key.pem").write_text("KEY")
    (tv / "tv.log").write_text("a call log")
    return tv


def test_a_copy_comes_with_the_tv_settings_but_not_the_call_log(v2, folder, tv_folder):
    copy = backup.create()
    archive = backup.tv_archive_for(copy)
    assert archive.name == copy.name[:-3] + ".tv.zip" and archive.parent == copy.parent
    assert sorted(backup.tv_archive_names(archive)) == ["certs/client-cert.pem", "certs/client-key.pem", "devices.json", "prefs.json"]


def test_no_archive_is_made_when_there_are_no_tv_settings_or_for_the_copy_before_an_upgrade(v2, folder, tmp_path):
    assert not backup.tv_archive_for(backup.create()).exists()
    (Path(db.DB_FILE).parent / "tv").mkdir()
    (Path(db.DB_FILE).parent / "tv" / "prefs.json").write_text("{}")
    assert not backup.tv_archive_for(backup.create(folder / "plain.db", with_tv=False)).exists()
    assert backup.tv_archive_for(backup.create(folder / "withtv.db")).exists()


def test_pruning_removes_the_tv_settings_of_the_copies_it_removes(v2, folder, tv_folder):
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        backup.create(now=datetime.strptime(stamp, "%Y%m%d-%H%M%S"))
    assert backup.prune(1) == ["synchub-20260101-000000.db", "synchub-20260102-000000.db"]
    assert sorted(p.name for p in backup.BACKUP_DIR.glob("*")) == ["synchub-20260103-000000.db", "synchub-20260103-000000.tv.zip"]


def test_a_check_says_whether_the_tv_settings_are_there(v2, folder, tv_folder):
    copy = backup.create()
    report = backup.check(copy)
    assert sorted(report["tv_archive"]) == ["certs/client-cert.pem", "certs/client-key.pem", "devices.json", "prefs.json"]
    assert "also holds: the TV settings (4 file(s)" in backup.describe(report)
    backup.tv_archive_for(copy).unlink()
    assert backup.check(copy)["tv_archive"] is None and "no TV settings beside it" in backup.describe(backup.check(copy))


def test_a_restore_brings_the_tv_settings_back_and_keeps_the_ones_it_replaces(v2, folder, tv_folder, tmp_path):
    copy = backup.create()
    where = tmp_path / "elsewhere" / "synchub.db"
    other = where.parent / "tv"
    other.mkdir(parents=True)
    (other / "prefs.json").write_text("the old settings")
    backup.restore(copy, dest=where)
    assert (other / "prefs.json").read_text() == '{"devices": [{"id": "den"}]}' and (other / "certs" / "client-key.pem").read_text() == "KEY"
    kept = [p for p in where.parent.glob("tv.before-restore-*")]
    assert len(kept) == 1 and (kept[0] / "prefs.json").read_text() == "the old settings"
    assert not list(where.parent.glob("tv-restore-*"))                                       # no staging folder left behind


def test_a_restore_without_tv_settings_leaves_the_tv_folder_alone(v2, folder, tmp_path):
    copy = backup.create()
    where = tmp_path / "elsewhere" / "synchub.db"
    (where.parent / "tv").mkdir(parents=True)
    (where.parent / "tv" / "prefs.json").write_text("mine")
    backup.restore(copy, dest=where)
    assert (where.parent / "tv" / "prefs.json").read_text() == "mine"


def test_an_archive_with_anything_but_tv_settings_is_refused_before_anything_is_changed(v2, folder, tv_folder, tmp_path):
    import zipfile
    copy = backup.create()
    with zipfile.ZipFile(backup.tv_archive_for(copy), "w") as zf:
        zf.writestr("../evil.txt", "x")
    where = tmp_path / "elsewhere" / "synchub.db"
    where.parent.mkdir()
    where.write_text("the current database")
    with pytest.raises(backup.BackupError, match="do not belong to the TV settings"):
        backup.restore(copy, dest=where)
    assert where.read_text() == "the current database" and not (tmp_path / "evil.txt").exists()
    backup.tv_archive_for(copy).write_bytes(b"not a zip")
    with pytest.raises(backup.BackupError, match="not a readable TV settings archive"):
        backup.restore(copy, dest=where)


def test_the_nightly_task_saves_the_tv_settings_too(v2, folder, tv_folder):
    tasks.backup_database()
    assert len(list(backup.BACKUP_DIR.glob("synchub-*.tv.zip"))) == 1


def test_the_command_line_mentions_the_tv_settings(v2, folder, tv_folder, tmp_path, capsys):
    assert backup.main(["create", str(tmp_path / "c.db")]) == 0
    assert "and the TV settings in c.tv.zip" in capsys.readouterr().out
    assert backup.main(["check", str(tmp_path / "c.db")]) == 0
    assert "also holds: the TV settings" in capsys.readouterr().out
