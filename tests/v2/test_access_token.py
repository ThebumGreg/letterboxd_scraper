"""The site's access token (SYNCHUB_ACCESS_TOKEN): closed without it, open to webhooks, workers and the health check."""
import pytest

from synchub import access
from synchub.web import TOKEN_COOKIE


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(access.TOKEN_ENV, "s3cret-token")
    access._login_failures.clear()
    return "s3cret-token"


def test_open_when_no_token_is_set(client, monkeypatch):
    monkeypatch.delenv(access.TOKEN_ENV, raising=False)
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 302


def test_pages_and_api_need_the_token(client, token):
    page = client.get("/search")
    assert page.status_code == 302 and "/login?next=" in page.headers["Location"]
    api = client.get("/api/tasks")
    assert api.status_code == 401 and api.get_json()["login"] == "/login"
    assert client.post("/api/tv/setup/reload").status_code == 401


def test_exempt_endpoints_stay_open(client, token):
    assert client.get("/healthz").status_code == 200
    assert client.post("/api/webhook/radarr", json={"eventType": "Test"}).status_code != 401
    assert client.post("/api/webhook/jellyfin", data="{}").status_code != 401
    assert client.post("/api/worker/v1/poll", json={}).status_code != 302       # a worker's own token decides, not this one
    assert client.get("/login").status_code == 200


def test_login_with_the_right_token_remembers_the_device(client, token):
    wrong = client.post("/login", data={"token": "nope", "next": "/search"})
    assert wrong.status_code == 401
    right = client.post("/login", data={"token": token, "next": "/search"})
    assert right.status_code == 302 and right.headers["Location"] == "/search"
    assert client.get_cookie(TOKEN_COOKIE) is not None
    assert client.get("/search").status_code == 200


def test_bearer_header_works_for_scripts(client, token):
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_changing_the_token_signs_devices_out(client, token, monkeypatch):
    client.post("/login", data={"token": token})
    assert client.get("/search").status_code == 200
    monkeypatch.setenv(access.TOKEN_ENV, "a-new-token")
    assert client.get("/search").status_code == 302


def test_too_many_wrong_tries_wait(client, token):
    for _ in range(access.LOGIN_FAILURES):
        assert client.post("/login", data={"token": "bad"}).status_code == 401
    assert client.post("/login", data={"token": token}).status_code == 429


def test_login_does_not_redirect_off_site(client, token):
    response = client.post("/login", data={"token": token, "next": "//evil.example"})
    assert response.headers["Location"] == "/"
