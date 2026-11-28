"""/healthz and /api/status: the open liveness check and the token-guarded status report for Homepage and Uptime Kuma."""
import json
import re
import sqlite3

import pytest

from synchub import public_status as status


@pytest.fixture(autouse=True)
def fresh_report(monkeypatch):
    monkeypatch.delenv(status.TOKEN_ENV, raising=False)
    status.clear_cache()
    yield
    status.clear_cache()


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(status.TOKEN_ENV, "t0ken-for-tests")
    return {"Authorization": "Bearer t0ken-for-tests"}


def _break_database(monkeypatch):
    class Broken:
        def connection(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error at /data/synchub.db")
    import synchub.web.pages as pages
    monkeypatch.setattr(pages, "db", Broken())
    monkeypatch.setattr(status, "db", Broken())


def test_healthz_is_open_and_minimal(client):
    answer = client.get("/healthz")
    assert answer.status_code == 200 and answer.get_json() == {"status": "ok"}
    assert answer.headers["Cache-Control"] == "no-store"


def test_healthz_is_503_when_the_database_is_down(client, monkeypatch):
    _break_database(monkeypatch)
    answer = client.get("/healthz")
    assert answer.status_code == 503 and answer.get_json() == {"status": "down"}


def test_status_is_off_without_a_token_setting(client):
    assert client.get("/api/status").status_code == 404
    assert client.get("/api/status", headers={"Authorization": "Bearer anything"}).status_code == 404


def test_status_needs_the_right_token(client, token):
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "t0ken-for-tests"}, {"Authorization": "Bearer "}):
        answer = client.get("/api/status", headers=headers)
        assert answer.status_code == 401 and answer.get_json() == {"error": "unauthorized"}


def test_status_answers_with_the_contract(client, token, conn):
    conn.execute("INSERT INTO worker (name, last_seen, slots) VALUES ('w', strftime('%Y-%m-%d %H:%M:%S', 'now'), 1)")
    conn.execute("INSERT INTO job (kind, status) VALUES ('scan', 'queued')")
    conn.execute("INSERT INTO job (kind, status, finished_at) VALUES ('scan', 'failed', strftime('%Y-%m-%d %H:%M:%S', 'now'))")
    conn.execute("INSERT INTO job (kind, status, finished_at) VALUES ('scan', 'failed', '2001-01-01 00:00:00')")
    conn.execute("INSERT INTO job (kind, status, finished_at) VALUES ('scan', 'done', '2026-11-27 16:40:12')")
    conn.commit()
    answer = client.get("/api/status", headers=token)
    assert answer.status_code == 200
    assert answer.headers["Content-Type"].startswith("application/json") and answer.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in answer.headers and "Set-Cookie" not in answer.headers
    body = answer.get_json()
    assert set(body) == {"app", "version", "status", "checked_at", "uptime_seconds", "checks", "metrics"}
    assert body["app"] == "synchub" and body["status"] == "ok" and isinstance(body["uptime_seconds"], int)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["checked_at"])
    assert body["checks"]["database"]["status"] == "ok" and isinstance(body["checks"]["database"]["latency_ms"], int)
    assert body["metrics"] == {"jobs_queued": 1, "jobs_running": 0, "jobs_failed_24h": 1, "workers_online": 1, "workers_total": 1,
                               "last_job_finished_at": "2026-11-27T16:40:12Z"}
    assert 3 <= len(body["metrics"]) <= 6 and not any(isinstance(v, (dict, list)) for v in body["metrics"].values())
    assert len(answer.data) < 4096


def test_empty_database_gives_nulls_not_missing_keys(client, token):
    metrics = client.get("/api/status", headers=token).get_json()["metrics"]
    assert metrics["last_job_finished_at"] is None and metrics["jobs_queued"] == 0 and len(metrics) == 6


def test_no_worker_online_is_degraded(client, token, conn):
    conn.execute("INSERT INTO worker (name, last_seen) VALUES ('w', '2020-01-01 00:00:00')")
    conn.commit()
    body = client.get("/api/status", headers=token).get_json()
    assert client.get("/api/status", headers=token).status_code == 200
    assert body["status"] == "degraded" and body["checks"]["workers"]["status"] == "degraded" and len(body["checks"]["workers"]["detail"]) <= 60


def test_database_down_is_down_with_503_and_no_error_text(client, token, monkeypatch):
    _break_database(monkeypatch)
    answer = client.get("/api/status", headers=token)
    body = answer.get_json()
    assert answer.status_code == 503 and body["status"] == "down" and body["checks"]["database"]["status"] == "down"
    assert "disk" not in answer.get_data(as_text=True) and "/data" not in answer.get_data(as_text=True)
    assert all(v is None for v in body["metrics"].values())


def test_jellyfin_down_is_degraded(client, token, monkeypatch):
    from synchub import settings
    import requests
    settings.set_value("jellyfin_url", "http://jellyfin.invalid:8096")
    def refuse(*a, **k):
        raise requests.ConnectionError("refused http://jellyfin.invalid:8096")
    monkeypatch.setattr(status.requests, "get", refuse)
    body = client.get("/api/status", headers=token).get_json()
    assert body["status"] == "degraded" and body["checks"]["jellyfin"]["detail"] == "unreachable"
    assert "invalid" not in json.dumps(body)


def test_jellyfin_up_is_ok(client, token, monkeypatch):
    from synchub import settings
    settings.set_value("jellyfin_url", "http://jellyfin.invalid:8096")
    class Reply:
        status_code = 200
    monkeypatch.setattr(status.requests, "get", lambda *a, **k: Reply())
    assert client.get("/api/status", headers=token).get_json()["checks"]["jellyfin"]["status"] == "ok"


def test_the_report_is_kept_for_fifteen_seconds(client, token, conn):
    first = client.get("/api/status", headers=token).get_json()
    conn.execute("INSERT INTO job (kind, status) VALUES ('scan', 'queued')")
    conn.commit()
    assert client.get("/api/status", headers=token).get_json() == first
    status.clear_cache()
    assert client.get("/api/status", headers=token).get_json()["metrics"]["jobs_queued"] == 1


def test_only_get_is_allowed(client, token):
    for path in ("/api/status", "/healthz"):
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)(path, headers=token).status_code == 405


def test_the_site_login_and_profiles_do_not_get_in_the_way(app, token, monkeypatch, conn):
    from synchub import access
    monkeypatch.setenv(access.TOKEN_ENV, "site-secret")
    conn.execute("INSERT INTO profile (name, pin_hash, permissions) VALUES ('Locked', 'x', '')")
    conn.commit()
    browser = app.test_client()
    assert browser.get("/api/status", headers=token).status_code == 200
    assert browser.get("/healthz").status_code == 200


def test_nothing_sensitive_in_the_response(client, token, conn, monkeypatch):
    from synchub import settings
    settings.set_value("jellyfin_url", "http://192.168.1.50:8096")
    conn.execute("INSERT INTO worker (name, hostname, last_seen) VALUES ('Secret-PC', 'bob@example.com', strftime('%Y-%m-%d %H:%M:%S', 'now'))")
    conn.execute("INSERT INTO job (kind, status, title, error, finished_at) VALUES ('scan', 'failed', 'Private Film (2020)', '/mnt/tower/Media/x.mkv', strftime('%Y-%m-%d %H:%M:%S', 'now'))")
    conn.commit()
    monkeypatch.setattr(status.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("no route to 192.168.1.50")))
    for url in ("/healthz", "/api/status"):
        text = client.get(url, headers=token).get_data(as_text=True)
        assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", text)                    # e-mail addresses
        assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)               # IP addresses
        assert not re.search(r"(/mnt/|/data|/home/|[A-Z]:\\)", text)           # file paths
        for word in ("Secret-PC", "Private Film", "t0ken-for-tests", "Traceback", "SELECT"):
            assert word not in text
