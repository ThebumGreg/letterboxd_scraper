"""Notifications, phase 1: the Pushover provider (against a pretend Pushover), a person's delivery methods, the outbox and its sender, and the page."""
import json

import pytest
import requests

from synchub import access, db, settings
from synchub.notifications import dispatch, emit, events, providers, store
from synchub.notifications.providers import pushover
from synchub.notifications.providers.base import BAD_CHANNEL, BAD_SETUP, OK, QUOTA, REJECTED, RETRY, Message, ProviderError, mask
from synchub.notifications.throttle import Throttle

TOKEN = "azGDORePK8gMaC0QOYAMyEEuzJnyUi"          # the examples in Pushover's documentation: right shape, not real
USER = "uQiRzpo4DXghDmr9QzzfQu27cmVRsG"
OTHER_USER = "gznej3rKEVAvPUxu9vvNnqpmZpokzF"
T0 = "2026-11-02 10:00:00"


# ---------------------------------------------------------------- a pretend Pushover
class Reply:
    def __init__(self, status=200, body="default", headers=None):
        self.status_code = status
        self._body = {"status": 1, "request": "req-1"} if body == "default" else body
        self.headers = headers if headers is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakePushover:
    """Stands in for synchub.http.request. Replies are used in order, then the default (accepted, with the allowance headers)."""

    def __init__(self):
        self.calls, self.replies = [], []

    def __call__(self, service, event, method, url, **kwargs):
        db.assert_not_in_write_transaction(f"HTTP {method} to {service}")         # what the real one does: a send must not hold the database
        self.calls.append({"service": service, "event": event, "method": method, "url": url, **kwargs})
        reply = self.replies.pop(0) if self.replies else Reply(headers={"X-Limit-App-Limit": "10000", "X-Limit-App-Remaining": "9990", "X-Limit-App-Reset": "1793000000"})
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def sent(self):
        return [c for c in self.calls if c["event"] == "send"]


@pytest.fixture
def push(monkeypatch, v2):
    fake = FakePushover()
    monkeypatch.setattr("synchub.http.request", fake)
    settings.set_value("pushover_app_token", TOKEN)
    return fake


@pytest.fixture(autouse=True)
def fresh_throttles(monkeypatch):
    from synchub.web import notifications as web
    monkeypatch.setattr(web, "CHECKS", Throttle(6, 60))
    monkeypatch.setattr(web, "TESTS", Throttle(5, 60))


def add_profile(conn, name="Alice"):
    pid = conn.execute("INSERT INTO profile (name) VALUES (?)", (name,)).lastrowid
    conn.commit()
    return pid


def add_channel(conn, pid, key=USER, device="", label="My phone"):
    cid = store.add_channel(conn, pid, "pushover", label, {"user_key": key, "device": device}, now=T0)
    conn.commit()
    return cid


def queue(conn, channel_id, key="k1", payload=None, now=T0):
    channel = store.get_channel(conn, channel_id)
    ids = emit.enqueue(conn, "test", key, payload or {}, [channel], now=now)
    conn.commit()
    return ids[0]


def row(conn, notification_id):
    return dict(conn.execute("SELECT * FROM notification WHERE id = ?", (notification_id,)).fetchone())


def channel_row(conn, cid):
    return dict(conn.execute("SELECT * FROM notification_channel WHERE id = ?", (cid,)).fetchone())


def switch_on():
    settings.set_value("enable_notifications", "1")


# ================================================================ the Pushover provider
def test_a_user_key_must_have_the_shape_pushover_uses_before_anything_is_asked(push):
    p = providers.get("pushover")
    assert p.validate({"user_key": f"  {USER} ", "device": " iphone , nexus-5 "}) == {"user_key": USER, "device": "iphone,nexus-5"}
    for bad in ("", "short", USER + "x", USER[:-1] + "!", "u" * 29 + " "):
        with pytest.raises(ProviderError, match="30 letters and digits"):
            p.validate({"user_key": bad})
    with pytest.raises(ProviderError, match="device name"):
        p.validate({"user_key": USER, "device": "a" * 26})
    with pytest.raises(ProviderError, match="device name"):
        p.validate({"user_key": USER, "device": "my phone"})
    assert push.calls == []


def test_a_message_is_posted_as_a_form_with_the_documented_fields(push):
    msg = Message("Heat is ready", "Watch it now", url="http://jf/heat", url_title="Open", priority=1, ttl_seconds=600, timestamp=1793000000)
    result = providers.get("pushover").send({"user_key": USER, "device": "iphone"}, msg)
    assert result.ok and result.ref == "req-1"
    (call,) = push.sent
    assert call["method"] == "POST" and call["url"] == "https://api.pushover.net/1/messages.json" and call["service"] == "pushover"
    assert call["data"] == {"token": TOKEN, "user": USER, "message": "Watch it now", "title": "Heat is ready", "device": "iphone", "url": "http://jf/heat",
                            "url_title": "Open", "priority": 1, "ttl": 600, "timestamp": 1793000000}


def test_optional_fields_are_left_out_and_emergency_messages_carry_retry_and_expire(push):
    p = providers.get("pushover")
    p.send({"user_key": USER}, Message("", "plain"))
    assert push.sent[0]["data"] == {"token": TOKEN, "user": USER, "message": "plain"}
    p.send({"user_key": USER}, Message("Down", "x", priority=2, ttl_seconds=600))
    data = push.sent[1]["data"]
    assert data["priority"] == 2 and data["retry"] >= 30 and data["expire"] <= 10800 and "ttl" not in data


def test_text_is_cut_to_what_pushover_accepts(push):
    providers.get("pushover").send({"user_key": USER}, Message("t" * 400, "m" * 3000, url="http://x/" + "u" * 600, url_title="v" * 200))
    data = push.sent[0]["data"]
    assert (len(data["title"]), len(data["message"]), len(data["url"]), len(data["url_title"])) == (250, 1024, 512, 100)
    assert data["message"].endswith("…")


def test_the_allowance_headers_are_remembered(push):
    providers.get("pushover").send({"user_key": USER}, Message("a", "b"))
    assert pushover.quota() == {"limit": 10000, "remaining": 9990, "reset": 1793000000, "at": pushover.quota()["at"]}
    assert dispatch.state()["quota"]["reset_label"] == "Oct 26"                                              # 1793000000 is 26 October 2026 (UTC)


def test_what_each_answer_from_pushover_means(push):
    p = providers.get("pushover")
    send = lambda: p.send({"user_key": USER}, Message("a", "b"))
    push.replies = [Reply(429, {"status": 0, "errors": ["over the limit"]}, {"X-Limit-App-Reset": "4102444800"})]
    got = send()
    assert got.outcome == QUOTA and 300 <= got.retry_after <= 7 * 86400 and "allowance" in got.detail
    push.replies = [Reply(500, None)]
    assert send().outcome == RETRY
    push.replies = [Reply(400, {"user": "invalid", "errors": ["user identifier is invalid"], "status": 0})]
    got = send()
    assert got.outcome == BAD_CHANNEL and "user identifier is invalid" in got.detail
    push.replies = [Reply(400, {"token": "invalid", "errors": ["application token is invalid"], "status": 0})]
    assert send().outcome == BAD_SETUP
    push.replies = [Reply(400, {"message": "blank", "errors": ["message cannot be blank"], "status": 0})]
    assert send().outcome == REJECTED
    push.replies = [Reply(200, {"status": 0, "errors": ["odd"]})]
    assert send().outcome == REJECTED
    push.replies = [Reply(403, None)]
    assert send().outcome == REJECTED                                           # a 4xx is final even without a body


def test_nothing_secret_is_ever_in_what_a_person_is_told(push):
    p = providers.get("pushover")
    push.replies = [Reply(400, {"user": "invalid", "errors": [f"{USER} is not valid, nor is {TOKEN}"], "status": 0})]
    got = p.send({"user_key": USER}, Message("a", "b"))
    assert USER not in got.detail and TOKEN not in got.detail and "***" in got.detail
    # `requests` quotes the address it was calling, and the allowance call carries the token in it
    push.replies = [requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: /1/apps/limits.json?token={TOKEN}")]
    got = pushover.check_token(TOKEN)
    assert got.outcome == RETRY and TOKEN not in got.detail and "ConnectionError" in got.detail


def test_checking_a_key_lists_the_devices_and_asks_pushover_the_documented_question(push):
    push.replies = [Reply(200, {"status": 1, "devices": ["iphone", "ipad"], "licenses": ["iOS"], "request": "r"})]
    got = providers.get("pushover").check({"user_key": USER, "device": ""})
    assert got.ok and got.data == {"devices": ["iphone", "ipad"], "licenses": ["iOS"]}
    assert providers.get("pushover").choices(got) == {"device": ["iphone", "ipad"]}
    (call,) = push.calls
    assert call["url"].endswith("/users/validate.json") and call["data"] == {"token": TOKEN, "user": USER}


def test_a_key_pushover_refuses_is_a_bad_channel_and_a_missing_or_malformed_token_never_reaches_pushover(push):
    p = providers.get("pushover")
    push.replies = [Reply(400, {"errors": ["user has no active devices"], "status": 0})]
    got = p.check({"user_key": USER})
    assert got.outcome == BAD_CHANNEL and "no active devices" in got.detail
    for token in ("", "short"):
        settings.set_value("pushover_app_token", token)
        assert p.check({"user_key": USER}).outcome == BAD_SETUP and p.send({"user_key": USER}, Message("a", "b")).outcome == BAD_SETUP
    assert len(push.calls) == 1


def test_the_application_token_is_checked_through_the_allowance_call_with_the_token_as_a_masked_parameter(push):
    push.replies = [Reply(200, {"status": 1, "limit": 10000, "remaining": 8123, "reset": 1793000000})]
    got = pushover.check_token(TOKEN)
    assert got.ok and got.data == {"limit": 10000, "remaining": 8123, "reset": 1793000000}
    (call,) = push.calls
    assert call["method"] == "GET" and call["url"].endswith("/apps/limits.json") and call["params"] == {"token": TOKEN}
    assert pushover.quota()["remaining"] == 8123
    push.replies = [Reply(200, {"status": 1, "limit": 10000, "remaining": 5, "reset": 1793000000})]
    pushover.check_token(TOKEN, remember_quota=False)                           # a token only being tried says nothing about the one in use
    assert pushover.quota()["remaining"] == 8123
    assert pushover.check_token("nope").outcome == BAD_SETUP and len(push.calls) == 2


def test_the_payload_log_masks_the_token_and_the_recipient_in_form_data():
    from synchub import http
    assert http._mask_params({"token": "t", "user": "u", "message": "hi"}) == {"token": "<redacted>", "user": "<redacted>", "message": "hi"}


def test_apprise_is_only_for_the_administrators_own_target(v2):
    assert [p.key for p in providers.for_people()] == ["pushover"]
    assert providers.get("apprise").scope == "admin"
    with pytest.raises(ProviderError, match="not available"):
        providers.get("carrier-pigeon")


def test_apprise_reports_what_it_could_not_do_without_quoting_the_url(monkeypatch):
    import apprise
    a = providers.get("apprise")
    assert a.send({"urls": ""}, Message("a", "b")).outcome == BAD_CHANNEL
    assert a.send({"urls": "nonsense://x"}, Message("a", "b")).outcome == BAD_CHANNEL
    sent = []
    monkeypatch.setattr(apprise.Apprise, "notify", lambda self, title, body: sent.append((title, body)) or True)
    assert a.send({"urls": "json://localhost"}, Message("a", "b")).ok and sent == [("a", "b")]
    monkeypatch.setattr(apprise.Apprise, "notify", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("secret-token-in-url")))
    got = a.send({"urls": "json://localhost"}, Message("a", "b"))
    assert got.outcome == RETRY and "secret-token-in-url" not in got.detail


def test_secrets_are_masked_by_the_provider_for_display():
    assert mask(USER) == "•" * 8 + USER[-4:] and mask("abc") == "•••" and mask("") == ""
    assert providers.get("pushover").summary({"user_key": USER, "device": "ipad"}) == {"user_key": "•" * 8 + USER[-4:], "device": "ipad"}


# ================================================================ a person's delivery methods
def test_adding_listing_and_removing_a_channel(v2, conn):
    pid = add_profile(conn)
    cid = add_channel(conn, pid, device="ipad", label="")
    (shown,) = store.list_channels(conn, pid)
    assert shown["label"] == "Pushover (ipad)" and shown["enabled"] and shown["verified_at"] == T0
    assert USER not in json.dumps(shown) and shown["summary"]["user_key"].endswith(USER[-4:])
    assert store.get_channel(conn, cid, pid)["config"]["user_key"] == USER and store.get_channel(conn, cid, pid + 1) is None
    assert not store.delete_channel(conn, cid, pid + 1) and store.delete_channel(conn, cid, pid)
    assert store.list_channels(conn, pid) == []


def test_a_channel_cannot_be_added_twice_beyond_the_limit_or_for_a_provider_people_may_not_use(v2, conn):
    pid = add_profile(conn)
    add_channel(conn, pid)
    with pytest.raises(ProviderError, match="already added"):
        add_channel(conn, pid)
    with pytest.raises(ProviderError, match="not available"):
        store.add_channel(conn, pid, "apprise", "x", {"urls": "json://localhost"})
    with pytest.raises(ProviderError, match="no longer exists"):
        store.add_channel(conn, 999, "pushover", "x", {"user_key": OTHER_USER})
    for i in range(store.MAX_CHANNELS - 1):
        add_channel(conn, pid, key=f"{i:030d}")
    with pytest.raises(ProviderError, match="most"):
        add_channel(conn, pid, key="9" * 30)


def test_switching_a_channel_back_on_forgets_its_failures(v2, conn):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    conn.execute("UPDATE notification_channel SET enabled = 0, failures = 3, last_error = 'bad' WHERE id = ?", (cid,))
    conn.commit()
    assert not store.set_enabled(conn, cid, pid + 1, True)
    assert store.set_enabled(conn, cid, pid, True)
    got = channel_row(conn, cid)
    assert (got["enabled"], got["failures"], got["last_error"]) == (1, 0, None)


def test_deleting_a_profile_takes_its_keys_and_its_history_with_it(v2, conn):
    pid = add_profile(conn)
    nid = queue(conn, add_channel(conn, pid))
    conn.execute("DELETE FROM profile WHERE id = ?", (pid,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM notification_channel").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM notification WHERE id = ?", (nid,)).fetchone()[0] == 0


def test_every_stored_key_is_known_to_the_log_viewer_so_it_can_be_blanked(v2, conn):
    from synchub.web import admin
    pid = add_profile(conn)
    add_channel(conn, pid)
    settings.set_value("pushover_app_token", TOKEN)
    assert USER in admin._known_secrets() and TOKEN in admin._known_secrets()
    assert admin._redact(f"sent to {USER} with {TOKEN}", admin._known_secrets()) == "sent to *** with ***"


# ================================================================ events and the outbox
def test_an_event_is_recorded_once_and_each_channel_gets_one_message(v2, conn):
    a, b = add_profile(conn, "Alice"), add_profile(conn, "Bob")
    ca, cb = add_channel(conn, a), add_channel(conn, b, key=OTHER_USER)
    channels = [store.get_channel(conn, ca), store.get_channel(conn, cb)]
    ids = emit.enqueue(conn, "test", "same", {"text": "hello"}, channels, now=T0)
    conn.commit()
    assert len(ids) == 2
    first = row(conn, ids[0])
    assert (first["title"], first["body"], first["status"], first["profile_id"], first["expires_at"]) == ("SyncHub test", "hello", "pending", a, "2026-11-02 11:00:00")
    assert emit.enqueue(conn, "test", "same", {"text": "hello"}, channels, now=T0) is None                    # the duplicate guard
    assert emit.enqueue(conn, "test", "other", {}, channels, now=T0) and conn.execute("SELECT COUNT(*) FROM notification_event").fetchone()[0] == 2


def test_a_switched_off_channel_is_not_queued_and_an_unknown_event_is_refused(v2, conn):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    store.set_enabled(conn, cid, pid, False)
    assert emit.enqueue(conn, "test", "k", {}, [store.get_channel(conn, cid)], now=T0) == []
    with pytest.raises(KeyError, match="no notification event"):
        emit.enqueue(conn, "nonsense", "k", {}, [], now=T0)
    assert events.get("test").audience == events.USER


def test_history_names_the_event_and_survives_the_channel_being_removed(v2, conn, push):
    pid = add_profile(conn)
    cid = add_channel(conn, pid, label="My phone")
    nid = queue(conn, cid)
    dispatch.deliver(nid, now=T0)
    store.delete_channel(conn, cid, pid)
    conn.commit()
    (h,) = store.history(conn, pid)
    assert (h["event_label"], h["status"], h["channel_label"], h["title"]) == ("Test notification", "sent", "My phone", "SyncHub test")


def test_old_history_is_pruned_with_its_messages_and_zero_keeps_everything(v2, conn):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    old, new = queue(conn, cid, "old", now="2026-01-01 00:00:00"), queue(conn, cid, "new", now=T0)
    assert store.prune(conn, 0, now=T0) == 0
    assert store.prune(conn, 90, now=T0) == 1
    conn.commit()
    assert conn.execute("SELECT id FROM notification").fetchall()[0][0] == new and conn.execute("SELECT COUNT(*) FROM notification WHERE id = ?", (old,)).fetchone()[0] == 0


# ================================================================ sending
def test_a_delivered_message_is_marked_sent_and_the_channel_remembers(v2, conn, push):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    nid = queue(conn, cid)
    got = dispatch.deliver(nid, now=T0)
    assert got == {"ok": True, "status": "sent", "message": "Sent."}
    n, c = row(conn, nid), channel_row(conn, cid)
    assert (n["status"], n["sent_at"], n["provider_ref"], n["attempts"], n["error"], n["next_attempt_at"]) == ("sent", T0, "req-1", 1, None, None)
    assert (c["last_ok_at"], c["failures"], c["last_error"]) == (T0, 0, None)
    assert push.sent[0]["data"]["timestamp"] == 1793613600                                                  # when it happened (T0) travels with it
    assert dispatch.deliver(nid, now=T0)["message"] == "Already sent." and len(push.sent) == 1                    # never twice


def test_a_message_someone_else_is_sending_is_left_alone(v2, conn, push):
    cid = add_channel(conn, add_profile(conn))
    nid = queue(conn, cid)
    conn.execute("UPDATE notification SET next_attempt_at = '2026-11-02 10:01:00' WHERE id = ?", (nid,))
    conn.commit()
    assert dispatch.deliver(nid, now=T0)["message"] == "Not due yet." and push.calls == []
    assert dispatch.deliver(nid, now="2026-11-02 10:01:00")["ok"] and len(push.sent) == 1                       # a lease that ran out is taken over


def test_a_message_too_old_to_matter_or_for_a_gone_channel_is_dropped_not_sent(v2, conn, push):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    stale = queue(conn, cid, "stale")
    assert dispatch.deliver(stale, now="2026-11-02 11:00:01") == {"ok": False, "status": "dropped", "message": "Too old to be worth sending."}
    gone = queue(conn, cid, "gone")
    store.delete_channel(conn, cid, pid)
    conn.commit()
    assert dispatch.deliver(gone, now=T0)["message"] == "That delivery method was removed."
    assert row(conn, gone)["status"] == "dropped" and push.calls == []
    assert dispatch.deliver(99999)["status"] == "gone"


def test_a_switched_off_channel_drops_what_was_waiting(v2, conn, push):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    nid = queue(conn, cid)
    store.set_enabled(conn, cid, pid, False)
    conn.commit()
    assert dispatch.deliver(nid, now=T0)["status"] == "dropped" and push.calls == []


def test_a_temporary_failure_is_tried_again_later_with_a_growing_wait_and_then_given_up(v2, conn, push):
    cid = add_channel(conn, add_profile(conn))
    nid = queue(conn, cid)
    conn.execute("UPDATE notification SET expires_at = '2999-01-01 00:00:00'")             # (a test message would expire within the hour, before the last try)
    conn.commit()
    now, waits = T0, []
    for attempt in range(1, dispatch.MAX_ATTEMPTS + 1):
        push.replies = [Reply(503, None)]
        got = dispatch.deliver(nid, now=now)
        n = row(conn, nid)
        assert n["attempts"] == attempt
        if attempt < dispatch.MAX_ATTEMPTS:
            assert got["status"] == "pending" and "tried again" in got["message"]
            waits.append((__import__("datetime").datetime.strptime(n["next_attempt_at"], store.FORMAT) - __import__("datetime").datetime.strptime(now, store.FORMAT)).seconds)
            now = n["next_attempt_at"]
    assert waits == [30, 120, 600, 3600] and got["status"] == "failed" and row(conn, nid)["status"] == "failed" and "HTTP 503" in row(conn, nid)["error"]


def test_a_message_still_waiting_to_be_retried_when_it_goes_stale_is_dropped(v2, conn, push):
    cid = add_channel(conn, add_profile(conn))
    nid = queue(conn, cid)
    push.replies = [Reply(503, None)]
    dispatch.deliver(nid, now=T0)
    assert row(conn, nid)["status"] == "pending"
    got = dispatch.deliver(nid, now="2026-11-02 11:00:00")                                                    # a test message lives an hour
    assert got["status"] == "dropped" and row(conn, nid)["attempts"] == 1 and len(push.sent) == 1


def test_a_test_a_person_is_waiting_on_fails_at_once_instead_of_queueing(v2, conn, push):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    push.replies = [Reply(503, None)]
    got = dispatch.send_test(pid, cid, now=T0)
    assert got["status"] == "failed" and "HTTP 503" in got["message"]
    assert conn.execute("SELECT status FROM notification").fetchone()[0] == "failed"
    with pytest.raises(LookupError):
        dispatch.send_test(pid + 1, cid)
    store.set_enabled(conn, cid, pid, False)
    conn.commit()
    assert dispatch.send_test(pid, cid)["message"] == "Switch this delivery method on first."


def test_a_test_goes_out_even_while_the_administrators_switch_is_off(v2, conn, push):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    assert settings.get("enable_notifications") == "0"
    assert dispatch.send_test(pid, cid)["ok"] and push.sent[0]["data"]["title"] == "SyncHub test" and push.sent[0]["data"]["ttl"] == 3600


def test_a_recipient_pushover_says_is_invalid_is_switched_off_after_three_answers(v2, conn, push):
    pid = add_profile(conn)
    cid = add_channel(conn, pid)
    ids = [queue(conn, cid, f"k{i}") for i in range(5)]
    for nid in ids[:3]:
        push.replies = [Reply(400, {"user": "invalid", "errors": ["user identifier is invalid"], "status": 0})]
        assert dispatch.deliver(nid, now=T0)["status"] == "failed"
    c = channel_row(conn, cid)
    assert (c["enabled"], c["failures"]) == (0, 3) and c["last_error"].startswith("Switched off after repeated failures")
    before = len(push.calls)
    assert dispatch.deliver(ids[3], now=T0)["status"] == "dropped" and len(push.calls) == before             # nothing more is sent to a dead key


def test_one_good_answer_resets_the_count_of_bad_ones(v2, conn, push):
    cid = add_channel(conn, add_profile(conn))
    a, b, c = queue(conn, cid, "a"), queue(conn, cid, "b"), queue(conn, cid, "c")
    push.replies = [Reply(400, {"user": "invalid", "errors": ["bad"], "status": 0})]
    dispatch.deliver(a, now=T0)
    assert channel_row(conn, cid)["failures"] == 1
    assert dispatch.deliver(b, now=T0)["ok"] and channel_row(conn, cid)["failures"] == 0
    assert channel_row(conn, cid)["enabled"] == 1


def test_a_used_up_allowance_puts_pushover_on_hold_and_nothing_hammers_it(v2, conn, push):
    switch_on()
    cid = add_channel(conn, add_profile(conn))
    first, second = queue(conn, cid, "a"), queue(conn, cid, "b")
    push.replies = [Reply(429, {"status": 0, "errors": ["limit"]}, {"X-Limit-App-Reset": "1793613600"})]
    got = dispatch.run_due(now=T0)
    assert got["pending"] == 2 and len(push.sent) == 1                                                       # one try; the other never reaches Pushover
    n = row(conn, first)
    assert n["status"] == "pending" and n["attempts"] == 0 and n["next_attempt_at"] > T0                     # it is not the message's fault: attempts stay
    with db.connection() as c:
        assert "allowance" in dispatch.paused(c, "pushover", T0)
    dispatch.run_due(now=T0)
    assert len(push.sent) == 1 and row(conn, second)["status"] == "pending"
    assert "On hold" in dispatch.deliver(second, now=T0)["message"]
    assert dispatch.state(now=T0)["paused"]


def test_a_rejected_application_token_holds_pushover_for_a_while_then_clears_when_fixed(v2, conn, push):
    switch_on()
    cid = add_channel(conn, add_profile(conn))
    nid = queue(conn, cid)
    push.replies = [Reply(400, {"token": "invalid", "errors": ["application token is invalid"], "status": 0})]
    dispatch.run_due(now=T0)
    n = row(conn, nid)
    assert n["status"] == "pending" and n["next_attempt_at"] == "2026-11-02 10:15:00"
    dispatch.run_due(now="2026-11-02 10:05:00")
    assert len(push.sent) == 1
    dispatch.clear_pause("pushover")
    dispatch.run_due(now="2026-11-02 10:05:00")                                                              # still leased until 10:15 by its own wait
    assert len(push.sent) == 1
    assert dispatch.run_due(now="2026-11-02 10:15:00")["sent"] == 1


def test_a_hold_ends_by_itself(v2, conn):
    with db.connection() as c:
        dispatch.pause(c, "pushover", "2026-11-02 10:30:00", "because")
    with db.connection() as c:
        assert dispatch.paused(c, "pushover", T0) == "because" and dispatch.paused(c, "pushover", "2026-11-02 10:30:00") is None
        assert dispatch.paused(c, "apprise", T0) is None


def test_the_scheduled_sender_does_nothing_while_the_switch_is_off(v2, conn, push):
    cid = add_channel(conn, add_profile(conn))
    nid = queue(conn, cid)
    assert dispatch.run_due(now=T0) == {"sent": 0, "failed": 0, "pending": 0, "dropped": 0} and push.calls == []
    switch_on()
    assert dispatch.run_due(now=T0)["sent"] == 1 and row(conn, nid)["status"] == "sent"


def test_the_sender_takes_what_is_due_oldest_first_and_skips_what_is_waiting(v2, conn, push):
    switch_on()
    cid = add_channel(conn, add_profile(conn))
    a, b, later = queue(conn, cid, "a"), queue(conn, cid, "b"), queue(conn, cid, "c")
    conn.execute("UPDATE notification SET next_attempt_at = '2026-11-02 10:30:00' WHERE id = ?", (later,))
    conn.commit()
    assert dispatch.run_due(now=T0)["sent"] == 2 and len(push.sent) == 2 and row(conn, later)["status"] == "pending"
    assert row(conn, a)["status"] == row(conn, b)["status"] == "sent"
    assert dispatch.run_due(now="2026-11-02 10:30:00")["sent"] == 1


def test_one_failing_message_does_not_stop_the_others(v2, conn, push, monkeypatch):
    switch_on()
    cid = add_channel(conn, add_profile(conn))
    a, b = queue(conn, cid, "a"), queue(conn, cid, "b")
    real = dispatch.deliver
    monkeypatch.setattr(dispatch, "deliver", lambda nid, now=None, retry=True: (_ for _ in ()).throw(RuntimeError("boom")) if nid == a else real(nid, now=now, retry=retry))
    assert dispatch.run_due(now=T0)["sent"] == 1 and row(conn, b)["status"] == "sent"


def test_a_provider_that_blows_up_counts_as_a_temporary_failure_not_a_crash(v2, conn, monkeypatch):
    cid = add_channel(conn, add_profile(conn))
    nid = queue(conn, cid)
    monkeypatch.setattr(providers.get("pushover"), "send", lambda config, message: (_ for _ in ()).throw(RuntimeError("secret")))
    got = dispatch.deliver(nid, now=T0)
    assert got["status"] == "pending" and "secret" not in got["message"] and "secret" not in row(conn, nid)["error"]


def test_the_send_happens_with_no_write_transaction_open(v2, conn, push):
    cid = add_channel(conn, add_profile(conn))
    nid = queue(conn, cid)
    dispatch.deliver(nid, now=T0)                       # FakePushover runs the real guard; the strict mode of the test suite makes it raise
    assert row(conn, nid)["status"] == "sent"


def test_the_scheduler_runs_the_sender_and_the_history_cleanup(v2):
    from synchub import scheduler
    scheduler.configure()
    try:
        assert scheduler.scheduler.get_job("job_notify").func is dispatch.run_due and scheduler.scheduler.get_job("job_notify_prune").func is dispatch.prune_history
    finally:
        scheduler.scheduler.remove_all_jobs()


# ================================================================ the page
def browser_for(app, pid):
    browser = app.test_client()
    browser.set_cookie("synchub_profile", str(pid))
    return browser


def text(response):
    return response.get_data(as_text=True)


@pytest.fixture
def alice(app, conn):
    pid = add_profile(conn)
    return pid, browser_for(app, pid)


def test_a_guest_is_asked_to_choose_a_profile(client, conn):
    r = client.get("/notifications")
    assert r.status_code == 302 and r.headers["Location"].startswith("/who")
    assert client.post("/api/notifications/verify", json={"provider": "pushover", "config": {"user_key": USER}}).status_code == 403


def test_the_page_shows_a_persons_delivery_methods_with_keys_masked(alice, conn, push):
    pid, browser = alice
    add_channel(conn, pid, device="ipad", label="My iPad")
    page = text(browser.get("/notifications", follow_redirects=True))
    assert "My iPad" in page and USER[-4:] in page and USER not in page and TOKEN not in page
    assert 'data-act="test"' in page and "Not tried yet" in page and 'id="addChannel"' in page and "Nothing has been sent to you yet." in page


def test_the_page_says_what_the_administrator_still_has_to_do(alice, push):
    _, browser = alice
    settings.set_value("pushover_app_token", "")
    page = text(browser.get("/notifications", follow_redirects=True))
    assert "Sending is switched off by the administrator" in page and "Pushover is not set up yet" in page
    switch_on()
    settings.set_value("pushover_app_token", TOKEN)
    page = text(browser.get("/notifications", follow_redirects=True))
    assert "switched off" not in page and "not set up yet" not in page


def test_checking_a_key_lists_the_persons_devices_and_saves_nothing(alice, conn, push):
    _, browser = alice
    push.replies = [Reply(200, {"status": 1, "devices": ["iphone", "ipad"], "licenses": ["iOS"]})]
    r = browser.post("/api/notifications/verify", json={"provider": "pushover", "config": {"user_key": USER}})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "message": "That works.", "choices": {"device": ["iphone", "ipad"]}}
    assert conn.execute("SELECT COUNT(*) FROM notification_channel").fetchone()[0] == 0


def test_a_key_of_the_wrong_shape_is_refused_without_asking_pushover(alice, push):
    _, browser = alice
    r = browser.post("/api/notifications/verify", json={"provider": "pushover", "config": {"user_key": "short"}})
    assert r.status_code == 400 and "30 letters and digits" in r.get_json()["message"] and push.calls == []
    assert browser.post("/api/notifications/verify", json={"provider": "apprise", "config": {"urls": "json://x"}}).status_code == 400
    assert browser.post("/api/notifications/verify", json={"provider": "nope"}).status_code == 400
    assert browser.post("/api/notifications/verify", data="not json").status_code == 400 and push.calls == []


def test_what_pushover_says_about_a_key_reaches_the_person_with_a_fitting_status(alice, push):
    _, browser = alice
    ask = lambda: browser.post("/api/notifications/verify", json={"provider": "pushover", "config": {"user_key": USER}})
    push.replies = [Reply(400, {"user": "invalid", "errors": ["user identifier is invalid"], "status": 0})]
    r = ask()
    assert r.status_code == 400 and "user identifier is invalid" in r.get_json()["message"]
    push.replies = [Reply(500, None)]
    assert ask().status_code == 502
    settings.set_value("pushover_app_token", "")
    r = ask()
    assert r.status_code == 503 and "administrator" in r.get_json()["message"]


def test_adding_a_channel_checks_the_key_again_on_the_server_and_then_saves_it(alice, conn, push):
    pid, browser = alice
    push.replies = [Reply(400, {"user": "invalid", "errors": ["user identifier is invalid"], "status": 0})]
    assert browser.post("/api/notifications/channels", json={"provider": "pushover", "label": "x", "config": {"user_key": USER}}).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM notification_channel").fetchone()[0] == 0                   # a browser's earlier "that works" is not trusted
    push.replies = [Reply(200, {"status": 1, "devices": ["ipad"], "licenses": []})]
    r = browser.post("/api/notifications/channels", json={"provider": "pushover", "label": "  My iPad ", "config": {"user_key": USER, "device": "ipad"}})
    assert r.status_code == 200 and r.get_json()["ok"]
    (c,) = store.list_channels(conn, pid)
    assert (c["label"], c["summary"]["device"]) == ("My iPad", "ipad")
    push.replies = [Reply(200, {"status": 1, "devices": ["ipad"], "licenses": []})]
    r = browser.post("/api/notifications/channels", json={"provider": "pushover", "config": {"user_key": USER, "device": "ipad"}})
    assert r.status_code == 400 and "already added" in r.get_json()["message"]


def test_a_test_message_goes_to_the_persons_own_device_and_shows_in_the_history(alice, conn, push):
    pid, browser = alice
    cid = add_channel(conn, pid)
    r = browser.post(f"/api/notifications/channels/{cid}/test")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "message": "Sent.", "status": "sent"}
    assert push.sent[0]["data"]["user"] == USER
    history = browser.get("/api/notifications/history").get_json()["history"]
    assert [(h["title"], h["status"]) for h in history] == [("SyncHub test", "sent")]
    assert "SyncHub test" in text(browser.get("/notifications", follow_redirects=True))
    push.replies = [Reply(400, {"user": "invalid", "errors": ["user identifier is invalid"], "status": 0})]
    r = browser.post(f"/api/notifications/channels/{cid}/test")
    assert r.status_code == 200 and not r.get_json()["ok"] and "invalid" in r.get_json()["message"]


def test_nobody_can_touch_or_test_someone_elses_delivery_methods(app, conn, push):
    a, b = add_profile(conn, "Alice"), add_profile(conn, "Bob")
    cid = add_channel(conn, a)
    bob = browser_for(app, b)
    assert bob.post(f"/api/notifications/channels/{cid}/test").status_code == 404 and push.calls == []
    assert bob.post(f"/api/notifications/channels/{cid}/enabled", json={"enabled": False}).status_code == 404
    assert bob.post(f"/api/notifications/channels/{cid}/delete").status_code == 404
    assert conn.execute("SELECT enabled FROM notification_channel WHERE id = ?", (cid,)).fetchone()[0] == 1
    page = text(bob.get("/notifications", follow_redirects=True))
    assert USER[-4:] not in page and store.history(conn, b) == []


def test_switching_off_and_removing_a_channel_from_the_page(alice, conn):
    pid, browser = alice
    cid = add_channel(conn, pid)
    assert browser.post(f"/api/notifications/channels/{cid}/enabled", json={"enabled": "yes"}).status_code == 400
    assert browser.post(f"/api/notifications/channels/{cid}/enabled", json={"enabled": False}).get_json()["message"] == "Switched off."
    assert "Switch on" in text(browser.get("/notifications", follow_redirects=True))
    assert browser.post(f"/api/notifications/channels/{cid}/delete").get_json()["ok"]
    assert browser.post(f"/api/notifications/channels/{cid}/delete").status_code == 404


def test_pushover_is_not_asked_about_keys_or_tests_more_than_a_few_times_a_minute(alice, conn, push):
    pid, browser = alice
    cid = add_channel(conn, pid)
    bad = lambda: browser.post("/api/notifications/verify", json={"provider": "pushover", "config": {"user_key": USER}})
    push.replies = [Reply(400, {"user": "invalid", "errors": ["x"], "status": 0})] * 10
    assert [bad().status_code for _ in range(8)] == [400] * 6 + [429] * 2
    assert len(push.calls) == 6
    codes = [browser.post(f"/api/notifications/channels/{cid}/test").status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]


def test_the_throttle_forgets_after_its_window():
    t = Throttle(2, 60)
    assert t.allow("a", 0) and t.allow("a", 1) and not t.allow("a", 2) and t.allow("b", 2) and t.allow("a", 61)


# ================================================================ who may change them
def lock_down(conn, owner_pin="4821"):
    """Alice gets a PIN, so from now on a profile without one is limited to what guests may do."""
    owner = add_profile(conn, "Owner")
    access.set_pin(conn, owner, owner_pin)
    conn.commit()
    return owner


def test_once_someone_has_a_pin_a_profile_without_one_cannot_manage_notifications(app, conn, push):
    lock_down(conn)
    dan = add_profile(conn, "Dan")
    browser = browser_for(app, dan)
    page = text(browser.get("/notifications", follow_redirects=True))
    assert "has no PIN" in page and 'id="channelsPanel"' not in page
    for call in (lambda: browser.post("/api/notifications/verify", json={"provider": "pushover", "config": {"user_key": USER}}),
                 lambda: browser.post("/api/notifications/channels", json={"provider": "pushover", "config": {"user_key": USER}}),
                 lambda: browser.get("/api/notifications/history")):
        assert call().status_code == 403
    assert push.calls == []


def test_a_profile_with_a_pin_that_was_entered_here_can(app, conn, push):
    owner = lock_down(conn)
    browser = app.test_client()
    assert browser.post("/who", data={"profile_id": str(owner), "pin": "4821"}).status_code == 302
    add_channel(conn, owner)
    page = text(browser.get("/notifications", follow_redirects=True))
    assert 'id="channelsPanel"' in page and "has no PIN" not in page
    assert browser.post(f"/api/notifications/channels/{store.list_channels(conn, owner)[0]['id']}/test").get_json()["ok"]


def test_the_notifications_permission_decides_who_may_open_the_page(app, conn):
    owner = lock_down(conn)
    settings.set_value("access_guest_permissions", "dashboard,tv")
    browser = app.test_client()
    browser.post("/who", data={"profile_id": str(owner), "pin": "4821"})
    assert browser.get("/notifications", follow_redirects=True).status_code == 200                             # everything, until limited
    access.set_permissions(conn, owner, ["access", "dashboard"])
    conn.commit()
    assert browser.get("/notifications", follow_redirects=True).status_code == 403 and browser.get("/api/notifications/history").status_code == 403
    access.set_permissions(conn, owner, ["access", "notifications"])
    conn.commit()
    assert browser.get("/notifications", follow_redirects=True).status_code == 200


def test_the_menu_offers_notifications_to_those_who_may_open_them(alice):
    _, browser = alice
    assert 'href="/my-profile"' in text(browser.get("/"))


# ================================================================ Settings
def test_the_administrator_enters_the_application_token_and_it_is_never_shown_again(client, v2):
    r = client.post("/tools/update-settings", data={"tab": "connections", "pushover_app_token": TOKEN, "enable_notifications": "1", "notify_history_keep_days": "30"})
    assert r.status_code == 302
    assert settings.get("pushover_app_token") == TOKEN and settings.get("enable_notifications") == "1" and settings.get("notify_history_keep_days") == "30"
    page = text(client.get("/settings"))
    assert TOKEN not in page and 'name="pushover_app_token"' in page and 'data-test-service="pushover"' in page and "Saved. Leave blank to keep the current value." in page
    client.post("/tools/update-settings", data={"tab": "connections", "pushover_app_token": ""})
    assert settings.get("pushover_app_token") == TOKEN                              # blank keeps it
    assert client.post("/tools/update-settings", data={"tab": "connections", "notify_history_keep_days": "-1"}).status_code == 302
    assert settings.get("notify_history_keep_days") == "30"


def test_testing_the_token_reports_the_allowance_and_ends_a_hold(client, v2, push):
    with db.connection() as c:
        dispatch.pause(c, "pushover", "2999-01-01 00:00:00", "old token")
    push.replies = [Reply(200, {"status": 1, "limit": 10000, "remaining": 9500, "reset": 1793613600})]
    r = client.post("/api/test-connection/pushover", json={"fields": {"pushover_app_token": ""}})
    assert r.get_json() == {"ok": True, "message": "Pushover accepts that token. 9500 of 10000 messages are left this month."}
    with db.connection() as c:
        assert dispatch.paused(c, "pushover") is None
    assert "9500 of 10000 Pushover messages left this month (resets Nov 2)" in text(client.get("/settings"))
    push.replies = [Reply(400, {"token": "invalid", "errors": ["application token is invalid"], "status": 0})]
    r = client.post("/api/test-connection/pushover", json={"fields": {"pushover_app_token": OTHER_USER}})
    assert r.get_json()["ok"] is False and push.calls[-1]["params"] == {"token": OTHER_USER}
    assert pushover.quota()["remaining"] == 9500


def test_trying_a_different_token_does_not_end_the_hold_on_the_one_in_use(client, v2, push):
    with db.connection() as c:
        dispatch.pause(c, "pushover", "2999-01-01 00:00:00", "old token")
    push.replies = [Reply(200, {"status": 1, "limit": 10000, "remaining": 1, "reset": 1793613600})]
    assert client.post("/api/test-connection/pushover", json={"fields": {"pushover_app_token": OTHER_USER}}).get_json()["ok"] is True
    with db.connection() as c:
        assert dispatch.paused(c, "pushover") == "old token"                     # still on hold: the saved token was not the one that worked
    assert pushover.quota() is None                                              # and its allowance is not recorded as ours


def test_saving_a_new_token_ends_a_hold(client, v2):
    with db.connection() as c:
        dispatch.pause(c, "pushover", "2999-01-01 00:00:00", "old token")
    client.post("/tools/update-settings", data={"tab": "connections", "pushover_app_token": TOKEN})
    with db.connection() as c:
        assert dispatch.paused(c, "pushover") is None


def test_a_hold_is_shown_to_the_administrator_and_to_people(alice, client, v2):
    with db.connection() as c:
        dispatch.pause(c, "pushover", "2999-01-01 00:00:00", "the allowance is used up")
    assert "Pushover is on hold: the allowance is used up" in text(client.get("/settings"))
    assert "Sending through Pushover is on hold: the allowance is used up" in text(alice[1].get("/notifications", follow_redirects=True))


def test_the_apprise_test_button_still_works(client, v2, monkeypatch):
    import apprise
    assert client.post("/api/test-connection/notifications", json={"fields": {"apprise_urls": ""}}).get_json() == {"ok": False, "message": "No Apprise URLs set."}
    monkeypatch.setattr(apprise.Apprise, "notify", lambda self, title, body: True)
    assert client.post("/api/test-connection/notifications", json={"fields": {"apprise_urls": "json://localhost"}}).get_json() == {"ok": True, "message": "Notification sent."}
    settings.set_value("apprise_urls", "json://localhost")
    assert client.post("/api/test-connection/notifications", json={"fields": {"apprise_urls": ""}}).get_json()["ok"]            # blank = the saved one


def test_the_old_notifier_is_gone():
    with pytest.raises(ImportError):
        __import__("synchub.notify")
