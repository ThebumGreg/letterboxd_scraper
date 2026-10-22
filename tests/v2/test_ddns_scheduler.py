from synchub import ddns, scheduler, settings, tasks


class Resp:
    def __init__(self, body=None, status=200, text=""):
        self._body, self.status_code, self.text = body, status, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def route(monkeypatch, table):
    calls = []

    def fake(service, event, method, url, **kw):
        calls.append((event, kw.get("json")))
        return table[event]() if callable(table[event]) else table[event]
    monkeypatch.setattr(ddns.http, "request", fake)
    return calls


def configure(v2, **extra):
    for k, v in {"dynu_domain": "home.dynu.net", "dynu_api_key": "KEY", "dynu_enabled": "1", **extra}.items():
        settings.set_value(k, v)


def test_disabled_and_missing_settings_are_reported(v2, monkeypatch):
    route(monkeypatch, {})
    assert ddns.run_update() == (False, "DDNS update skipped: disabled in settings.")
    assert settings.get("ddns_last_status") == "SKIPPED"
    settings.set_value("dynu_enabled", "1")
    assert ddns.run_update()[0] is False and "missing" in settings.get("ddns_last_message")


def test_update_when_ip_changed(v2, monkeypatch):
    configure(v2, dynu_dns_id="42", ddns_last_ip="1.1.1.1")
    calls = route(monkeypatch, {"GetPublicIP": Resp(text="2.2.2.2\n"), "UpdateIP": Resp(status=200)})
    ok, message = ddns.run_update()
    assert ok and "2.2.2.2" in message and settings.get("ddns_last_ip") == "2.2.2.2"
    assert calls[-1] == ("UpdateIP", {"name": "home.dynu.net", "ipv4Address": "2.2.2.2", "ipv4": True})
    assert ddns.history()[0]["status"] == "SUCCESS" and ddns.history()[0]["old_ip"] == "1.1.1.1"


def test_no_update_when_ip_unchanged_unless_forced(v2, monkeypatch):
    configure(v2, dynu_dns_id="42", ddns_last_ip="2.2.2.2")
    calls = route(monkeypatch, {"GetPublicIP": Resp(text="2.2.2.2"), "UpdateIP": Resp(status=200)})
    assert "No IP change" in ddns.run_update()[1] and [c[0] for c in calls] == ["GetPublicIP"]
    ddns.run_update(force=True)
    assert [c[0] for c in calls][-1] == "UpdateIP"


def test_failures_are_logged(v2, monkeypatch):
    configure(v2, dynu_dns_id="42")
    route(monkeypatch, {"GetPublicIP": Resp(text="2.2.2.2"), "UpdateIP": Resp(status=500, text="boom")})
    ok, message = ddns.run_update()
    assert not ok and "HTTP 500" in message and ddns.history()[0]["status"] == "FAILED"
    route(monkeypatch, {"GetPublicIP": Resp(status=500)})
    assert ddns.run_update()[1] == "DDNS update failed: public IP check failed."


def test_dns_id_is_looked_up_and_cached(v2, monkeypatch):
    configure(v2)
    route(monkeypatch, {"GetPublicIP": Resp(text="2.2.2.2"), "UpdateIP": Resp(),
                        "GetDNS": Resp({"domains": [{"id": 77, "name": "HOME.dynu.net", "state": "Complete", "ttl": 90, "ipv4Address": "9.9.9.9"}]})})
    assert ddns.run_update()[0]
    assert settings.get("dynu_dns_id") == "77" and settings.get("dynu_ttl") == "90"


def test_save_settings_keeps_key_when_blank_and_forgets_ids_on_change(v2, monkeypatch):
    configure(v2, dynu_dns_id="42")
    fetched = []
    monkeypatch.setattr(ddns, "fetch_metadata", lambda d, k: fetched.append((d, k)))
    ddns.save_settings("home.dynu.net", "", True)
    assert settings.get("dynu_api_key") == "KEY" and settings.get("dynu_dns_id") == "42" and fetched == []
    ddns.save_settings("other.dynu.net", "", True)
    assert settings.get("dynu_api_key") == "KEY" and settings.get("dynu_dns_id") == "" and fetched == [("other.dynu.net", "KEY")]


def test_scheduler_jobs_follow_settings_and_survive_bad_values(v2):
    settings.set_value("scraper_interval_hours", "abc")
    settings.set_value("radarr_interval_hours", "0")
    settings.set_value("sonarr_interval_hours", "-3")
    settings.set_value("jellyfin_interval_hours", "12")
    scheduler.configure()
    jobs = {j.id: j.trigger.interval.total_seconds() / 3600 for j in scheduler.scheduler.get_jobs() if hasattr(j.trigger, "interval")}
    assert jobs == {"job_ddns": 5 / 60, "job_scrape": 24, "job_jellyfin": 12, "job_sonarr": 6, "job_queue_poll": 30 / 3600}      # radarr off, bad values -> defaults


def test_radarr_task_auto_adds_when_enabled_and_resyncs(v2, monkeypatch):
    from .test_automation import FakeRadarr
    from .test_ingest_arr import FakeRadarr as LibRadarr

    class Both(FakeRadarr, LibRadarr):
        def __init__(self):
            FakeRadarr.__init__(self, known=("949",))
            self._movies, self._queue = [], []

    client = Both()
    monkeypatch.setattr(tasks, "RadarrClient", lambda: client)
    with v2.connection() as c:
        c.execute("INSERT INTO profile (name, letterboxd_username) VALUES ('A', 'alice')")
        mid = c.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'Heat', '949')").lastrowid
        c.execute("INSERT INTO src_letterboxd_entry (username, slug, list, first_seen, last_seen, tmdb_id) VALUES ('alice', 'heat', 'watchlist', 'a', 'a', '949')")
    tasks.sync_radarr()
    assert client.added == []                                         # queue addition is off by default
    settings.set_value("radarr_enable_queue_addition", "1")
    tasks.sync_radarr()
    assert client.added and "Auto-add: added 1" in _status(v2, "radarr")
    settings.set_value("radarr_dry_run", "1")
    client.added.clear()
    tasks.sync_radarr()
    assert client.added == [] and "dry run" in _status(v2, "radarr")

    settings.set_value("radarr_dry_run", "0")
    settings.set_value("radarr_root_folder", "/gone")
    tasks.sync_radarr()
    status = _status(v2, "radarr")
    assert client.added == [] and "BLOCKED, 1 waiting" in status and "root folder '/gone'" in status


def _status(v2, name):
    with v2.connection() as c:
        return c.execute("SELECT message FROM task_status WHERE task_name = ?", (name,)).fetchone()[0]
