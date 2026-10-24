"""PINs and permissions: who is who, what each person may open, the menu they see, and the Access page that changes it."""
import re
import time

import pytest
from itsdangerous import TimestampSigner, URLSafeTimedSerializer

from synchub import access, db, profiles, settings
from synchub.web import PIN_COOKIE, PROFILE_COOKIE, create_app


# ---------------------------------------------------------------- arranging
def aged_token(secret, profile_id, version, days_old):
    """A proof as it would have been signed `days_old` days ago."""
    class OldSigner(TimestampSigner):
        def get_timestamp(self):
            return int(time.time()) - days_old * 86400
    return URLSafeTimedSerializer(secret, salt=access.PROOF_SALT, signer=OldSigner).dumps({"p": profile_id, "v": version})


def add_profile(conn, name, permissions=None, pin=None):
    pid = conn.execute("INSERT INTO profile (name, jellyfin_user_id, permissions) VALUES (?, ?, ?)", (name, f"jf-{name}", permissions)).lastrowid
    if pin:
        access.set_pin(conn, pid, pin)
    conn.commit()
    return pid


@pytest.fixture
def people(v2, conn):
    """Alice: PIN 4821, may open everything. Bob: no PIN, may open the dashboard and search. Cara: PIN 1357, may open Library and Workers."""
    return {"alice": add_profile(conn, "Alice", None, "4821"), "bob": add_profile(conn, "Bob", "dashboard,search"), "cara": add_profile(conn, "Cara", "library,workers", "1357")}


@pytest.fixture
def browser(app):
    """A device that has not chosen anyone yet."""
    return app.test_client()


def choose(browser, pid, pin=None, **extra):
    data = {"profile_id": str(pid), **extra}
    if pin is not None:
        data["pin"] = pin
    return browser.post("/who", data=data)


def sign_in(browser, pid, pin=None):
    r = choose(browser, pid, pin)
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return browser


def text(response):
    return response.get_data(as_text=True)


def cookie(browser, name):
    got = browser.get_cookie(name)
    return got.value if got else None


# ---------------------------------------------------------------- the rules table
def test_every_endpoint_of_the_app_has_an_access_rule(app):
    unlisted = sorted({r.endpoint for r in app.url_map.iter_rules() if access.required(r.endpoint) is None})
    assert unlisted == [], f"give these an entry in access.RULES: {unlisted}"


def test_every_permission_is_needed_by_something():
    used = {need for _, need in access.RULES if need not in (access.PUBLIC, access.OPEN)}
    assert used == set(access.KEYS)


@pytest.mark.parametrize("endpoint, need", [
    ("index", "dashboard"), ("show_details", "dashboard"), ("stream_events", "dashboard"), ("search_page", "search"), ("api_search_add_movie", "requests"),
    ("api_request_all", "requests"), ("api_refresh", "requests"), ("resolution_center", "resolution"), ("api_resolution_approve", "resolution"),
    ("ddns_page", "ddns"), ("api_ddns_check", "ddns"), ("library_page", "library"), ("api_library_scan", "library"), ("workers_page", "workers"), ("api_worker_create", "workers"),
    ("api_job_log", "workers"), ("api_test_copy", "workers"), ("storage_page", "storage"), ("api_storage_create", "storage"), ("tools", "tools"), ("trigger", "tools"),
    ("export_csv", "tools"), ("api_diagnostics", "tools"), ("logs_page", "logs"), ("api_logs", "logs"), ("jellyfin_events_export", "logs"), ("payloads_page", "payloads"),
    ("api_clear_payload_logs", "payloads"), ("settings_page", "settings"), ("update_settings", "settings"), ("api_webhook_setup", "settings"), ("api_test_connection", "settings"),
    ("save_profile", "access"), ("delete_profile", "access"), ("api_jellyfin_users", "access"), ("access_page", "access"), ("api_access_set_pin", "access"),
    ("who", access.PUBLIC), ("static", access.PUBLIC), ("poster", access.PUBLIC), ("api_webhook", access.PUBLIC), ("worker_api_poll", access.PUBLIC), ("worker_api_scan_records", access.PUBLIC),
    ("api_tasks", access.OPEN), ("something_new", None), (None, None)])
def test_what_each_endpoint_needs(endpoint, need):
    assert access.required(endpoint) == need


# ---------------------------------------------------------------- permissions
def test_stored_permissions_mean_everything_nothing_or_a_list():
    assert access.parse(None) is None and access.parse("") == frozenset() and access.parse("library,workers,bogus") == {"library", "workers"}
    assert access.allowed(None, "settings") and access.allowed({"library"}, "library") and not access.allowed({"library"}, "settings") and not access.allowed(frozenset(), "index")
    assert access.clean(["workers", "dashboard"]) == ["dashboard", "workers"] and access.clean(None) == []
    with pytest.raises(access.AccessError, match="no permission called 'root'"):
        access.clean(["root"])


def test_saving_every_permission_stores_everything_so_later_pages_are_included(v2, conn, people):
    access.set_permissions(conn, people["bob"], list(access.KEYS))
    assert conn.execute("SELECT permissions FROM profile WHERE id = ?", (people["bob"],)).fetchone()[0] is None
    access.set_permissions(conn, people["bob"], ["search", "dashboard"])
    assert conn.execute("SELECT permissions FROM profile WHERE id = ?", (people["bob"],)).fetchone()[0] == "dashboard,search"
    access.set_guest_permissions(conn, list(access.KEYS))
    assert settings.get("access_guest_permissions", conn=conn) == "*" and access.guest_permissions(conn) is None
    access.set_guest_permissions(conn, ["dashboard"])
    assert access.guest_permissions(conn) == {"dashboard"}


def test_somebody_always_keeps_the_access_permission(v2, conn, people):
    access.set_guest_permissions(conn, ["dashboard"])
    access.set_permissions(conn, people["cara"], ["library"])                  # Alice still has everything
    with pytest.raises(access.AccessError, match="Somebody has to keep the Access permission"):
        access.set_permissions(conn, people["alice"], ["dashboard"])             # she is the last one
    access.set_permissions(conn, people["cara"], ["library", "access"])
    access.set_permissions(conn, people["alice"], ["dashboard"])                 # now Cara holds it
    with pytest.raises(access.AccessError):
        access.set_permissions(conn, people["cara"], ["library"])
    with pytest.raises(access.AccessError):
        access.ensure_profile_can_go(conn, people["cara"])
    access.ensure_profile_can_go(conn, people["alice"])                          # losing Alice loses nothing
    with pytest.raises(access.AccessError, match="no longer exists"):
        access.set_permissions(conn, 9999, ["access"])


def test_guests_holding_access_count_as_somebody(v2, conn, people):
    access.set_guest_permissions(conn, list(access.KEYS))                        # the default: everyone can
    access.set_permissions(conn, people["alice"], ["dashboard"])
    with pytest.raises(access.AccessError):
        access.set_guest_permissions(conn, ["dashboard"])


def test_an_installation_that_never_opens_the_access_page_changes_nothing(v2, conn):
    pid = conn.execute("INSERT INTO profile (name) VALUES ('Old')").lastrowid
    row = conn.execute("SELECT * FROM profile WHERE id = ?", (pid,)).fetchone()
    assert row["permissions"] is None and row["pin_hash"] is None and row["pin_version"] == 0
    assert access.guest_permissions(conn) is None


# ---------------------------------------------------------------- PINs
@pytest.mark.parametrize("pin", ["", None, "123", "123456789", "12a4", "12 34", "-1234", "١٢٣٤"])
def test_a_pin_is_four_to_eight_digits(v2, conn, people, pin):
    with pytest.raises(access.AccessError, match="4 to 8 digits"):
        access.set_pin(conn, people["bob"], pin)


def test_a_pin_is_stored_hashed_and_changing_it_signs_everyone_out(v2, conn, people):
    row = conn.execute("SELECT pin_hash, pin_version FROM profile WHERE id = ?", (people["alice"],)).fetchone()
    assert row["pin_hash"] and "4821" not in row["pin_hash"] and row["pin_hash"] != "4821" and row["pin_version"] == 1
    access.set_pin(conn, people["alice"], " 4821 ")
    assert conn.execute("SELECT pin_version FROM profile WHERE id = ?", (people["alice"],)).fetchone()[0] == 2
    access.sign_out_everywhere(conn, people["alice"])
    access.clear_pin(conn, people["alice"])
    assert conn.execute("SELECT pin_hash, pin_version FROM profile WHERE id = ?", (people["alice"],)).fetchone()[:] == (None, 4)
    for call in (access.set_pin, ):
        with pytest.raises(access.AccessError, match="no longer exists"):
            call(conn, 9999, "1234")
    for call in (access.clear_pin, access.sign_out_everywhere):
        with pytest.raises(access.AccessError):
            call(conn, 9999)


def test_the_listing_of_profiles_never_includes_a_pin(v2, conn, people):
    for p in profiles.list_all(conn):
        assert "pin_hash" not in p and set(p) >= {"id", "name", "has_pin"}
    assert {p["name"]: p["has_pin"] for p in profiles.list_all(conn)} == {"Alice": 1, "Bob": 0, "Cara": 1}


def test_wrong_pins_are_counted_and_the_fifth_locks_for_a_while(v2, conn, people):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    tries = [access.check_pin(conn, people["alice"], "0000", now=t0) for _ in range(5)]
    assert tries[:4] == [("wrong", 4), ("wrong", 3), ("wrong", 2), ("wrong", 1)] and tries[4] == ("locked", 30)
    assert access.check_pin(conn, people["alice"], "4821", now=t0 + timedelta(seconds=10))[0] == "locked"          # the right PIN does not help while locked
    outcome, wait = access.check_pin(conn, people["alice"], "4821", now=t0 + timedelta(seconds=10))
    assert 19 <= wait <= 21
    assert access.check_pin(conn, people["alice"], "0000", now=t0 + timedelta(seconds=31)) == ("locked", 60)       # a wrong one after the lock doubles it
    assert access.check_pin(conn, people["alice"], "4821", now=t0 + timedelta(seconds=31 + 61))[0] == "ok"
    assert conn.execute("SELECT pin_failures, pin_locked_until FROM profile WHERE id = ?", (people["alice"],)).fetchone()[:] == (0, None)         # success forgets the failures
    assert access.check_pin(conn, people["alice"], "0000", now=t0)[0] == "wrong"                                    # and counting starts again


def test_the_wait_doubles_up_to_fifteen_minutes():
    assert [access.lock_seconds(n) for n in (0, 4, 5, 6, 7, 8, 9, 10, 11, 12, 30)] == [0, 0, 30, 60, 120, 240, 480, 900, 900, 900, 900]


def test_unlocking_clears_the_lock(v2, conn, people):
    for _ in range(5):
        access.check_pin(conn, people["alice"], "0000")
    assert access.check_pin(conn, people["alice"], "4821")[0] == "locked"
    access.unlock(conn, people["alice"])
    assert access.check_pin(conn, people["alice"], "4821")[0] == "ok"


def test_a_profile_without_a_pin_or_that_is_gone_has_nothing_to_check(v2, conn, people):
    assert access.check_pin(conn, people["bob"], "1234") == ("none", None) and access.check_pin(conn, 9999, "1234") == ("none", None)
    assert access.locked_for({"pin_locked_until": "garbage"}) == 0 and access.locked_for({"pin_locked_until": None}) == 0


def test_attempts_survive_a_restart_because_they_are_in_the_database(v2, conn, people):
    for _ in range(3):
        access.check_pin(conn, people["alice"], "0000")
    conn.commit()
    create_app(csrf=False)                                                                  # "restart"
    assert access.check_pin(conn, people["alice"], "0000") == ("wrong", 1)


# ---------------------------------------------------------------- remembering a PIN on a device
def test_a_proof_is_for_one_profile_at_one_pin_version_and_expires():
    secret = "s3cret"
    token = access.issue_proof(secret, 7, 3)
    assert access.proof_valid(secret, token, 7, 3, 30)
    assert not access.proof_valid(secret, token, 8, 3, 30) and not access.proof_valid(secret, token, 7, 4, 30)          # someone else's, or the PIN changed since
    assert not access.proof_valid("other-key", token, 7, 3, 30) and not access.proof_valid(secret, token + "x", 7, 3, 30)
    assert not access.proof_valid(secret, "", 7, 3, 30) and not access.proof_valid(secret, None, 7, 3, 30) and not access.proof_valid(secret, "garbage", 7, 3, 30)

    old = aged_token(secret, 7, 3, 40)
    assert not access.proof_valid(secret, old, 7, 3, 30) and access.proof_valid(secret, old, 7, 3, 60)                  # 40 days old
    assert not access.proof_valid(secret, URLSafeTimedSerializer(secret, salt="another").dumps({"p": 7, "v": 3}), 7, 3, 30)       # a token made for something else


def test_the_signing_key_is_kept_so_a_restart_does_not_sign_anyone_out(v2, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    first = access.persistent_secret()
    assert len(first) == 64 and access.persistent_secret() == first
    assert create_app(csrf=False).secret_key == first and create_app(csrf=False).secret_key == first
    monkeypatch.setenv("SECRET_KEY", "from-the-environment")
    assert create_app(csrf=False).secret_key == "from-the-environment"


def test_the_session_length_is_a_setting_with_a_safe_fallback(v2):
    assert access.session_days() == 30
    for stored, expected in (("7", 7), ("365", 365), ("0", 30), ("366", 30), ("abc", 30)):
        settings.set_value("access_session_days", stored)
        assert access.session_days() == expected


# ---------------------------------------------------------------- choosing a profile with a PIN
def test_choosing_a_profile_with_a_pin_asks_for_it_and_sets_nothing_yet(browser, people):
    r = choose(browser, people["alice"], next="/settings")
    page = text(r)
    assert r.status_code == 200 and "Enter the PIN for Alice" in page and 'name="pin"' in page and 'name="next" value="/settings"' in page
    assert cookie(browser, PROFILE_COOKIE) is None and cookie(browser, PIN_COOKIE) is None


def test_the_right_pin_signs_in_with_two_cookies_that_scripts_cannot_read(browser, people):
    r = choose(browser, people["alice"], "4821", next="/settings")
    assert r.status_code == 302 and r.headers["Location"] == "/settings"
    assert cookie(browser, PROFILE_COOKIE) == str(people["alice"]) and cookie(browser, PIN_COOKIE)
    headers = " ".join(r.headers.getlist("Set-Cookie"))
    assert headers.count("HttpOnly") == 2 and headers.count("SameSite=Lax") == 2 and f"Max-Age={30 * 86400}" in headers.split(PIN_COOKIE)[1]
    assert browser.get("/settings").status_code == 200


def test_a_wrong_pin_is_refused_with_the_tries_left_and_then_locks(browser, people):
    for left in ("4 more tries", "3 more tries", "2 more tries", "1 more try before it locks"):
        r = choose(browser, people["alice"], "0000")
        assert r.status_code == 401 and f"That PIN is not right. {left}" in text(r) and cookie(browser, PIN_COOKIE) is None
    r = choose(browser, people["alice"], "0000")                                                                   # the fifth
    assert r.status_code == 429 and "Too many wrong tries. Try again in 30 seconds." in text(r)
    r = choose(browser, people["alice"], "4821")
    assert r.status_code == 429 and cookie(browser, PIN_COOKIE) is None and cookie(browser, PROFILE_COOKIE) is None   # locked: even the right PIN waits


def test_the_prompt_can_be_opened_directly_and_only_for_a_profile_that_has_a_pin(browser, people):
    assert "Enter the PIN for Cara" in text(browser.get(f"/who?profile={people['cara']}"))
    page = text(browser.get(f"/who?profile={people['bob']}"))
    assert "Who is watching?" in page and "Enter the PIN" not in page
    assert "Who is watching?" in text(browser.get("/who?profile=999"))


def test_locks_show_on_the_chooser_and_a_profile_without_a_pin_signs_in_at_once(browser, people):
    page = text(browser.get("/who"))
    assert re.search(r"Alice\s*<i class=\"bi bi-lock-fill", page) and not re.search(r"Bob\s*<i class=\"bi bi-lock-fill", page)
    assert 'aria-label="Continue as Alice (asks for a PIN)"' in page and 'aria-label="Continue as Bob"' in page
    r = choose(browser, people["bob"])
    assert r.status_code == 302 and cookie(browser, PROFILE_COOKIE) == str(people["bob"]) and cookie(browser, PIN_COOKIE) is None


def test_switching_away_forgets_the_pin_proof(browser, people):
    sign_in(browser, people["alice"], "4821")
    assert cookie(browser, PIN_COOKIE)
    choose(browser, people["bob"])
    assert cookie(browser, PIN_COOKIE) is None
    sign_in(browser, people["cara"], "1357")
    choose(browser, "0")
    assert cookie(browser, PIN_COOKIE) is None and cookie(browser, PROFILE_COOKIE) == "0"


def test_a_pin_removed_while_the_prompt_was_open_sends_you_back_to_choose(browser, v2, conn, people):
    access.clear_pin(conn, people["alice"])
    conn.commit()
    r = browser.post("/who", data={"profile_id": str(people["alice"]), "pin": "4821"})
    assert r.status_code == 302 and cookie(browser, PROFILE_COOKIE) == str(people["alice"])                       # no PIN any more: it simply signs in


def test_a_profile_cookie_alone_is_not_enough_for_a_profile_with_a_pin(browser, people):
    browser.set_cookie(PROFILE_COOKIE, str(people["alice"]))
    r = browser.get("/settings")
    assert r.status_code == 302 and f"/who?next=%2Fsettings&profile={people['alice']}" in r.headers["Location"] or f"profile={people['alice']}" in r.headers["Location"]
    assert "Enter the PIN for Alice" in text(browser.get(r.headers["Location"]))
    api = browser.get("/api/logs")
    assert api.status_code == 401 and api.get_json()["ok"] is False and "PIN" in api.get_json()["message"] and "/who?profile=" in api.get_json()["login"]
    assert browser.post("/tools/trigger/all").status_code == 401                                                  # nor can it act
    assert browser.get("/poster/1").status_code in (200, 404)                                                      # images and webhooks are not behind it
    assert browser.post("/api/webhook/radarr", json={"eventType": "Test"}).status_code == 200


def test_someone_whose_pin_has_not_been_entered_sees_the_guest_menu_not_the_profiles(browser, v2, conn, people):
    access.set_guest_permissions(conn, ["dashboard"])
    conn.commit()
    browser.set_cookie(PROFILE_COOKIE, str(people["alice"]))                                                      # Alice can open everything; she has not entered her PIN here
    shown = menu(text(browser.get(f"/who?profile={people['alice']}")))
    assert shown == {"links": ["Dashboard"], "dropdowns": {}}


def test_webhooks_and_workers_do_not_look_anyone_up(browser, app, people, monkeypatch):
    import synchub.web as web
    monkeypatch.setattr(web, "_profile_rows", lambda conn: pytest.fail("a webhook or a worker call must not read the profiles"))
    assert browser.post("/api/webhook/radarr", json={"eventType": "Test"}).status_code == 200
    assert browser.post("/api/worker/v1/poll", json={}).status_code == 401


def test_a_forged_a_stale_or_someone_elses_proof_does_not_count(browser, app, v2, conn, people):
    sign_in(browser, people["alice"], "4821")
    good = cookie(browser, PIN_COOKIE)
    assert browser.get("/settings").status_code == 200
    browser.set_cookie(PIN_COOKIE, access.issue_proof("not-the-apps-key", people["alice"], 1))
    assert browser.get("/settings").status_code == 302                                                             # signed with another key
    browser.set_cookie(PIN_COOKIE, access.issue_proof(app.secret_key, people["cara"], 1))
    assert browser.get("/settings").status_code == 302                                                             # a proof for Cara used as Alice
    browser.set_cookie(PIN_COOKIE, good)
    assert browser.get("/settings").status_code == 200
    access.set_pin(conn, people["alice"], "4821")
    conn.commit()
    assert browser.get("/settings").status_code == 302                                                             # the PIN changed: this device asks again
    sign_in(browser, people["alice"], "4821")
    access.sign_out_everywhere(conn, people["alice"])
    conn.commit()
    assert browser.get("/settings").status_code == 302
    sign_in(browser, people["alice"], "4821")
    settings.set_value("access_session_days", "1")
    assert browser.get("/settings").status_code == 200
    version = conn.execute("SELECT pin_version FROM profile WHERE id = ?", (people["alice"],)).fetchone()[0]
    browser.set_cookie(PIN_COOKIE, aged_token(app.secret_key, people["alice"], version, 0))
    assert browser.get("/settings").status_code == 200                                                             # fresh: fine
    browser.set_cookie(PIN_COOKIE, aged_token(app.secret_key, people["alice"], version, 3))
    assert browser.get("/settings").status_code == 302                                                             # older than the remembered days


def test_removing_the_pin_makes_the_profile_cookie_enough_again(browser, v2, conn, people):
    sign_in(browser, people["alice"], "4821")
    access.clear_pin(conn, people["alice"])
    conn.commit()
    assert browser.get("/settings").status_code == 200


def test_a_deleted_profile_is_nobody(browser, v2, conn, people):
    sign_in(browser, people["alice"], "4821")
    conn.execute("DELETE FROM profile WHERE id = ?", (people["alice"],))
    conn.commit()
    assert browser.get("/").status_code == 302 and "/who" in browser.get("/").headers["Location"]


# ---------------------------------------------------------------- what a person may open
def test_a_profile_can_open_exactly_the_pages_it_was_given(browser, people):
    sign_in(browser, people["cara"], "1357")
    assert browser.get("/library").status_code == 200 and browser.get("/workers").status_code == 200
    for denied in ("/", "/settings", "/storage", "/tools", "/logs", "/payloads", "/ddns", "/search", "/resolution", "/access", "/jellyfin-events"):
        r = browser.get(denied)
        assert r.status_code == 403, denied
    page = text(browser.get("/settings"))
    assert "This is not open to Cara" in page and "You do not have access to Settings" in page and "Choose another profile" in page and "Back to the dashboard" not in page


def test_the_denial_of_an_action_is_json_for_scripts_and_a_page_for_forms(browser, people):
    sign_in(browser, people["bob"])
    r = browser.get("/api/logs")
    assert r.status_code == 403 and r.get_json() == {"ok": False, "message": "You don't have access to Logs."}
    assert browser.post("/api/library/scan", json={"root": "x"}).status_code == 403
    assert browser.post("/api/request/movie/1", json={}).get_json()["message"] == "You don't have access to Requests."
    form = browser.post("/tools/trigger/all")
    assert form.status_code == 403 and "This is not open to Bob" in text(form) and "Back to the dashboard" in text(form)


def test_the_live_stream_belongs_to_the_dashboard(browser, people):
    sign_in(browser, people["cara"], "1357")
    assert browser.get("/api/stream/events").status_code == 403


def test_actions_need_their_own_permission_even_on_a_page_the_person_can_open(browser, v2, conn, people):
    access.set_permissions(conn, people["bob"], ["dashboard"])
    conn.commit()
    sign_in(browser, people["bob"])
    assert browser.get("/").status_code == 200
    assert browser.post("/api/request/movie/1", json={}).status_code == 403 and browser.post("/api/search/add/movie", json={}).status_code == 403
    assert browser.post("/api/movie/1/refresh").status_code == 403
    access.set_permissions(conn, people["bob"], ["dashboard", "requests"])
    conn.commit()
    assert browser.post("/api/request/movie/1", json={}).status_code != 403


def test_guests_have_their_own_permissions_and_a_browser_that_never_chose_is_a_guest_for_scripts(browser, v2, conn, people):
    access.set_guest_permissions(conn, ["dashboard"])
    conn.commit()
    choose(browser, "0")
    assert browser.get("/").status_code == 200 and browser.get("/settings").status_code == 403 and "This is not open to guests" in text(browser.get("/settings"))
    fresh = create_app(csrf=False).test_client()
    assert fresh.get("/api/logs").status_code == 403 and fresh.get("/").status_code == 302                         # pages still send a newcomer to choose
    assert fresh.get("/api/tasks").status_code == 200                                                              # open to anyone


def test_webhooks_workers_images_and_the_chooser_are_never_behind_permissions(browser, v2, conn, people):
    access.set_guest_permissions(conn, [])
    access.set_permissions(conn, people["alice"], list(access.KEYS))
    conn.commit()
    choose(browser, "0")
    assert browser.post("/api/webhook/radarr", json={"eventType": "Test"}).status_code == 200
    assert browser.post("/api/worker/v1/poll", json={}).status_code == 401                                         # the worker's own token check, not ours
    assert browser.get("/who").status_code == 200 and browser.get("/static/css/synchub.css").status_code == 200
    assert browser.get("/").status_code == 403


def test_a_route_nobody_gave_a_rule_is_closed(browser, app, people):
    @app.route("/brand-new-page")
    def brand_new_page():
        return "should never be seen"
    sign_in(browser, people["alice"], "4821")
    r = browser.get("/brand-new-page")
    assert r.status_code == 403 and "should never be seen" not in text(r) and "has not been given an access rule" in text(r)


def test_everything_stays_open_when_nobody_changed_anything(browser, v2, conn):
    pid = conn.execute("INSERT INTO profile (name) VALUES ('Dana')").lastrowid
    conn.commit()
    sign_in(browser, pid)
    for path in ("/", "/search", "/resolution", "/ddns", "/library", "/workers", "/storage", "/tools", "/logs", "/payloads", "/settings", "/access", "/jellyfin-events"):
        assert browser.get(path).status_code == 200, path


# ---------------------------------------------------------------- the menu
def menu(page):
    """{'links': top-level labels, 'dropdowns': {label: [item labels]}} from the page's navigation bar."""
    nav = re.search(r'<ul class="navbar-nav me-auto">(.*?)</ul>\s*(?:<a class="nav-link text-body-secondary|</div>)', page, re.S).group(1)
    dropdowns = {}
    for toggle, body in re.findall(r'<a class="nav-link dropdown-toggle[^"]*"[^>]*>(.*?)</a>\s*<ul class="dropdown-menu">(.*?)</ul>', nav, re.S):
        dropdowns[toggle.strip()] = re.findall(r'class="dropdown-item[^"]*"[^>]*>(.*?)</a>', body)
    flat = re.sub(r'<li class="nav-item dropdown">.*?</ul>\s*</li>', "", nav, flags=re.S)
    return {"links": re.findall(r'class="nav-link[^"]*"[^>]*>(.*?)</a>', flat), "dropdowns": dropdowns}


def test_the_menu_groups_workers_library_and_storage_and_tools_logs_and_payloads(browser, v2, conn):
    pid = conn.execute("INSERT INTO profile (name) VALUES ('Dana')").lastrowid
    conn.commit()
    sign_in(browser, pid)
    shown = menu(text(browser.get("/")))
    assert shown["links"] == ["Dashboard", "Search", "Resolution Center", "Dynamic DNS", "Settings"]
    assert shown["dropdowns"] == {"Media": ["Library", "Workers", "Storage"], "System": ["Tools &amp; Operations", "Logs", "Payloads", "Jellyfin events", "Access"]}


def test_the_menu_shows_only_what_the_person_may_open(browser, people):
    sign_in(browser, people["cara"], "1357")
    shown = menu(text(browser.get("/library")))
    assert shown == {"links": [], "dropdowns": {"Media": ["Library", "Workers"]}}
    sign_in(browser, people["bob"])
    assert menu(text(browser.get("/"))) == {"links": ["Dashboard", "Search"], "dropdowns": {}}


def test_the_dropdown_holding_the_current_page_is_marked_active(browser, v2, conn):
    pid = conn.execute("INSERT INTO profile (name) VALUES ('Dana')").lastrowid
    conn.commit()
    sign_in(browser, pid)
    page = text(browser.get("/workers"))
    assert re.search(r'dropdown-toggle active"[^>]*>Media', page) and not re.search(r'dropdown-toggle active"[^>]*>System', page)
    assert re.search(r'dropdown-item active" href="/workers" aria-current="page"', page)
    assert re.search(r'dropdown-toggle active"[^>]*>System', text(browser.get("/logs"))) and re.search(r'nav-link active" href="/settings"', text(browser.get("/settings")))


def test_the_dropdowns_work_on_a_phone_menu(browser, v2, conn):
    pid = conn.execute("INSERT INTO profile (name) VALUES ('Dana')").lastrowid
    conn.commit()
    sign_in(browser, pid)
    page = text(browser.get("/"))
    assert page.count('data-bs-toggle="dropdown"') == 2 and 'aria-expanded="false"' in page and 'class="collapse navbar-collapse"' in page


def test_settings_hides_profile_management_from_people_without_access_and_never_prints_a_pin(browser, v2, conn, people):
    sign_in(browser, people["alice"], "4821")
    page = text(browser.get("/settings"))
    assert "Import Jellyfin Users" in page and 'id="profileModal"' in page and "PIN and pages" in page and "bi-lock-fill" in page
    assert "scrypt:" not in page and "pbkdf2:" not in page
    access.set_permissions(conn, people["cara"], ["settings"])
    conn.commit()
    sign_in(browser, people["cara"], "1357")
    page = text(browser.get("/settings"))
    assert "Import Jellyfin Users" not in page and 'id="profileModal"' not in page and "profile-edit" not in page and "System Settings" in page
    assert browser.post("/settings/profiles/save", data={"profile_name": "Eve"}).status_code == 403
    assert browser.post(f"/settings/profiles/delete/{people['bob']}").status_code == 403 and browser.post("/api/profiles/toggle", json={"id": 1, "is_active": 0}).status_code == 403
    assert browser.get("/api/jellyfin/users").status_code == 403


def test_the_last_person_who_can_manage_access_cannot_be_deleted(browser, v2, conn, people):
    access.set_permissions(conn, people["cara"], ["library"])
    access.set_guest_permissions(conn, ["dashboard"])
    conn.commit()
    sign_in(browser, people["alice"], "4821")
    page = text(browser.post(f"/settings/profiles/delete/{people['alice']}", follow_redirects=True))
    assert "Somebody has to keep the Access permission" in page
    assert conn.execute("SELECT COUNT(*) FROM profile WHERE id = ?", (people["alice"],)).fetchone()[0] == 1
    assert "Profile removed" in text(browser.post(f"/settings/profiles/delete/{people['bob']}", follow_redirects=True))


# ---------------------------------------------------------------- the Access page
def api(browser, url, body=None):
    return browser.post(url, json=body if body is not None else {})


@pytest.fixture
def admin(browser, people):
    return sign_in(browser, people["alice"], "4821")


def test_the_access_page_lists_everyone_with_their_pin_state_and_permissions(admin, people):
    page = text(admin.get("/access"))
    assert "PIN set" in page and "No PIN" in page and ">you<" in page and 'data-card="guest"' in page
    for key in access.KEYS:
        assert f'id="perm-guest-{key}"' in page and f'id="perm-{people["bob"]}-{key}"' in page
    assert re.search(r'id="perm-%d-dashboard" value="dashboard" checked' % people["bob"], page) and not re.search(r'id="perm-%d-settings" value="settings" checked' % people["bob"], page)
    assert "python -m synchub.access reset-all" in page


def test_the_page_warns_about_profiles_without_a_pin_that_can_do_more_than_a_guest(admin, v2, conn, people):
    page = text(admin.get("/access"))
    assert 'id="accessWarnings"' in page and "Guests (anyone who picks" in page                              # guests may open everything by default, access included
    access.set_guest_permissions(conn, ["dashboard"])
    access.set_permissions(conn, people["bob"], ["dashboard", "search", "settings"])
    conn.commit()
    warnings = access.overview(conn)["warnings"]
    assert len(warnings) == 1 and warnings[0].startswith("Bob has no PIN") and "Search" in warnings[0] and "Settings" in warnings[0] and "anyone on your network" in warnings[0]
    access.set_pin(conn, people["bob"], "2468")
    conn.commit()
    assert access.overview(conn)["warnings"] == [] and "Every profile that can open more than a guest can has a PIN." in text(admin.get("/access"))


def test_permissions_are_saved_checked_and_refused_when_they_would_lock_everyone_out(admin, v2, conn, people):
    r = api(admin, f"/api/access/profile/{people['bob']}/permissions", {"permissions": ["library", "dashboard"]})
    assert r.get_json() == {"ok": True, "message": "Saved."}
    assert conn.execute("SELECT permissions FROM profile WHERE id = ?", (people["bob"],)).fetchone()[0] == "dashboard,library"
    assert api(admin, f"/api/access/profile/{people['bob']}/permissions", {"permissions": ["root"]}).status_code == 400
    assert api(admin, f"/api/access/profile/{people['bob']}/permissions", {"permissions": "library"}).status_code == 400
    assert api(admin, "/api/access/guest", {"permissions": ["dashboard"]}).get_json()["ok"]
    bad = api(admin, f"/api/access/profile/{people['alice']}/permissions", {"permissions": ["dashboard"]})
    assert bad.status_code == 400 and "Somebody has to keep the Access permission" in bad.get_json()["message"]
    assert api(admin, "/api/access/profile/999/permissions", {"permissions": []}).status_code == 400


def test_a_pin_is_set_by_someone_with_access_and_must_be_four_to_eight_digits(admin, conn, people):
    for bad in ("12", "abcd", "123456789", ""):
        r = api(admin, f"/api/access/profile/{people['bob']}/pin", {"pin": bad})
        assert r.status_code == 400 and "4 to 8 digits" in r.get_json()["message"]
    assert api(admin, f"/api/access/profile/{people['bob']}/pin", {"pin": "2468"}).get_json()["ok"]
    assert access.check_pin(conn, people["bob"], "2468")[0] == "ok"
    conn.commit()                                                                  # (checking a PIN writes; do not hold the database while the app is asked something)
    assert api(admin, "/api/access/profile/999/pin", {"pin": "2468"}).status_code == 400


def test_changing_your_own_pin_keeps_this_device_signed_in_and_removing_it_forgets_the_proof(admin, v2, conn, people):
    before = cookie(admin, PIN_COOKIE)
    r = api(admin, f"/api/access/profile/{people['alice']}/pin", {"pin": "9753"})
    assert r.status_code == 200 and "PIN saved" in r.get_json()["message"] and cookie(admin, PIN_COOKIE) not in (None, before)
    assert admin.get("/access").status_code == 200                                                                  # still in, with the new proof
    other = create_app(csrf=False).test_client()
    assert choose(other, people["alice"], "4821").status_code == 401                                              # the old PIN is gone
    assert choose(other, people["alice"], "9753").status_code == 302
    r = api(admin, f"/api/access/profile/{people['alice']}/pin/clear")
    assert r.status_code == 200 and cookie(admin, PIN_COOKIE) is None and admin.get("/access").status_code == 200


def test_changing_someone_elses_pin_signs_them_out_but_not_you(admin, app, v2, conn, people):
    cara = create_app(csrf=False).test_client()
    sign_in(cara, people["cara"], "1357")
    assert cara.get("/library").status_code == 200
    api(admin, f"/api/access/profile/{people['cara']}/pin", {"pin": "8642"})
    assert cara.get("/library").status_code == 302 and admin.get("/access").status_code == 200


def test_sign_out_everywhere_and_unlock_work_from_the_page(admin, v2, conn, people):
    cara = create_app(csrf=False).test_client()
    sign_in(cara, people["cara"], "1357")
    assert api(admin, f"/api/access/profile/{people['cara']}/signout").get_json()["ok"] and cara.get("/library").status_code == 302
    for _ in range(5):
        choose(create_app(csrf=False).test_client(), people["cara"], "0000")
    assert "locked for" in text(admin.get("/access"))
    assert api(admin, f"/api/access/profile/{people['cara']}/unlock").get_json()["message"] == "Unlocked."
    assert "locked for" not in text(admin.get("/access")) and choose(create_app(csrf=False).test_client(), people["cara"], "1357").status_code == 302


def test_the_remembered_days_are_validated(admin):
    assert api(admin, "/api/access/session-days", {"days": 7}).get_json()["message"] == "A PIN is asked for again after 7 days on a device." and settings.get("access_session_days") == "7"
    assert "1 day " in api(admin, "/api/access/session-days", {"days": 1}).get_json()["message"]
    for bad in (0, 366, "x", None):
        assert api(admin, "/api/access/session-days", {"days": bad}).status_code == 400
    assert settings.get("access_session_days") == "1"


def test_only_someone_with_access_can_use_any_of_it(browser, people):
    sign_in(browser, people["cara"], "1357")
    for url in (f"/api/access/profile/{people['bob']}/permissions", f"/api/access/profile/{people['bob']}/pin", f"/api/access/profile/{people['bob']}/pin/clear",
                f"/api/access/profile/{people['bob']}/signout", f"/api/access/profile/{people['bob']}/unlock", "/api/access/guest", "/api/access/session-days"):
        assert api(browser, url, {"permissions": list(access.KEYS), "pin": "1111", "days": 1}).status_code == 403, url
    assert browser.get("/access").status_code == 403


# ---------------------------------------------------------------- getting back in
def test_the_command_line_can_take_a_pin_off_one_profile_or_all_and_give_everything_back(v2, conn, people, capsys):
    access.set_permissions(conn, people["cara"], ["library", "access"])
    access.set_permissions(conn, people["alice"], ["dashboard"])
    access.set_guest_permissions(conn, ["dashboard"])
    conn.commit()
    assert access.main(["list"]) == 0
    listing = capsys.readouterr().out
    assert "Alice: PIN set, dashboard" in listing and "Bob: no PIN, dashboard, search" in listing and "Cara: PIN set, library, access" in listing
    assert access.main(["reset-pin", "alice"]) == 0 and "Removed the PIN and gave back every permission for: Alice" in capsys.readouterr().out
    row = conn.execute("SELECT pin_hash, permissions FROM profile WHERE id = ?", (people["alice"],)).fetchone()
    assert (row[0], row[1]) == (None, None) and conn.execute("SELECT pin_hash IS NOT NULL FROM profile WHERE id = ?", (people["cara"],)).fetchone()[0] == 1
    assert access.guest_permissions(conn) == {"dashboard"}                                                          # a single reset leaves the guests alone
    assert access.main(["reset-all"]) == 0 and "Alice, Bob, Cara" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM profile WHERE pin_hash IS NOT NULL OR permissions IS NOT NULL").fetchone()[0] == 0 and access.guest_permissions(conn) is None


def test_the_command_line_explains_itself_and_unknown_names(v2, conn, people, capsys):
    assert access.main([]) == 2 and "usage:" in capsys.readouterr().out
    assert access.main(["reset-pin"]) == 2 and access.main(["format-disk"]) == 2
    capsys.readouterr()
    assert access.main(["reset-pin", "Nobody"]) == 1 and "There is no profile called 'Nobody'" in capsys.readouterr().out


def test_a_reset_signs_out_every_device(browser, v2, conn, people):
    sign_in(browser, people["alice"], "4821")
    access.reset(conn, "Alice")
    conn.commit()
    assert browser.get("/settings").status_code == 200                                                              # no PIN now, the profile cookie is enough
    access.set_pin(conn, people["alice"], "4821")
    conn.commit()
    assert browser.get("/settings").status_code == 302
