"""Jellyfin's Webhook plugin: every event kept, only library changes acted on, and the one-click setup that turns every notification on."""
import json

import pytest

from synchub import jellyfin_events as je
from synchub import scheduler, settings, tasks, webhooks
from synchub.clients import ApiError

URL = "http://synchub:5001/api/webhook/jellyfin"


@pytest.fixture(autouse=True)
def no_library_reads(monkeypatch):
    """An ItemAdded would start a real Jellyfin sync in the background; what it triggers is checked where it matters."""
    monkeypatch.setattr(tasks, "refresh_soon", lambda *a, **k: None)


def kept(v2, **where):
    with v2.connection() as conn:
        clause = " AND ".join(f"{k} = ?" for k in where) or "1 = 1"
        return [dict(r) for r in conn.execute(f"SELECT * FROM jellyfin_event WHERE {clause} ORDER BY id", tuple(where.values()))]


def send(client, body, content_type="text/plain"):
    return client.post("/api/webhook/jellyfin", data=body if isinstance(body, (str, bytes)) else json.dumps(body), content_type=content_type)


PLAYBACK = {"NotificationType": "PlaybackStop", "NotificationUsername": "Alice", "UserId": "u1", "ItemId": "i1", "ItemType": "Episode", "Name": "Pilot", "SeriesName": "Show",
            "SeasonNumber": 1, "EpisodeNumber": 2, "Year": 2020, "DeviceName": "Living room TV", "DeviceId": "d1", "ClientName": "Swiftfin", "PlaybackPositionTicks": 36_000_000_000,
            "RunTimeTicks": 72_000_000_000, "PlayedToCompletion": True, "IsPaused": False, "UtcTimestamp": "2026-01-01T10:00:00Z", "Overview": "x" * 50}


# ---------------------------------------------------------------- what each event means
def test_the_notification_list_is_the_plugins_whole_enum():
    assert len(webhooks.JELLYFIN_NOTIFICATION_TYPES) == 24 and len(set(webhooks.JELLYFIN_NOTIFICATION_TYPES)) == 24
    assert {"ItemAdded", "ItemDeleted", "PlaybackStart", "PlaybackProgress", "PlaybackStop", "SessionStart", "AuthenticationSuccess", "AuthenticationFailure", "TaskCompleted",
            "PluginInstalled", "UserCreated", "UserDataSaved", "Generic", "PendingRestart", "SubtitleDownloadFailure", "UserLockedOut"} <= set(webhooks.JELLYFIN_NOTIFICATION_TYPES)


@pytest.mark.parametrize("event", webhooks.JELLYFIN_NOTIFICATION_TYPES)
def test_only_changes_to_the_library_make_syncHub_read_it_again(event):
    assert webhooks.classify("jellyfin", event) == ("sync" if event in ("ItemAdded", "ItemDeleted", "TaskCompleted") else "ignore")


def test_an_unnamed_or_unknown_type_still_reads_the_library():
    assert webhooks.classify("jellyfin", "WebhookTrigger") == "sync" and webhooks.classify("jellyfin", "SomethingFromANewerPlugin") == "sync"


def test_the_receiver_keeps_every_event_but_only_reads_the_library_for_changes(client, v2, monkeypatch):
    refreshed = []
    monkeypatch.setattr(tasks, "refresh_soon", lambda service, job, **kw: refreshed.append(service))
    for event in webhooks.JELLYFIN_NOTIFICATION_TYPES:
        assert send(client, {"NotificationType": event, "ItemId": event}).status_code == 200
    assert sorted(e["event_type"] for e in kept(v2)) == sorted(webhooks.JELLYFIN_NOTIFICATION_TYPES)        # nothing was dropped
    assert len(refreshed) == 3 and set(refreshed) == {"jellyfin"}


# ---------------------------------------------------------------- what is kept
def test_a_text_plain_body_is_read_as_json_and_kept_whole(client, v2):
    body = json.dumps(PLAYBACK)
    assert send(client, body, content_type="text/plain").status_code == 200
    [row] = kept(v2)
    assert row["event_type"] == "PlaybackStop" and row["payload"] == body                                   # verbatim
    assert (row["user_name"], row["user_id"], row["item_id"], row["item_type"], row["item_name"], row["series_name"]) == ("Alice", "u1", "i1", "Episode", "Pilot", "Show")
    assert (row["season_number"], row["episode_number"], row["year"]) == (1, 2, 2020)
    assert (row["device_name"], row["device_id"], row["client_name"]) == ("Living room TV", "d1", "Swiftfin")
    assert (row["position_ticks"], row["runtime_ticks"], row["played_to_completion"], row["is_paused"]) == (36_000_000_000, 72_000_000_000, 1, 0)
    assert row["sent_at"] == "2026-01-01T10:00:00Z" and row["received_at"] and row["remote_addr"] == "127.0.0.1"
    assert webhooks.last_received("jellyfin")["event"] == "PlaybackStop"


def test_json_content_type_and_odd_key_casing_work_the_same(client, v2):
    send(client, {"notificationtype": "UserCreated", "NOTIFICATIONUSERNAME": "bob", "playedToCompletion": "True", "SeasonNumber": "3"}, content_type="application/json")
    [row] = kept(v2)
    assert (row["event_type"], row["user_name"], row["played_to_completion"], row["season_number"]) == ("UserCreated", "bob", 1, 3)


@pytest.mark.parametrize("body", ["", "not json at all", "[1, 2, 3]", '"just a string"', "null", "{broken"])
def test_a_body_that_is_not_a_json_object_is_still_answered_and_kept(client, v2, body):
    assert send(client, body).status_code == 200
    [row] = kept(v2)
    assert row["event_type"] == "WebhookTrigger" and row["payload"] == body and row["item_id"] is None


def test_a_huge_body_is_cut_instead_of_filling_the_database(client, v2):
    send(client, json.dumps({"NotificationType": "Generic", "Overview": "y" * (je.PAYLOAD_LIMIT + 5000)}))
    [row] = kept(v2)
    assert len(row["payload"]) == je.PAYLOAD_LIMIT and row["event_type"] == "Generic"


def test_numbers_and_flags_that_are_not_numbers_or_flags_are_left_empty(client, v2):
    send(client, {"NotificationType": "PlaybackStart", "SeasonNumber": "abc", "PlaybackPositionTicks": None, "IsPaused": "maybe", "PlayedToCompletion": 1})
    [row] = kept(v2)
    assert (row["season_number"], row["position_ticks"], row["is_paused"], row["played_to_completion"]) == (None, None, None, None)


def test_a_failure_to_keep_an_event_never_fails_the_webhook(client, v2, monkeypatch):
    monkeypatch.setattr(je, "store", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert send(client, {"NotificationType": "ItemAdded"}).status_code == 200


# ---------------------------------------------------------------- playback progress is sampled
def test_progress_ticks_are_stored_at_most_every_half_minute_per_player_and_item(v2):
    tick = lambda now, **kw: je.store(json.dumps({"NotificationType": "PlaybackProgress", "UserId": "u1", "DeviceId": "d1", "ItemId": "i1", **kw}), now=now)
    for now in (1000, 1005, 1010, 1029):
        tick(now)
    assert len(kept(v2)) == 1                                                                              # one for the first half minute
    tick(1031)
    tick(1032, ItemId="i2")                                                                                # another item
    tick(1033, DeviceId="d2")                                                                              # another player
    tick(1034, UserId="u2")                                                                                # another user
    assert len(kept(v2)) == 5
    je.store(json.dumps({"NotificationType": "PlaybackStop", "UserId": "u1", "DeviceId": "d1", "ItemId": "i1"}), now=1035)
    je.store(json.dumps({"NotificationType": "PlaybackStart", "UserId": "u1", "DeviceId": "d1", "ItemId": "i1"}), now=1036)
    assert len(kept(v2, event_type="PlaybackStop")) == 1 and len(kept(v2, event_type="PlaybackStart")) == 1   # only progress is sampled


def test_the_sampling_memory_does_not_grow_for_ever(v2):
    je._last_progress.clear()
    for i in range(2100):
        je.store(json.dumps({"NotificationType": "PlaybackProgress", "UserId": "u", "DeviceId": "d", "ItemId": str(i)}), now=float(i))
    assert len(je._last_progress) <= 2100 - 1 and len(je._last_progress) < 2100                              # entries older than an hour were dropped
    je._last_progress.clear()


# ---------------------------------------------------------------- keeping them for a while
def test_old_events_are_deleted_by_the_retention_setting_and_zero_keeps_everything(v2):
    for days in (1, 100, 400):
        je.store(json.dumps({"NotificationType": "Generic", "Name": f"{days}"}))
    with v2.connection() as conn:
        for days in (100, 400):
            conn.execute("UPDATE jellyfin_event SET received_at = datetime('now', ?) WHERE item_name = ?", (f"-{days} days", str(days)))
        conn.execute("UPDATE jellyfin_event SET received_at = datetime('now', '-1 days') WHERE item_name = '1'")
    assert settings.get("jellyfin_events_keep_days") == "365" and je.prune() == 1                            # the default keeps a year
    assert sorted(e["item_name"] for e in kept(v2)) == ["1", "100"]
    settings.set_value("jellyfin_events_keep_days", "30")
    assert je.prune() == 1 and [e["item_name"] for e in kept(v2)] == ["1"]
    settings.set_value("jellyfin_events_keep_days", "0")
    with v2.connection() as conn:
        conn.execute("UPDATE jellyfin_event SET received_at = '2001-01-01 00:00:00'")
    assert je.prune() == 0 and len(kept(v2)) == 1
    settings.set_value("jellyfin_events_keep_days", "garbage")
    assert je.keep_days() == 365


def test_the_retention_setting_is_validated_and_a_daily_job_applies_it(client, v2):
    from settings_validation import validate_setting
    assert validate_setting("jellyfin_events_keep_days", "0") == (True, "0") and validate_setting("jellyfin_events_keep_days", "36500")[0]
    assert not validate_setting("jellyfin_events_keep_days", "-1")[0] and not validate_setting("jellyfin_events_keep_days", "x")[0] and not validate_setting("jellyfin_events_keep_days", "36501")[0]
    scheduler.configure()
    job = scheduler.scheduler.get_job("job_prune_jellyfin_events")
    assert job is not None and job.trigger.interval.total_seconds() == 86400
    scheduler.scheduler.remove_all_jobs()


# ---------------------------------------------------------------- looking at what was kept
def test_the_events_page_counts_filters_and_escapes(client, v2):
    send(client, {**PLAYBACK, "Name": "<b>Pilot</b>"})
    send(client, {"NotificationType": "ItemAdded", "ItemType": "Movie", "Name": "Heat", "NotificationUsername": "Bob"})
    send(client, {"NotificationType": "ItemAdded", "ItemType": "Movie", "Name": "Alien"})
    page = client.get("/jellyfin-events").get_data(as_text=True)
    assert "Heat" in page and "Alien" in page and "&lt;b&gt;Pilot&lt;/b&gt;" in page and "<b>Pilot</b>" not in page
    assert 'id="evTotal">3<' in page
    only = client.get("/jellyfin-events?type=ItemAdded").get_data(as_text=True)
    assert "Heat" in only and "Alien" in only and "Pilot" not in only.split('id="eventTable"')[1]
    assert "Heat" in client.get("/jellyfin-events?user=Bob").get_data(as_text=True) and "Alien" not in client.get("/jellyfin-events?user=Bob").get_data(as_text=True).split('id="eventTable"')[1]
    assert "Alien" in client.get("/jellyfin-events?q=alie").get_data(as_text=True) and "Nothing matches" in client.get("/jellyfin-events?q=zzz").get_data(as_text=True)
    assert "S01E02" in page and "1:00:00 / 2:00:00" in page and "finished" in page                           # a stopped episode: where it got to, and that it ended


def test_an_empty_events_page_says_how_to_start(client, v2):
    page = client.get("/jellyfin-events").get_data(as_text=True)
    assert "Nothing has arrived yet" in page and "No events kept yet" in page


def test_one_event_can_be_opened_in_full_and_a_missing_one_says_so(client, v2):
    send(client, PLAYBACK)
    send(client, "plain words")
    first, second = (e["id"] for e in kept(v2))
    data = client.get(f"/jellyfin-events/{first}/payload").get_json()
    assert data["ok"] and data["payload"]["Overview"] == "x" * 50 and data["raw"] is None
    assert client.get(f"/jellyfin-events/{second}/payload").get_json()["raw"] == "plain words"
    assert client.get("/jellyfin-events/9999/payload").status_code == 404


def test_the_export_is_every_event_oldest_first_with_the_body_and_follows_the_filter(client, v2):
    send(client, {"NotificationType": "ItemAdded", "Name": "first"})
    send(client, {"NotificationType": "UserCreated", "Name": "second"})
    send(client, "not json")
    res = client.get("/jellyfin-events/export.jsonl")
    assert res.mimetype == "application/x-ndjson" and "jellyfin_events_" in res.headers["Content-Disposition"]
    lines = [json.loads(line) for line in res.get_data(as_text=True).splitlines()]
    assert [r["event_type"] for r in lines] == ["ItemAdded", "UserCreated", "WebhookTrigger"]
    assert lines[0]["payload"] == {"NotificationType": "ItemAdded", "Name": "first"} and lines[2]["payload"] == "not json"
    only = [json.loads(line) for line in client.get("/jellyfin-events/export.jsonl?type=UserCreated").get_data(as_text=True).splitlines()]
    assert [r["item_name"] for r in only] == ["second"]


def test_the_export_pages_through_a_long_history(v2):
    for i in range(23):
        je.store(json.dumps({"NotificationType": "Generic", "Name": str(i)}))
    with v2.connection() as conn:
        names = [json.loads(line)["item_name"] for line in je.export_lines(conn, chunk=5)]
    assert names == [str(i) for i in range(23)]


def test_delete_all_empties_it_after_asking(client, v2):
    send(client, {"NotificationType": "ItemAdded"})
    send(client, {"NotificationType": "ItemAdded"})
    assert "confirm(" in client.get("/jellyfin-events").get_data(as_text=True)
    res = client.post("/jellyfin-events/clear", follow_redirects=True)
    assert "Deleted 2 kept Jellyfin events" in res.get_data(as_text=True) and kept(v2) == []


# ---------------------------------------------------------------- one-click setup in Jellyfin
class FakeJellyfin:
    def __init__(self, plugins=None, configuration=None, reject=None):
        self._plugins = plugins if plugins is not None else [{"Name": "Webhook", "Id": "71552a5a-5c5c-4350-a2ae-ebe451a30173", "Status": "Active"}]
        self.configuration = configuration if configuration is not None else {"ServerUrl": "http://jf:8096", "DiscordOptions": [{"WebhookUri": "https://discord"}], "GenericOptions": []}
        self.reject, self.saved = reject, []

    def plugins(self): return self._plugins
    def plugin_configuration(self, plugin_id): return json.loads(json.dumps(self.configuration))

    def set_plugin_configuration(self, plugin_id, configuration):
        if self.reject:
            raise ApiError(self.reject)
        self.saved.append((plugin_id, configuration))
        self.configuration = configuration


def test_setup_adds_a_destination_with_every_type_every_item_and_json_headers():
    jf = FakeJellyfin()
    assert webhooks.ensure_jellyfin_webhook(jf, URL) == {"status": "created", "types": 24}
    [(plugin_id, saved)] = jf.saved
    [option] = saved["GenericOptions"]
    assert plugin_id == "71552a5a-5c5c-4350-a2ae-ebe451a30173" and option["WebhookUri"] == URL and option["WebhookName"] == "SyncHub"
    assert option["NotificationTypes"] == list(webhooks.JELLYFIN_NOTIFICATION_TYPES) and len(option["NotificationTypes"]) == 24
    assert all(option[flag] is True for flag in ("EnableMovies", "EnableEpisodes", "EnableSeries", "EnableSeasons", "EnableAlbums", "EnableSongs", "EnableVideos"))
    assert option["SendAllProperties"] is True and option["EnableWebhook"] is True and option["SkipEmptyMessageBody"] is False and option["UserFilter"] == []
    assert option["Headers"] == [{"Key": "Content-Type", "Value": "application/json"}]
    assert saved["DiscordOptions"] == [{"WebhookUri": "https://discord"}] and saved["ServerUrl"] == "http://jf:8096"     # nothing else was touched


def test_setup_is_idempotent_and_does_not_write_when_nothing_would_change():
    jf = FakeJellyfin()
    webhooks.ensure_jellyfin_webhook(jf, URL)
    jf.saved.clear()
    assert webhooks.ensure_jellyfin_webhook(jf, URL + "/") == {"status": "exists", "types": 24}                     # a trailing slash is the same address
    assert jf.saved == []


def test_setup_upgrades_an_existing_destination_and_keeps_what_the_owner_set():
    old = {"WebhookName": "My hook", "WebhookUri": URL, "NotificationTypes": ["ItemAdded"], "EnableMovies": True, "EnableEpisodes": False, "SendAllProperties": False,
           "UserFilter": ["someone"], "Fields": [{"Key": "token", "Value": "abc"}], "Template": "e30=",
           "Headers": [{"Key": "Authorization", "Value": "Bearer 1"}, {"Key": "content-type", "Value": "text/plain"}]}
    other = {"WebhookName": "Other", "WebhookUri": "http://elsewhere", "NotificationTypes": ["ItemAdded"]}
    jf = FakeJellyfin(configuration={"GenericOptions": [other, old]})
    assert webhooks.ensure_jellyfin_webhook(jf, URL) == {"status": "updated", "types": 24}
    options = jf.saved[0][1]["GenericOptions"]
    assert options[0] == other                                                                              # another destination is left alone
    mine = options[1]
    assert mine["WebhookName"] == "My hook" and mine["Template"] == "e30=" and mine["Fields"] == [{"Key": "token", "Value": "abc"}]
    assert mine["Headers"] == [{"Key": "Authorization", "Value": "Bearer 1"}, {"Key": "Content-Type", "Value": "application/json"}]       # one Content-Type, ours
    assert mine["EnableEpisodes"] is True and mine["SendAllProperties"] is True and mine["UserFilter"] == [] and len(mine["NotificationTypes"]) == 24


def test_setup_says_what_to_do_when_the_plugin_is_missing_or_not_running():
    with pytest.raises(webhooks.WebhookSetupError, match="Install it in Jellyfin"):
        webhooks.ensure_jellyfin_webhook(FakeJellyfin(plugins=[{"Name": "Trakt", "Id": "x", "Status": "Active"}]), URL)
    with pytest.raises(webhooks.WebhookSetupError, match="Restart Jellyfin"):
        webhooks.ensure_jellyfin_webhook(FakeJellyfin(plugins=[{"Name": "Webhook", "Id": "71552a5a-5c5c-4350-a2ae-ebe451a30173", "Status": "Restart"}]), URL)
    renamed = FakeJellyfin(plugins=[{"Name": "Hooks", "Id": "71552A5A5C5C4350A2AEEBE451A30173", "Status": "Active"}])
    assert webhooks.ensure_jellyfin_webhook(renamed, URL)["status"] == "created"                            # found by its ID when the name differs


def test_the_settings_button_sets_up_jellyfin_and_remembers_the_address(client, v2, monkeypatch):
    import synchub.web.api as api
    settings.set_value("jellyfin_url", "http://jf:8096")
    settings.set_value("jellyfin_api_key", "SAVEDKEY")
    made, jf = [], FakeJellyfin()
    monkeypatch.setattr(api, "JellyfinClient", lambda url, key: made.append((url, key)) or jf)
    ok = client.post("/api/webhook-setup/jellyfin", json={"fields": {"synchub_url": "http://192.168.1.50:5001/"}}).get_json()
    assert ok["ok"] and ok["status"] == "created" and "all 24 notification types" in ok["message"] and "http://192.168.1.50:5001/api/webhook/jellyfin" in ok["message"]
    assert made == [("http://jf:8096", "SAVEDKEY")] and settings.get("synchub_url") == "http://192.168.1.50:5001"
    assert client.post("/api/webhook-setup/jellyfin", json={"fields": {}}).get_json()["status"] == "exists"


def test_the_jellyfin_button_refuses_bad_input_and_explains_a_refusal(client, v2, monkeypatch):
    import synchub.web.api as api
    settings.set_value("jellyfin_url", "http://jf:8096")
    settings.set_value("jellyfin_api_key", "SAVEDKEY")
    monkeypatch.setattr(api, "JellyfinClient", lambda url, key: pytest.fail("must not connect"))
    assert "localhost" in client.post("/api/webhook-setup/jellyfin", json={"fields": {"synchub_url": "http://localhost:5001"}}).get_json()["message"]
    r = client.post("/api/webhook-setup/jellyfin", json={"fields": {"synchub_url": "http://synchub:5001", "jellyfin_url": "http://evil.example"}})
    assert r.status_code == 400 and "different URL" in r.get_json()["message"]                                # the saved key never goes to another address
    monkeypatch.setattr(api, "JellyfinClient", lambda url, key: FakeJellyfin(reject="Jellyfin saving the plugin settings failed: HTTP 400"))
    r = client.post("/api/webhook-setup/jellyfin", json={"fields": {"synchub_url": "http://synchub:5001"}})
    assert r.status_code == 400 and "would not take the webhook settings" in r.get_json()["message"] and "HTTP 400" in r.get_json()["message"]
    monkeypatch.setattr(api, "JellyfinClient", lambda url, key: FakeJellyfin(plugins=[]))
    assert "Install it in Jellyfin" in client.post("/api/webhook-setup/jellyfin", json={"fields": {"synchub_url": "http://synchub:5001"}}).get_json()["message"]


def test_the_client_talks_to_the_plugin_endpoints(v2, monkeypatch):
    from synchub.clients.jellyfin import JellyfinClient
    from synchub import http
    seen = []

    class Reply:
        status_code = 200
        def json(self): return {"ok": 1}

    monkeypatch.setattr(http, "request", lambda service, event, method, url, **kw: seen.append((method, url, kw.get("json"))) or Reply())
    jf = JellyfinClient("http://jf:8096/", "key")
    jf.plugins()
    jf.plugin_configuration("abc")
    jf.set_plugin_configuration("abc", {"GenericOptions": []})
    assert seen == [("GET", "http://jf:8096/Plugins", None), ("GET", "http://jf:8096/Plugins/abc/Configuration", None), ("POST", "http://jf:8096/Plugins/abc/Configuration", {"GenericOptions": []})]


def test_the_settings_page_offers_the_jellyfin_button_the_events_link_and_the_retention(client, v2):
    page = client.get("/settings").get_data(as_text=True)
    assert 'data-webhook-setup="jellyfin"' in page and 'data-webhook-fields="synchub_url,jellyfin_url,jellyfin_api_key"' in page
    assert 'name="jellyfin_events_keep_days"' in page and "/jellyfin-events" in page and "every notification type" in page
