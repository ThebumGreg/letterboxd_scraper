import json
from datetime import datetime, timedelta, timezone

import pytest

from synchub import settings, tasks, webhooks
from synchub.clients import ApiError

from .fakes import ServarrSetup, SonarrSetup


def template(implementation="Webhook", supports=("onGrab", "onDownload", "onUpgrade", "onRename", "onMovieAdded", "onMovieDelete"), flags=None):
    """A notification template as Radarr's /notification/schema returns it."""
    body = {"id": 0, "name": "", "implementation": implementation, "configContract": "WebhookSettings", "infoLink": "https://wiki",
            "fields": [{"order": 0, "name": "url", "value": None, "type": "url"}, {"order": 1, "name": "method", "value": 1, "type": "select"},
                       {"order": 2, "name": "username", "value": None, "type": "textbox"}]}
    for flag in flags or ("onGrab", "onDownload", "onUpgrade", "onRename", "onMovieAdded", "onMovieDelete", "onMovieFileDelete", "onHealthIssue"):
        body[flag] = False
        body["supports" + flag[0].upper() + flag[1:]] = flag in supports or flag == "onHealthIssue"
    return body


class FakeArr(ServarrSetup):
    def __init__(self, existing=(), schema=None, reject=None):
        self.existing, self.schema, self.reject, self.created = list(existing), schema if schema is not None else [template("Email"), template()], reject, []

    def notifications(self): return self.existing
    def notification_schema(self): return self.schema

    def add_notification(self, body):
        if self.reject:
            raise ApiError(self.reject)
        self.created.append(body)
        return {**body, "id": 7}


# ---------------------------------------------------------------- what an event means
@pytest.mark.parametrize("service,event,expected", [
    ("radarr", "Test", "ignore"), ("radarr", "Health", "ignore"), ("sonarr", "ApplicationUpdate", "ignore"), ("radarr", "HealthRestored", "ignore"),
    ("radarr", "Grab", "queue"), ("sonarr", "Grab", "queue"),
    ("radarr", "Download", "sync"), ("radarr", "MovieAdded", "sync"), ("sonarr", "SeriesDelete", "sync"), ("sonarr", "SomethingNew", "sync"), ("radarr", "WebhookTrigger", "sync"),
    ("jellyfin", "ItemAdded", "sync"), ("jellyfin", "WebhookTrigger", "sync"),
    ("jellyfin", "PlaybackStart", "ignore"), ("jellyfin", "PlaybackProgress", "ignore"), ("jellyfin", "UserDataSaved", "ignore"), ("jellyfin", "SessionStart", "ignore"),
])
def test_event_classification(service, event, expected):
    assert webhooks.classify(service, event) == expected


def test_receiver_acts_on_the_event_and_remembers_it(client, v2, monkeypatch):
    refreshed = []
    monkeypatch.setattr(tasks, "refresh_soon", lambda service, job, **kw: refreshed.append(service))
    assert webhooks.last_received("radarr") is None
    client.post("/api/webhook/radarr", json={"eventType": "Test"})
    assert refreshed == [] and webhooks.last_received("radarr")["event"] == "Test"                    # acknowledged, nothing to read
    client.post("/api/webhook/radarr", json={"eventType": "Health"})
    client.post("/api/webhook/radarr", json={"eventType": "Download", "movie": {"tmdbId": 1}})
    assert refreshed == ["radarr"] and webhooks.last_received("radarr")["event"] == "Download"
    client.post("/api/webhook/jellyfin", json={"NotificationType": "PlaybackStart"})
    assert refreshed == ["radarr"]                                                                    # plays do not trigger a library read
    client.post("/api/webhook/jellyfin", json={"NotificationType": "ItemAdded"})
    assert refreshed == ["radarr", "jellyfin"] and webhooks.last_received("jellyfin")["event"] == "ItemAdded"
    assert client.post("/api/webhook/bogus", json={}).status_code == 404 and webhooks.last_received("bogus") is None


def test_last_received_and_how_long_ago(v2):
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    at = lambda **kw: (now - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")
    assert [webhooks.ago(at(seconds=s), now) for s in (5, 59, 60, 150, 3600, 7300, 86400, 3 * 86400)] == \
        ["just now", "just now", "1 minute ago", "2 minutes ago", "1 hour ago", "2 hours ago", "1 day ago", "3 days ago"]
    assert webhooks.ago("garbage") == "" and webhooks.ago(None) == ""
    settings.set_value("webhook_last_radarr", "not json")
    assert webhooks.last_received("radarr") is None
    settings.set_value("webhook_last_radarr", json.dumps({"at": at(minutes=3), "event": "Grab"}))
    assert webhooks.last_received("radarr", now) == {"event": "Grab", "at": at(minutes=3), "ago": "3 minutes ago"}


# ---------------------------------------------------------------- creating the webhook
def test_the_address_radarr_will_use_must_be_reachable_from_elsewhere():
    assert webhooks.check_base_url(" http://192.168.1.50:5001/ ") == "http://192.168.1.50:5001"
    assert webhooks.webhook_url("http://synchub:5001/", "radarr") == "http://synchub:5001/api/webhook/radarr"
    for bad in ("", "192.168.1.50:5001", "ftp://x", "http://localhost:5001", "http://127.0.0.1:5001"):
        with pytest.raises(webhooks.WebhookSetupError):
            webhooks.check_base_url(bad)


def test_a_webhook_is_built_from_the_services_own_template():
    arr = FakeArr()
    got = webhooks.ensure_webhook(arr, "http://synchub:5001/api/webhook/radarr")
    body = arr.created[0]
    assert got == {"status": "created", "id": 7, "events": ["onGrab", "onDownload", "onUpgrade", "onRename", "onMovieAdded", "onMovieDelete"]}
    assert body["name"] == "SyncHub" and body["implementation"] == "Webhook" and "id" not in body
    assert {f["name"]: f["value"] for f in body["fields"]} == {"url": "http://synchub:5001/api/webhook/radarr", "method": 1, "username": None}
    assert body["onGrab"] and body["onDownload"] and body["onMovieAdded"]
    assert body["onMovieFileDelete"] is False and body["onHealthIssue"] is False                       # not supported / not wanted: left off
    assert all(f["value"] is None for f in arr_template_fields()), "the schema itself must not be modified"


def arr_template_fields():
    return template()["fields"][2:]


def test_creating_the_webhook_twice_changes_nothing():
    url = "http://synchub:5001/api/webhook/radarr"
    arr = FakeArr(existing=[{"id": 3, "name": "My hook", "implementation": "Webhook", "fields": [{"name": "url", "value": url}]},
                            {"id": 4, "name": "Mail", "implementation": "Email", "fields": [{"name": "url", "value": url}]}])
    assert webhooks.ensure_webhook(arr, url) == {"status": "exists", "id": 3, "name": "My hook"} and arr.created == []
    other = webhooks.ensure_webhook(FakeArr(existing=arr.existing), "http://synchub:5001/api/webhook/other")
    assert other["status"] == "created"


def test_unusable_templates_and_refusals_are_explained():
    with pytest.raises(webhooks.WebhookSetupError, match="does not offer a Webhook"):
        webhooks.ensure_webhook(FakeArr(schema=[template("Email")]), "http://x:1/y")
    no_url = template()
    no_url["fields"] = [f for f in no_url["fields"] if f["name"] != "url"]
    with pytest.raises(webhooks.WebhookSetupError, match="no URL field"):
        webhooks.ensure_webhook(FakeArr(schema=[no_url]), "http://x:1/y")
    with pytest.raises(ApiError):
        webhooks.ensure_webhook(FakeArr(reject="HTTP 400 unable to send test message"), "http://x:1/y")


def test_sonarr_gets_its_own_event_flags():
    sonarr = FakeArr()
    sonarr.service, sonarr.label = "sonarr", "Sonarr"
    sonarr.schema = [template(supports=("onGrab", "onDownload", "onSeriesAdd", "onImportComplete"), flags=("onGrab", "onDownload", "onSeriesAdd", "onImportComplete", "onSeriesDelete"))]
    assert webhooks.ensure_webhook(sonarr, "http://x:1/y")["events"] == ["onGrab", "onDownload", "onSeriesAdd", "onImportComplete"]


# ---------------------------------------------------------------- the settings page endpoint
def test_setup_endpoint_creates_and_remembers_the_address(client, v2, monkeypatch):
    import synchub.web.api as api
    settings.set_value("radarr_url", "http://radarr:7878")
    settings.set_value("radarr_api_key", "SAVEDKEY")
    made, arr = [], FakeArr()
    monkeypatch.setattr(api, "RadarrClient", lambda url, key: made.append((url, key)) or arr)
    ok = client.post("/api/webhook-setup/radarr", json={"fields": {"synchub_url": "http://192.168.1.50:5001/"}}).get_json()
    assert ok["ok"] and ok["status"] == "created" and "http://192.168.1.50:5001/api/webhook/radarr" in ok["message"]
    assert made == [("http://radarr:7878", "SAVEDKEY")] and settings.get("synchub_url") == "http://192.168.1.50:5001"
    again = client.post("/api/webhook-setup/radarr", json={"fields": {}}).get_json()                  # the remembered address is used when the field is empty
    assert again["ok"] and again["status"] == "created"                                                # (the fake does not keep what it created)


def test_setup_endpoint_refuses_unusable_input_and_never_sends_the_saved_key_elsewhere(client, v2, monkeypatch):
    import synchub.web.api as api
    settings.set_value("radarr_url", "http://radarr:7878")
    settings.set_value("radarr_api_key", "SAVEDKEY")
    monkeypatch.setattr(api, "RadarrClient", lambda url, key: pytest.fail("must not connect"))
    assert client.post("/api/webhook-setup/bogus", json={}).status_code == 404
    r = client.post("/api/webhook-setup/radarr", json={"fields": {}})
    assert r.status_code == 400 and "address" in r.get_json()["message"]
    r = client.post("/api/webhook-setup/radarr", json={"fields": {"synchub_url": "http://localhost:5001"}})
    assert r.status_code == 400 and "localhost" in r.get_json()["message"]
    r = client.post("/api/webhook-setup/radarr", json={"fields": {"synchub_url": "http://synchub:5001", "radarr_url": "http://evil.example"}})
    assert r.status_code == 400 and "different URL" in r.get_json()["message"]


def test_setup_endpoint_explains_a_refusal_by_the_service(client, v2, monkeypatch):
    import synchub.web.api as api
    settings.set_value("sonarr_url", "http://sonarr:8989")
    settings.set_value("sonarr_api_key", "K")
    arr = FakeArr(reject="Sonarr AddNotification failed: HTTP 400 Unable to send test message")
    arr.service, arr.label = "sonarr", "Sonarr"
    monkeypatch.setattr(api, "SonarrClient", lambda url, key: arr)
    r = client.post("/api/webhook-setup/sonarr", json={"fields": {"synchub_url": "http://synchub:5001"}})
    message = r.get_json()["message"]
    assert r.status_code == 400 and "cannot reach http://synchub:5001/api/webhook/sonarr" in message and "Unable to send test message" in message
    assert settings.get("synchub_url") == ""                                                           # not remembered when it did not work


def test_settings_page_shows_webhook_addresses_and_status(client, v2):
    page = client.get("/settings").get_data(as_text=True)
    assert "/api/webhook/radarr" in page and "/api/webhook/sonarr" in page and "Nothing received yet" in page
    settings.set_value("synchub_url", "http://192.168.1.50:5001")
    client.post("/api/webhook/radarr", json={"eventType": "Grab"})
    page = client.get("/settings").get_data(as_text=True)
    assert "http://192.168.1.50:5001/api/webhook/radarr" in page and "Last event: Grab" in page
