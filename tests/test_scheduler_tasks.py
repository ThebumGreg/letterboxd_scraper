import scheduler


def _status(db, name):
    with db.get_connection() as conn:
        return dict(conn.execute("SELECT * FROM task_status WHERE task_name = ?", (name,)).fetchone())


def test_serializd_task_runs_the_real_scraper(db, monkeypatch):
    import serializd_scraper
    calls = []
    monkeypatch.setattr(serializd_scraper, "run_serializd_scraper_task", lambda: calls.append(1))
    monkeypatch.setattr(scheduler, "reconcile_master_catalog", lambda: None)

    scheduler.run_serializd_scraper_task()

    assert calls == [1]
    assert _status(db, "scraper_serializd")["status"] == "completed"


def test_serializd_task_imports_watchlist_for_active_profiles(db, monkeypatch):
    import serializd_scraper

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"shows": [{"id": 7, "name": "Some Show", "tvdbId": 111, "tmdbId": 222}]}

    monkeypatch.setattr(serializd_scraper.requests, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(scheduler, "reconcile_master_catalog", lambda: None)
    db.save_user_profile_mapping("Alice", serializd_username="alice_sz")
    db.save_user_profile_mapping("NoTv", letterboxd_username="lb_only")

    scheduler.run_serializd_scraper_task()

    with db.get_connection() as conn:
        rows = conn.execute("SELECT username, movie_slug, tvdb_id, media_type, letterboxd_lists FROM user_requests").fetchall()
    assert [tuple(r) for r in rows] == [("alice_sz", "serializd-7", "111", "tv", "Watchlist")]
    assert _status(db, "scraper_serializd")["status"] == "completed"


def test_serializd_task_failure_is_reported(db, monkeypatch):
    import serializd_scraper

    def boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(serializd_scraper, "run_serializd_scraper_task", boom)
    monkeypatch.setattr(scheduler, "reconcile_master_catalog", lambda: None)

    scheduler.run_serializd_scraper_task()

    status = _status(db, "scraper_serializd")
    assert status["status"] == "failed" and "nope" in status["message"]


def test_failed_jellyfin_sync_is_reported_as_failed(db, monkeypatch):
    import jellyfin_integration
    monkeypatch.setattr(jellyfin_integration, "sync_jellyfin_library", lambda media_type="all": False)

    scheduler.run_jellyfin_movies_task()
    scheduler.run_jellyfin_tv_task()

    assert _status(db, "jellyfin_movies")["status"] == "failed"
    assert _status(db, "jellyfin_tv")["status"] == "failed"


def test_successful_jellyfin_sync_is_completed_and_uses_right_media_type(db, monkeypatch):
    import jellyfin_integration
    seen = []
    monkeypatch.setattr(jellyfin_integration, "sync_jellyfin_library", lambda media_type="all": seen.append(media_type) or True)

    scheduler.run_jellyfin_movies_task()
    scheduler.run_jellyfin_tv_task()

    assert seen == ["movie", "tv"]
    assert _status(db, "jellyfin_movies")["status"] == "completed"
    assert _status(db, "jellyfin_tv")["status"] == "completed"


def test_id_resolver_task_reconciles_catalog(db, monkeypatch):
    import id_resolver
    calls = []
    monkeypatch.setattr(id_resolver, "resolve_missing_ids", lambda: calls.append("resolve"))
    monkeypatch.setattr(scheduler, "reconcile_master_catalog", lambda: calls.append("reconcile"))

    scheduler.run_id_resolver_task()

    assert calls == ["resolve", "reconcile"]


def test_id_resolver_task_reconciles_even_when_it_fails(db, monkeypatch):
    import id_resolver
    calls = []

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(id_resolver, "resolve_missing_ids", boom)
    monkeypatch.setattr(scheduler, "reconcile_master_catalog", lambda: calls.append("reconcile"))

    scheduler.run_id_resolver_task()

    assert calls == ["reconcile"]
    assert _status(db, "id_resolver")["status"] == "failed"


def test_tools_page_has_id_resolver_button(client):
    html = client.get("/tools").get_data(as_text=True)
    assert "/tools/trigger-id-resolver" in html


def test_scheduler_survives_corrupt_interval_settings(db):
    db.set_setting("scraper_interval_hours", "abc")
    db.set_setting("radarr_interval_hours", "-5")
    db.set_setting("sonarr_interval_hours", "")
    db.set_setting("jellyfin_interval_hours", "12")

    scheduler.reconfigure_scheduler()  # must not raise

    jobs = {job.id: job for job in scheduler.scheduler.get_jobs()}
    assert jobs["job_lb_scraper"].trigger.interval.total_seconds() == 24 * 3600   # fell back to the default
    assert jobs["job_radarr"].trigger.interval.total_seconds() == 6 * 3600
    assert jobs["job_jf_tv"].trigger.interval.total_seconds() == 12 * 3600
