import json
import re

import pytest

from synchub import settings


@pytest.fixture
def newcomer(app):
    """A browser that has not chosen anyone yet."""
    return app.test_client()


@pytest.fixture
def two_profiles(v2):
    with v2.connection() as conn:
        alice = conn.execute("INSERT INTO profile (name, jellyfin_user_id) VALUES ('Alice', 'aaa111')").lastrowid
        bob = conn.execute("INSERT INTO profile (name, jellyfin_user_id, is_active) VALUES ('Bob', NULL, 0)").lastrowid
    return alice, bob


def cookie(client):
    got = client.get_cookie("synchub_profile")
    return got.value if got else None


def test_nobody_is_asked_to_choose_when_there_are_no_profiles(newcomer, v2):
    assert newcomer.get("/").status_code == 200
    assert newcomer.get("/who").status_code == 302 and newcomer.get("/who?next=/settings").headers["Location"] == "/settings"


def test_a_browser_that_has_not_chosen_is_sent_to_the_chooser_and_back(newcomer, two_profiles):
    alice, bob = two_profiles
    for path in ("/", "/settings", "/resolution", "/logs", "/tools", "/movie/1"):
        r = newcomer.get(path)
        assert r.status_code == 302 and r.headers["Location"].startswith("/who?next="), path
    assert newcomer.get("/settings").headers["Location"] == "/who?next=/settings"
    page = newcomer.get("/who?next=/settings").get_data(as_text=True)
    assert "Who is watching?" in page and "Alice" in page and "Bob" in page and 'value="/settings"' in page and "Just looking" in page
    assert f'value="{alice}"' in page and f'value="{bob}"' in page
    r = newcomer.post("/who", data={"profile_id": str(alice), "next": "/settings"})
    assert r.status_code == 302 and r.headers["Location"] == "/settings" and cookie(newcomer) == str(alice)
    assert newcomer.get("/settings").status_code == 200 and newcomer.get("/").status_code == 200


def test_the_cookie_is_long_lived_and_not_readable_by_scripts(newcomer, two_profiles):
    r = newcomer.post("/who", data={"profile_id": str(two_profiles[0])})
    header = r.headers["Set-Cookie"]
    assert "Max-Age=31536000" in header and "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header


def test_just_looking_is_allowed_and_remembered(newcomer, two_profiles):
    newcomer.post("/who", data={"profile_id": "0"})
    assert cookie(newcomer) == "0" and newcomer.get("/").status_code == 200
    assert "Guest" in newcomer.get("/").get_data(as_text=True)


def test_unknown_profiles_are_refused(newcomer, two_profiles):
    for bad in ("999", "abc", "", "-1"):
        r = newcomer.post("/who", data={"profile_id": bad, "next": "/settings"})
        assert r.status_code == 302 and r.headers["Location"].startswith("/who?next=") and cookie(newcomer) is None, bad


def test_a_cookie_for_a_deleted_profile_means_choosing_again(newcomer, v2, two_profiles):
    alice = two_profiles[0]
    newcomer.post("/who", data={"profile_id": str(alice)})
    assert newcomer.get("/").status_code == 200
    with v2.connection() as conn:
        conn.execute("DELETE FROM profile WHERE id = ?", (alice,))
    assert newcomer.get("/").status_code == 302


@pytest.mark.parametrize("target,expected", [
    ("/settings", "/settings"), ("/movie/3?x=1", "/movie/3?x=1"), ("", "/"), (None, "/"),
    ("//evil.example", "/"), ("http://evil.example/x", "/"), ("https://evil.example", "/"), ("/\\evil.example", "/"), ("javascript:alert(1)", "/"), ("/who", "/"), ("/who?next=/x", "/"),
])
def test_after_choosing_you_only_ever_go_to_a_page_on_this_site(newcomer, two_profiles, target, expected):
    r = newcomer.post("/who", data={"profile_id": "0", "next": target} if target is not None else {"profile_id": "0"})
    assert r.headers["Location"] == expected


def test_things_that_cannot_choose_are_not_gated(newcomer, two_profiles, monkeypatch):
    from synchub import tasks
    monkeypatch.setattr(tasks, "refresh_soon", lambda *a, **k: None)
    assert newcomer.post("/api/webhook/radarr", json={"eventType": "Test"}).status_code == 200
    assert newcomer.get("/api/tasks").status_code == 200
    assert newcomer.get("/static/js/app.js").status_code == 200
    assert newcomer.get("/poster/1").status_code in (200, 404)


def test_the_nav_names_who_you_are_and_offers_a_switch(newcomer, two_profiles):
    alice = two_profiles[0]
    newcomer.post("/who", data={"profile_id": str(alice)})
    page = newcomer.get("/settings").get_data(as_text=True)
    link = re.search(r'<a [^>]*id="whoAmI"[^>]*>(.*?)</a>', page, re.S)
    assert "Alice" in link.group(1) and "switch" in link.group(1) and 'href="/who?next=/settings"' in link.group(0)


def test_the_chosen_profile_decides_which_jellyfin_user_opens_the_app(newcomer, two_profiles):
    alice, bob = two_profiles
    settings.set_value("jellyfin_server_id", "srv123")
    config = lambda: json.loads(re.search(r'id="jellyfin-app-config">(.*?)</script>', newcomer.get("/").get_data(as_text=True), re.S).group(1))
    newcomer.post("/who", data={"profile_id": str(alice)})
    assert config()["current_user"] == "aaa111" and config()["server_id"] == "srv123"
    newcomer.post("/who", data={"profile_id": str(bob)})               # Bob has no Jellyfin user: the app falls back to its own question
    assert config()["current_user"] == ""
    newcomer.post("/who", data={"profile_id": "0"})
    assert config()["current_user"] == ""
