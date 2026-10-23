import json

import pytest

from synchub import http, settings, tasks
from synchub.clients import ApiError, ConfigError
from synchub.clients.arr import RadarrClient, SonarrClient
from synchub.clients.jellyfin import JellyfinClient


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


class Recorded(list):
    """A list of (method, url, kwargs) that also carries the route table."""
    routes = None


@pytest.fixture
def calls(monkeypatch):
    recorded = Recorded()

    def fake(method, url, **kwargs):
        recorded.append((method, url, kwargs))
        for suffix, response in fake.routes.items():
            if url.endswith(suffix):
                return response(kwargs) if callable(response) else response
        raise AssertionError(f"unexpected call {url}")
    fake.routes = {}
    monkeypatch.setattr(http.requests, "request", fake)
    recorded.routes = fake.routes
    return recorded


def test_clients_require_configuration(v2):
    for cls in (JellyfinClient, RadarrClient, SonarrClient):
        with pytest.raises(ConfigError):
            cls()


def test_jellyfin_paginates_until_total_and_queries_as_admin(v2, calls):
    pages = {0: [{"Id": str(i)} for i in range(2)], 2: [{"Id": "2"}]}
    calls.routes["/Users"] = Resp([{"Id": "u1", "Policy": {"IsAdministrator": False}}, {"Id": "admin", "Policy": {"IsAdministrator": True}}])
    calls.routes["/Items"] = lambda kw: Resp({"Items": pages.get(kw["params"]["StartIndex"], []), "TotalRecordCount": 3})
    client = JellyfinClient("http://jf:8096/", "KEY")
    assert [i["Id"] for i in client.iter_items(["Movie"], page_size=2)] == ["0", "1", "2"]
    item_calls = [c for c in calls if c[1].endswith("/Items")]
    assert len(item_calls) == 2 and item_calls[0][2]["params"]["UserId"] == "admin" and item_calls[0][2]["params"]["IncludeItemTypes"] == "Movie"
    assert 'Token="KEY"' in item_calls[0][2]["headers"]["Authorization"]


def test_http_errors_raise(v2, calls):
    calls.routes["/Items"] = Resp({}, 500)
    calls.routes["/Users"] = Resp([])
    with pytest.raises(ApiError):
        list(JellyfinClient("http://jf", "K").iter_items(["Movie"]))


def test_arr_clients_send_key_and_page_the_queue(v2, calls):
    calls.routes["/api/v3/queue"] = lambda kw: Resp({"records": [{"id": kw["params"]["page"]}], "totalRecords": 2})
    client = RadarrClient("http://radarr:7878/", "RKEY")
    assert [r["id"] for r in client.queue(page_size=1)] == [1, 2]
    assert calls[0][2]["headers"]["X-Api-Key"] == "RKEY"
    calls.routes["/api/v3/episode"] = Resp([{"id": 1}])
    assert SonarrClient("http://sonarr", "S").episodes(10) == [{"id": 1}]
    assert calls[-1][2]["params"] == {"seriesId": 10}


def test_payload_logging_masks_secrets_and_is_opt_in(v2, calls):
    calls.routes["/api/v3/movie"] = Resp([])
    client = RadarrClient("http://radarr", "SECRETKEY")
    client.movies()
    with v2.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM api_payload_log").fetchone()[0] == 0   # off by default
    settings.set_value("payload_logging_enabled", "1")
    settings.set_value("log_service_radarr", "1")
    client.movies()
    with v2.connection() as c:
        row = c.execute("SELECT * FROM api_payload_log").fetchone()
    assert row["service"] == "radarr" and row["status_code"] == 200
    assert "SECRETKEY" not in row["headers_json"] and "<redacted>" in row["headers_json"]


def test_payload_log_retention(v2, calls):
    calls.routes["/api/v3/movie"] = Resp([])
    settings.set_value("payload_logging_enabled", "1")
    settings.set_value("log_service_radarr", "1")
    settings.set_value("payload_retention_limit", "3")
    client = RadarrClient("http://radarr", "K")
    for _ in range(6):
        client.movies()
    with v2.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM api_payload_log").fetchone()[0] == 3


def status(v2, name):
    with v2.connection() as c:
        return dict(c.execute("SELECT * FROM task_status WHERE task_name = ?", (name,)).fetchone())


def test_task_runner_records_success_and_failure(v2):
    assert tasks.run("demo", "Demo", lambda: "all good") == "all good"
    assert status(v2, "demo")["status"] == "completed" and status(v2, "demo")["message"] == "all good"

    def boom():
        raise ConfigError("Radarr URL or API key is not configured.")
    assert tasks.run("demo", "Demo", boom) is None
    s = status(v2, "demo")
    assert s["status"] == "failed" and "not configured" in s["message"]


def test_task_runner_blocks_concurrent_runs(v2):
    import threading
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(2)
        return "done"

    worker = threading.Thread(target=lambda: tasks.run("slow", "Slow", slow))
    worker.start()
    started.wait(2)
    assert tasks.run("slow", "Slow", lambda: "second") is None      # skipped, not queued
    release.set()
    worker.join()
    assert status(v2, "slow")["message"] == "done"


def test_reset_stale_marks_interrupted_jobs_failed(v2):
    tasks.set_status("a", "in_progress", "x")
    tasks.set_status("b", "completed", "y")
    tasks.reset_stale()
    assert status(v2, "a")["status"] == "failed" and status(v2, "b")["status"] == "completed"


def test_unconfigured_services_fail_visibly(v2):
    tasks.sync_radarr()
    tasks.sync_sonarr()
    tasks.sync_jellyfin_movies()
    for name in ("radarr", "sonarr", "jellyfin_movies"):
        assert status(v2, name)["status"] == "failed" and "not configured" in status(v2, name)["message"]


def test_jellyfin_sync_remembers_the_server_id_for_app_links(v2, monkeypatch):
    from .test_ingest_jellyfin import FakeJellyfin

    class Jellyfin(FakeJellyfin):
        info = {"Id": "srv123", "ServerName": "Home"}

        def system_info(self):
            if isinstance(self.info, Exception):
                raise self.info
            return self.info

    client = Jellyfin([])
    monkeypatch.setattr(tasks, "JellyfinClient", lambda: client)
    tasks.sync_jellyfin_movies()
    assert settings.get("jellyfin_server_id") == "srv123" and status(v2, "jellyfin_movies")["status"] == "completed"
    client.info = {"Id": "other"}
    tasks.sync_jellyfin_movies()
    assert settings.get("jellyfin_server_id") == "other"                  # follows the server it is pointed at
    client.info = RuntimeError("down")
    tasks.sync_jellyfin_movies()
    assert settings.get("jellyfin_server_id") == "other" and status(v2, "jellyfin_movies")["status"] == "completed"      # not worth failing the sync over


def test_disabled_jellyfin_is_skipped_not_failed(v2):
    settings.set_value("disable_jellyfin_connection", "1")
    tasks.sync_jellyfin_tv()
    assert status(v2, "jellyfin_tv")["status"] == "completed" and "disabled" in status(v2, "jellyfin_tv")["message"]
