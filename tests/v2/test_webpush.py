"""Phone or browser push: the provider (against a pretend push service), the subscribe routes, the service worker and the Home Screen app files."""
import json
from pathlib import Path

import pytest

from synchub import settings
from synchub.notifications import dispatch, providers, store
from synchub.notifications.providers import webpush
from synchub.notifications.providers.base import BAD_CHANNEL, BAD_SETUP, OK, REJECTED, RETRY, Message, ProviderError

ENDPOINT = "https://web.push.apple.com/QGxZ2o0example"
KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM", "auth": "tBHItJI5svbpez7KI4CCXg"}
CONFIG = {"endpoint": ENDPOINT, **KEYS}
BASE = "https://synchub.example.com"
ROOT = Path(__file__).resolve().parents[2]


class Gone(Exception):
    def __init__(self, code):
        self.response = type("R", (), {"status_code": code})()


@pytest.fixture
def pushed(monkeypatch, v2):
    """Stands in for pywebpush.webpush: records each call; set .fail to an exception to raise."""
    calls = []
    state = {"fail": None, "status": 201}

    def fake(subscription_info, data=None, vapid_private_key=None, vapid_claims=None, ttl=0, timeout=None, **kw):
        calls.append({"sub": subscription_info, "data": json.loads(data), "key": vapid_private_key, "claims": vapid_claims, "ttl": ttl})
        if state["fail"]:
            raise state["fail"]
        return type("R", (), {"status_code": state["status"]})()
    import pywebpush
    monkeypatch.setattr(pywebpush, "webpush", fake)
    monkeypatch.setattr(pywebpush, "WebPushException", Gone)
    settings.set_value("public_url", BASE)
    fake.calls, fake.state = calls, state
    return fake


def send(message=None):
    return providers.get("webpush").send(CONFIG, message or Message("Hi", "A film is ready", "/library"))


def test_only_push_service_addresses_are_accepted():
    provider = providers.get("webpush")
    assert provider.validate(CONFIG) == CONFIG
    for bad in ("http://web.push.apple.com/x", "https://evil.example/x", "https://web.push.apple.com.evil.example/x", "https://127.0.0.1/x", "https://web.push.apple.com:8443/x", "", None):
        with pytest.raises(ProviderError):
            provider.validate({**CONFIG, "endpoint": bad})
    with pytest.raises(ProviderError):
        provider.validate({**CONFIG, "auth": "short"})
    for good in ("https://fcm.googleapis.com/fcm/send/abc", "https://updates.push.services.mozilla.com/wpush/v2/abc", "https://wns2-par02p.notify.windows.com/w/?token=abc"):
        provider.validate({**CONFIG, "endpoint": good})


def test_a_send_is_signed_and_carries_the_title_text_and_full_address(pushed):
    assert send().ok
    call = pushed.calls[0]
    assert call["sub"] == {"endpoint": ENDPOINT, "keys": KEYS}
    assert call["data"] == {"title": "Hi", "body": "A film is ready", "url": BASE + "/library"}
    assert call["claims"] == {"sub": BASE} and call["key"]


def test_the_signing_key_is_made_once_and_the_private_half_is_a_secret(v2):
    private, public = webpush.vapid_keys()
    assert webpush.vapid_keys() == (private, public) and len(public) == 87
    assert "webpush_vapid_private" in settings.SECRET_FIELDS and private not in public


def test_it_says_what_is_missing_before_it_tries(v2):
    assert send().outcome == BAD_SETUP          # no public address yet


@pytest.mark.parametrize("code,outcome", [(404, BAD_CHANNEL), (410, BAD_CHANNEL), (403, BAD_CHANNEL), (413, REJECTED), (429, RETRY), (503, RETRY), (None, RETRY)])
def test_what_the_push_service_says_decides_what_happens_next(pushed, code, outcome):
    pushed.state["fail"] = Gone(code) if code else ConnectionError("down")
    assert send().outcome == outcome


def test_a_gone_device_is_switched_off_after_three_refusals_by_the_sender(pushed, conn):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    channel = store.add_channel(conn, 1, "webpush", "Phone", CONFIG)
    conn.commit()
    pushed.state["fail"] = Gone(410)
    for n in range(3):
        with __import__("synchub").db.connection() as c:
            nid = c.execute("INSERT INTO notification_event (event_type, dedupe_key, payload) VALUES ('test', ?, '{}')", (str(n),)).lastrowid
            c.execute("INSERT INTO notification (event_id, profile_id, channel_id, channel_label, title, body) VALUES (?, 1, ?, 'Phone', 't', 'b')", (nid, channel))
            note = c.execute("SELECT MAX(id) FROM notification").fetchone()[0]
        dispatch.deliver(note, retry=False)
    row = conn.execute("SELECT enabled, last_error FROM notification_channel WHERE id = ?", (channel,)).fetchone()
    assert row["enabled"] == 0 and "no longer subscribed" in row["last_error"]


# ---------------------------------------------------------------- the routes
@pytest.fixture
def phone(client, conn):
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    client.set_cookie("synchub_profile", "1")
    return client


HTTPS = {"X-Forwarded-Proto": "https"}


def https_client(app, conn):
    from synchub import proxy
    conn.execute("INSERT INTO profile (name) VALUES ('Alice')")
    conn.commit()
    proxy.install(app, "127.0.0.1")
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "1")
    return browser


def test_the_key_is_only_given_over_https_and_once_the_public_address_is_set(app, conn):
    browser = https_client(app, conn)
    assert browser.get("/api/notifications/push/key", headers=HTTPS, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 409      # no public address yet
    settings.set_value("public_url", BASE)
    assert browser.get("/api/notifications/push/key", environ_base={"REMOTE_ADDR": "192.168.1.9"}).status_code == 409                    # plain http
    got = browser.get("/api/notifications/push/key", headers=HTTPS, environ_base={"REMOTE_ADDR": "127.0.0.1"}).get_json()
    assert got["ok"] and got["key"] == webpush.vapid_keys()[1]


def test_a_device_subscribes_once_and_a_bad_one_is_refused(phone, conn):
    body = {"subscription": {"endpoint": ENDPOINT, "keys": KEYS}, "label": " My iPhone "}
    first = phone.post("/api/notifications/push/subscribe", json=body)
    assert first.status_code == 200 and first.get_json()["ok"]
    again = phone.post("/api/notifications/push/subscribe", json=body)
    assert again.get_json()["id"] == first.get_json()["id"]
    assert conn.execute("SELECT COUNT(*) FROM notification_channel WHERE provider = 'webpush'").fetchone()[0] == 1
    assert conn.execute("SELECT label FROM notification_channel").fetchone()[0] == "My iPhone"
    evil = phone.post("/api/notifications/push/subscribe", json={"subscription": {"endpoint": "https://evil.example/x", "keys": KEYS}})
    assert evil.status_code == 400
    assert phone.post("/api/notifications/push/subscribe", json={}).status_code == 400


def test_send_test_reaches_the_device(phone, pushed):
    channel = phone.post("/api/notifications/push/subscribe", json={"subscription": {"endpoint": ENDPOINT, "keys": KEYS}}).get_json()["id"]
    got = phone.post(f"/api/notifications/channels/{channel}/test").get_json()
    assert got["ok"] and pushed.calls and pushed.calls[0]["sub"]["endpoint"] == ENDPOINT


def test_the_page_has_the_panel_and_push_is_not_a_generic_form(phone):
    page = phone.get("/people/1").get_data(as_text=True)
    assert 'id="pushPanel"' in page and "js/push.js" in page
    assert 'data-provider="webpush"' not in page


# ---------------------------------------------------------------- the files a Home Screen app needs
def test_the_service_worker_is_served_from_the_root_without_caching(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.mimetype and r.headers["Cache-Control"] == "no-cache"
    text = r.get_data(as_text=True)
    assert "addEventListener('push'" in text and "notificationclick" in text and "fetch" not in text.replace("fetch handler", "")


def test_the_manifest_icons_exist_and_the_page_links_them(client):
    manifest = json.loads((ROOT / "static" / "manifest.json").read_text())
    assert manifest["display"] == "standalone" and manifest["background_color"] == "#212529"
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/static/icons/") and (ROOT / icon["src"].lstrip("/")).is_file() and icon["type"] == "image/png"
    page = client.get("/settings").get_data(as_text=True)
    assert 'rel="apple-touch-icon"' in page and 'name="apple-mobile-web-app-capable"' in page
    assert (ROOT / "static/icons/apple-touch-icon.png").is_file() and (ROOT / "static/icons/badge-96.png").is_file()
    assert "http" not in "".join(m["src"] for m in manifest["icons"])         # no CDN: everything is served by SyncHub
