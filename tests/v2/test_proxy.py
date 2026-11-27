"""SyncHub behind a reverse proxy: forwarded headers are believed only from the proxy's address, and cookies are Secure over https."""
import pytest

from synchub import proxy
from synchub.web import create_app


def make(csrf=False, trust=None):
    app = create_app(csrf=csrf)
    app.config["TESTING"] = True
    if trust is not None:
        proxy.install(app, trust)
    return app


HEADERS = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "synchub.example.com"}


def seen(app, address, headers=None):
    from flask import request
    got = {}

    @app.after_request
    def probe(response):
        got.update(secure=request.is_secure, host=request.host)
        return response
    app.test_client().get("/healthz", headers=headers or {}, environ_base={"REMOTE_ADDR": address})
    return got


def test_parse_accepts_addresses_and_networks_and_rejects_nonsense():
    assert [str(n) for n in proxy.parse("10.0.20.5, 192.168.1.0/24 ::1")] == ["10.0.20.5/32", "192.168.1.0/24", "::1/128"]
    assert proxy.parse("") == [] and proxy.parse(None) == []
    with pytest.raises(ValueError):
        proxy.parse("npm.local")


def test_nothing_is_trusted_by_default(v2):
    got = seen(make(), "10.0.20.5", HEADERS)
    assert got == {"secure": False, "host": "localhost"}


def test_headers_from_the_proxy_are_believed(v2):
    got = seen(make(trust="10.0.20.5"), "10.0.20.5", HEADERS)
    assert got == {"secure": True, "host": "synchub.example.com"}


def test_headers_from_anyone_else_are_ignored(v2):
    got = seen(make(trust="10.0.20.5"), "192.168.1.77", HEADERS)
    assert got == {"secure": False, "host": "localhost"}


def test_cookies_are_secure_only_over_https(v2, conn):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    app = make(trust="10.0.20.5")
    chosen = lambda r: r.headers.get("Set-Cookie", "")
    over_https = app.test_client().post("/who", data={"profile_id": "0"}, headers=HEADERS, environ_base={"REMOTE_ADDR": "10.0.20.5"})
    plain = app.test_client().post("/who", data={"profile_id": "0"}, environ_base={"REMOTE_ADDR": "192.168.1.77"})
    assert "synchub_profile=0" in chosen(over_https) and "Secure" in chosen(over_https) and "HttpOnly" in chosen(over_https)
    assert "synchub_profile=0" in chosen(plain) and "Secure" not in chosen(plain)


def test_a_form_still_saves_through_the_proxy_with_csrf_on(v2, conn):
    import re
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    app = make(csrf=True, trust="10.0.20.5")
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "0")
    env = {"REMOTE_ADDR": "10.0.20.5"}
    page = browser.get("/who", headers=HEADERS, environ_base=env).get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    ok = browser.post("/who", data={"profile_id": "0", "csrf_token": token}, headers={**HEADERS, "Referer": "https://synchub.example.com/who"}, environ_base=env)
    assert ok.status_code == 302


def test_public_address_must_be_https_without_a_path():
    from settings_validation import validate_setting
    assert validate_setting("public_url", "https://synchub.example.com/") == (True, "https://synchub.example.com")
    assert validate_setting("public_url", "") == (True, "")
    assert not validate_setting("public_url", "http://synchub.example.com")[0]
    assert not validate_setting("public_url", "https://synchub.example.com/app")[0]
