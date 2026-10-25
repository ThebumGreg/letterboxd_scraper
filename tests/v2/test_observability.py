"""Troubleshooting aids: progress messages, task logging, lock reporting, diagnostics."""
import logging
import sqlite3
import threading
import time

import pytest

from synchub import db, diagnostics, http, settings, tasks
from synchub.ingest import letterboxd as lb
from synchub.ingest import letterboxd_ids, sonarr
from synchub.matching import Matcher

from .fakes import FakeTmdb
from .test_ingest_arr import FakeSonarr
from .test_letterboxd_ids import entry, film_page


def row(name):
    with db.connection() as c:
        return dict(c.execute("SELECT * FROM task_status WHERE task_name = ?", (name,)).fetchone())


# ---------------------------------------------------------------- task logging and progress
def test_task_logs_start_duration_and_summary(v2, caplog):
    with caplog.at_level(logging.INFO, logger="synchub.tasks"):
        tasks.run("demo", "Demo job", lambda: "all done")
    messages = [r.getMessage() for r in caplog.records]
    assert "[demo] Demo job: started" in messages
    assert any(m.startswith("[demo] finished in ") and m.endswith("all done") for m in messages)


def test_failed_task_logs_the_full_traceback_and_stores_the_message(v2, caplog):
    def boom():
        raise ValueError("kaboom")
    with caplog.at_level(logging.INFO, logger="synchub.tasks"):
        tasks.run("demo", "Demo job", boom)
    failure = next(r for r in caplog.records if "failed after" in r.getMessage())
    assert "kaboom" in failure.getMessage() and failure.exc_info and "ValueError" in "".join(__import__("traceback").format_exception(*failure.exc_info))
    assert row("demo")["status"] == "failed" and row("demo")["message"] == "kaboom"


def test_skipped_duplicate_run_says_how_long_the_first_has_been_running(v2, caplog):
    started, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=lambda: tasks.run("slow", "Slow", lambda: (started.set(), release.wait(3), "x")[2]))
    worker.start(); started.wait(2)
    with caplog.at_level(logging.INFO, logger="synchub.tasks"):
        assert tasks.run("slow", "Slow", lambda: "second") is None
    release.set(); worker.join()
    assert any("[slow] is already running (for" in r.getMessage() for r in caplog.records)


def test_progress_updates_the_message_without_touching_the_start_time(v2, caplog):
    def work(report):
        report("step one", force=True)
        report("throttled away")                   # inside the 2 s window: logged but not stored
        return "done"
    tasks.set_status("demo", "in_progress", "starting")
    before = row("demo")["started_at"]
    reporter = tasks.Reporter("demo")
    with caplog.at_level(logging.INFO, logger="synchub.tasks"):
        reporter("first", force=True); reporter("second")
    assert row("demo")["message"] == "first" and row("demo")["started_at"] == before and row("demo")["status"] == "in_progress"
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("[demo]")] == ["[demo] first", "[demo] second"]


def test_progress_reaches_the_tools_page_while_a_task_runs(v2):
    seen = {}

    def work(report):
        report("Checked 40 of 150", force=True)
        seen["during"] = row("demo")["message"]
        return "finished"
    tasks.run("demo", "Demo", work)
    assert seen["during"] == "Checked 40 of 150" and row("demo")["message"] == "finished"


# ---------------------------------------------------------------- progress hooks in the long loops
def collect():
    messages = []
    return messages, lambda message, force=False: messages.append(message)


def test_letterboxd_scrape_reports_pages_users_and_results(v2):
    with v2.connection() as c:
        c.execute("INSERT INTO profile (name, letterboxd_username) VALUES ('A', 'alice')")
        c.execute("INSERT INTO profile (name, letterboxd_username) VALUES ('B', 'bob')")
    page = '<div class="react-component" data-item-slug="x" data-item-full-display-name="X (2000)"></div>'
    messages, progress = collect()
    lb.sync_all(fetch=lambda url: (200, f"<html><body>{page}</body></html>"), sleep=lambda s: None, progress=progress)
    assert any("alice/watchlist: page 1" in m for m in messages)
    assert any(m.startswith("Waiting ") and "before the next user (2 of 2: bob)" in m for m in messages)
    assert any("alice/watchlist: 1 film(s)" in m for m in messages)


def test_id_resolver_reports_totals_and_every_tenth_film(v2):
    with v2.connection() as c:
        for i in range(12):
            entry(c, f"film-{i}")
        c.commit()
        messages, progress = collect()
        letterboxd_ids.resolve(c, lambda url: (200, film_page(str(100 + len(messages)))), progress=progress, now="2026-01-10 00:00:00")
    assert messages[0] == "12 film(s) need an ID" and any(m.startswith("Checked 10 of 12") for m in messages)


def test_matcher_reports_every_25_records(conn):
    for i in range(60):
        conn.execute("INSERT INTO src_radarr_movie (radarr_id, tmdb_id, title, year, first_seen, last_seen) VALUES (?, ?, 'M', 2000, 'a', 'a')", (str(i), str(i + 1)))
    conn.commit()
    messages, progress = collect()
    Matcher(conn, None).resolve_all(progress=progress)
    assert messages == ["Matching 25 of 60 source records", "Matching 50 of 60 source records"]


def test_sonarr_reports_each_series(v2):
    client = FakeSonarr([{"id": 10, "tvdbId": 1, "title": "Alpha"}, {"id": 11, "tvdbId": 2, "title": "Beta"}], {})
    messages, progress = collect()
    with v2.connection() as c:
        sonarr.sync_library(c, client, progress=progress)
    assert messages == ["Sonarr: reading episodes of 'Alpha' (1 of 2)", "Sonarr: reading episodes of 'Beta' (2 of 2)"]


def test_run_all_reports_which_step_it_is_on(v2, monkeypatch):
    steps = ["sync_jellyfin_movies", "sync_jellyfin_tv", "scrape_letterboxd", "scrape_serializd", "resolve_letterboxd_ids", "reconcile", "sync_radarr", "sync_sonarr", "reconcile"]
    for name in set(steps):
        stub = lambda: None
        stub.__name__ = name                       # the real steps are named functions; the message is built from the name
        monkeypatch.setattr(tasks, name, stub)
    seen = []
    real = tasks.update_message
    monkeypatch.setattr(tasks, "update_message", lambda task, message: (seen.append(message), real(task, message)))
    tasks.run_all()
    assert "Step 1 of 9: sync jellyfin movies" in seen and "Step 9 of 9: reconcile" in seen


# ---------------------------------------------------------------- lock reporting and the guard
def test_lock_error_names_the_connection_that_holds_the_database(v2, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_TIMEOUT", 0.2)

    def hold(started, release):
        with db.connection() as c:
            c.execute("INSERT INTO app_setting (key, value) VALUES ('held', '1')")
            started.set(); release.wait(3)

    started, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=hold, args=(started, release), name="the-holder")
    holder.start(); started.wait(2)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            with db.connection() as c:
                c.execute("INSERT INTO app_setting (key, value) VALUES ('other', '1')")
    finally:
        release.set(); holder.join()
    message = str(caught.value)
    assert "database is locked" in message and "held by:" in message and "hold in thread 'the-holder'" in message and message.count("held by:") == 1


def test_guard_raises_in_tests_and_only_warns_in_the_app(v2, monkeypatch, caplog):
    with db.connection() as c:
        c.execute("INSERT INTO app_setting (key, value) VALUES ('x', '1')")        # write transaction now open on this thread
        with pytest.raises(db.NetworkInWriteTransaction, match="commit first"):
            db.assert_not_in_write_transaction("a slow call")
        c.commit()
        db.assert_not_in_write_transaction("a slow call")                           # fine once committed
        c.execute("INSERT INTO app_setting (key, value) VALUES ('y', '1')")
        monkeypatch.setattr(db, "STRICT_NETWORK_GUARD", False)
        with caplog.at_level(logging.WARNING, logger="synchub.db"):
            db.assert_not_in_write_transaction("a slow call")
    assert any("a slow call was started while a database write transaction" in r.getMessage() for r in caplog.records)


def test_payload_logging_failure_never_breaks_the_request(v2, monkeypatch, caplog):
    settings.set_value("payload_logging_enabled", "1"); settings.set_value("log_service_radarr", "1")

    class Resp:
        status_code, text = 200, "{}"
        def json(self): return {}
    monkeypatch.setattr(http.requests, "request", lambda *a, **k: Resp())
    monkeypatch.setattr(db, "DEFAULT_TIMEOUT", 0.2)
    started, release = threading.Event(), threading.Event()

    def hold():
        with db.connection() as c:
            c.execute("INSERT INTO app_setting (key, value) VALUES ('held', '1')")
            started.set(); release.wait(3)
    worker = threading.Thread(target=hold); worker.start(); started.wait(2)
    try:
        with caplog.at_level(logging.WARNING, logger="synchub.http"):
            assert http.request("radarr", "GetLibrary", "GET", "http://radarr/api").status_code == 200
    finally:
        release.set(); worker.join()
    assert any("Could not store the radarr GetLibrary payload log entry" in r.getMessage() for r in caplog.records)


def test_slow_http_calls_are_warned_about(v2, monkeypatch, caplog):
    class Resp:
        status_code, text = 200, "{}"
        def json(self): return {}
    times = iter([100.0, 107.5, 107.5, 107.5, 107.5, 107.5])
    monkeypatch.setattr(http.time, "time", lambda: next(times))
    monkeypatch.setattr(http.requests, "request", lambda *a, **k: Resp())
    with caplog.at_level(logging.WARNING, logger="synchub.http"):
        http.request("tmdb", "find", "GET", "https://api.themoviedb.org/3/find/1", params={"api_key": "SECRET"})
    warning = next(r.getMessage() for r in caplog.records if "in 7.5s" in r.getMessage())
    assert "tmdb find GET api.themoviedb.org/3/find/1 -> HTTP 200" in warning and "SECRET" not in warning


def test_non_wal_journal_mode_is_called_out(tmp_path, monkeypatch, caplog):
    class NoWal(sqlite3.Connection):
        pass
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "x.db"))
    real_connect = db.connect

    class Fake:
        """Wraps a real connection but pretends WAL is unavailable (as on a Windows-mounted drive)."""
        def __init__(self, conn): self._conn = conn
        def execute(self, sql, *a):
            if sql.startswith("PRAGMA journal_mode = WAL"):
                class R:
                    def fetchone(self): return ("delete",)
                return R()
            return self._conn.execute(sql, *a)
        def __getattr__(self, name): return getattr(self._conn, name)
    monkeypatch.setattr(db, "connect", lambda timeout=None: Fake(real_connect(timeout)))
    with caplog.at_level(logging.WARNING, logger="synchub.db"):
        db.migrate()
    assert any("could not switch to WAL mode" in r.getMessage() and "/mnt/c" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- diagnostics
def test_snapshot_shows_a_running_task_its_age_and_what_its_thread_is_doing(v2):
    started, release = threading.Event(), threading.Event()

    def slow(report):
        report("Checked 40 of 150", force=True)
        started.set(); release.wait(5)
        return "done"

    worker = threading.Thread(target=lambda: tasks.run("id_resolver", "Resolving", slow), name="resolver-thread")
    worker.start(); started.wait(2)
    try:
        snap = diagnostics.snapshot()
    finally:
        release.set(); worker.join()
    task = next(t for t in snap["running_tasks"] if t["task"] == "id_resolver")
    assert task["message"] == "Checked 40 of 150" and task["running_for_seconds"] is not None
    thread = next(t for t in snap["threads"] if t["thread"] == "resolver-thread")
    assert any("slow" in line for line in thread["stack"]) and "wait" in thread["waiting_in"].lower()
    assert snap["database"]["journal_mode"].lower() == "wal" and snap["database"]["schema_version"] == max(v for v, _ in db._migration_files())
    assert isinstance(snap["database_connections"], list)


def test_snapshot_lists_a_connection_with_a_pending_write(v2):
    with db.connection() as c:
        c.execute("INSERT INTO app_setting (key, value) VALUES ('x', '1')")
        mine = [r for r in diagnostics.snapshot()["database_connections"] if r["writing"] and r["thread"] == threading.current_thread().name]
    assert mine and "test_observability.py" in mine[0]["caller"]


def test_diagnostics_endpoint_and_page(v2):
    from synchub.web import create_app
    client = create_app(csrf=False).test_client()
    data = client.get("/api/activity").get_json()
    assert set(data) == {"time", "pid", "database", "running_tasks", "database_connections", "threads"}
    page = client.get("/activity").get_data(as_text=True)
    assert "What it&#39;s doing now" in page or "What it's doing now" in page
    assert "/api/activity" in page
    assert "/activity" in client.get("/tools").get_data(as_text=True)
    assert client.get("/diagnostics").status_code == 301
