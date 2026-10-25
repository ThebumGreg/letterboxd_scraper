"""CSRF protection is on in the real app: a page that was not loaded from SyncHub cannot make the browser act for you."""
import re

import pytest

from synchub import settings
from synchub.web import create_app


@pytest.fixture
def secured(v2, conn):
    """The app as it runs for real (token required), with a profile so the chooser has something to show, and a browser that chose 'just looking'."""
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    app = create_app()
    app.config["TESTING"] = True
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "0")
    return browser


def token_from(browser, path="/settings"):
    page = browser.get(path).get_data(as_text=True)
    return re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)


def test_every_page_carries_a_token_for_scripts(secured):
    for path in ("/", "/settings", "/library", "/workers", "/people", "/ddns", "/who"):
        assert token_from(secured, path)


def test_a_form_without_the_token_is_refused_and_with_it_is_accepted(secured):
    assert secured.post("/who", data={"profile_id": "0"}).status_code == 400
    page = secured.get("/who").get_data(as_text=True)
    form_token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page) or re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
    assert form_token, "the chooser's form must carry the token"
    assert secured.post("/who", data={"profile_id": "0", "csrf_token": form_token.group(1)}).status_code == 302


def test_a_script_call_needs_the_token_in_its_header(secured):
    assert secured.post("/api/library/duplicates", json={}).status_code == 400
    assert secured.post("/api/library/duplicates", json={}, headers={"X-CSRFToken": "forged"}).status_code == 400
    ok = secured.post("/api/library/duplicates", json={}, headers={"X-CSRFToken": token_from(secured)})
    assert ok.status_code != 400                                                    # past the check: whatever it answers is about duplicates, not the token


@pytest.mark.parametrize("url, body", [("/api/tasks", None), ("/healthz", None), ("/jellyfin-events", None)])
def test_reading_never_needs_a_token(secured, url, body):
    assert secured.get(url).status_code == 200


def test_webhooks_and_workers_have_their_own_ways_in_and_need_no_token(secured):
    assert secured.post("/api/webhook/radarr", json={"eventType": "Test"}).status_code == 200
    assert secured.post("/api/webhook/jellyfin", data="{}", content_type="text/plain").status_code == 200
    assert secured.post("/api/worker/v1/poll", json={}).status_code == 401           # refused for the missing worker token, not for a CSRF token


def test_the_token_does_not_survive_a_different_browser(secured, v2):
    token = token_from(secured)
    other = create_app().test_client()
    other.set_cookie("synchub_profile", "0")
    assert other.post("/api/library/duplicates", json={}, headers={"X-CSRFToken": token}).status_code == 400


def test_the_app_refuses_to_run_without_the_package_that_provides_it(v2, monkeypatch):
    import builtins
    real = builtins.__import__

    def without(name, *a, **k):
        if name.startswith("flask_wtf"):
            raise ImportError("not installed")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", without)
    with pytest.raises(RuntimeError, match="pip install -r requirements.txt"):
        create_app()


def test_the_requirements_list_the_package():
    from pathlib import Path
    assert "flask-wtf" in (Path(__file__).resolve().parents[2] / "requirements.txt").read_text().lower()
