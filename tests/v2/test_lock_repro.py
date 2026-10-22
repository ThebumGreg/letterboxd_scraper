"""Reproduces the reported 'database is locked' failures: payload logging on + real HTTP clients."""
import json
import sqlite3

import pytest

from synchub import db, http, settings, tasks


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def quick(monkeypatch, v2):
    """Fail fast (0.3 s) instead of waiting 30 s when another connection holds the write lock."""
    monkeypatch.setattr(db, "DEFAULT_TIMEOUT", 0.3)


def serve(monkeypatch, routes):
    def fake(method, url, **kw):
        for suffix, body in routes.items():
            if url.endswith(suffix):
                return Resp(body)
        raise AssertionError(url)
    monkeypatch.setattr(http.requests, "request", fake)


def configure(service):
    settings.set_value(f"{service}_url", f"http://{service}:1234")
    settings.set_value(f"{service}_api_key", "KEY")
    settings.set_value("payload_logging_enabled", "1")
    settings.set_value(f"log_service_{service}", "1")


def status(name):
    with db.connection() as c:
        return dict(c.execute("SELECT * FROM task_status WHERE task_name = ?", (name,)).fetchone())


def test_radarr_sync_with_payload_logging_on(v2, quick, monkeypatch):
    configure("radarr")
    serve(monkeypatch, {"/api/v3/movie": [{"id": 1, "tmdbId": 5, "title": "M", "year": 2000, "monitored": True, "hasFile": True}],
                        "/api/v3/queue": {"records": [], "totalRecords": 0}})
    tasks.sync_radarr()
    assert status("radarr")["status"] == "completed", status("radarr")["message"]


def test_sonarr_sync_with_payload_logging_on(v2, quick, monkeypatch):
    configure("sonarr")
    serve(monkeypatch, {"/api/v3/series": [{"id": 10, "tvdbId": 1, "title": "S"}], "/api/v3/episode": [{"id": 1, "seasonNumber": 1, "episodeNumber": 1}],
                        "/api/v3/queue": {"records": [], "totalRecords": 0}})
    tasks.sync_sonarr()
    assert status("sonarr")["status"] == "completed", status("sonarr")["message"]
