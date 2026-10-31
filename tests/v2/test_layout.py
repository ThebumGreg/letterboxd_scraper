"""How the pages are organised: Settings in tabs, People (profiles and access together), Workers and Jobs as tabs, the logging
switches on Settings, and page headings that match the menu."""
import html as htmllib
import json
import re

import pytest

from synchub import jobs, settings, workers


def text(response):
    return response.get_data(as_text=True)


def forms(page):
    """{tab: [field names]} for the Settings page's forms."""
    out = {}
    for tab, body in re.findall(r'<form[^>]*class="settings-form" data-tab="(\w+)">(.*?)</form>', page, re.S):
        out[tab] = set(re.findall(r'name="(\w+)"', body))
    return out


# ---------------------------------------------------------------- Settings
def test_settings_has_three_tabs_each_with_a_form_of_its_own(client, v2):
    page = text(client.get("/settings"))
    assert re.findall(r'data-bs-target="#pane-(\w+)"', page) == ["connections", "automation", "logging"]
    found = forms(page)
    assert set(found) == {"connections", "automation", "logging"}
    assert {"jellyfin_url", "jellyfin_api_key", "tmdb_api_key", "radarr_url", "sonarr_url", "apprise_urls"} <= found["connections"]
    assert {"scraper_interval_hours", "metadata_refresh_hours", "queue_poll_seconds", "synchub_url", "backup_keep", "radarr_quality_profile_id", "sonarr_root_folder"} <= found["automation"]
    assert {"payload_logging_enabled", "payload_retention_limit", "log_service_radarr", "log_service_dynu"} <= found["logging"]
    assert not found["connections"] & {"radarr_quality_profile_id", "scraper_interval_hours"} and not found["logging"] & {"jellyfin_url"}


def test_nothing_the_old_single_form_saved_is_lost(client, v2):
    every = set().union(*forms(text(client.get("/settings"))).values())
    expected = {"scraper_interval_hours", "radarr_interval_hours", "sonarr_interval_hours", "jellyfin_interval_hours", "request_delay_min", "request_delay_max",
                "user_delay_min", "user_delay_max", "tmdb_api_key", "metadata_refresh_hours", "queue_poll_seconds", "synchub_url", "jellyfin_events_keep_days",
                "backup_keep", "disable_jellyfin_connection", "jellyfin_url", "jellyfin_api_key", "enable_notifications", "apprise_urls",
                "payload_logging_enabled", "payload_retention_limit"}
    for service in ("radarr", "sonarr"):
        expected |= {f"{service}_{k}" for k in ("url", "api_key", "quality_profile_id", "min_user_count", "root_folder", "enable_queue_addition", "dry_run")}
    expected |= {f"log_service_{s}" for s in ("jellyfin", "radarr", "sonarr", "tmdb", "dynu")}
    assert expected - every == set()


def test_the_tab_in_the_address_is_the_one_that_opens(client, v2):
    assert re.search(r'id="pane-automation"[^>]*class="[^"]*|class="tab-pane fade show active" id="pane-automation"', text(client.get("/settings?tab=automation")))
    assert 'class="tab-pane fade show active" id="pane-automation"' in text(client.get("/settings?tab=automation"))
    assert 'class="tab-pane fade show active" id="pane-connections"' in text(client.get("/settings?tab=nonsense"))
    assert 'class="tab-pane fade show active" id="pane-connections"' in text(client.get("/settings"))


def test_saving_a_tab_changes_only_its_own_settings_and_comes_back_to_it(client, v2):
    settings.set_value("radarr_interval_hours", "5")
    r = client.post("/tools/update-settings", data={"tab": "logging", "payload_logging_enabled": "1", "log_service_radarr": "1", "payload_retention_limit": "1000"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings?tab=logging")
    assert settings.get("payload_logging_enabled") == "1" and settings.get("log_service_radarr") == "1" and settings.get("payload_retention_limit") == "1000"
    assert settings.get("radarr_interval_hours") == "5"
    assert client.post("/tools/update-settings", data={"radarr_interval_hours": "5"}).headers["Location"].endswith("/settings")


def test_profiles_are_no_longer_on_settings(client, v2):
    page = text(client.get("/settings"))
    assert 'id="profileModal"' not in page and "Import Jellyfin Users" not in page and "/people" in page


# ---------------------------------------------------------------- Payloads and Tools
def test_the_payload_page_shows_the_logging_state_and_links_to_the_switches(client, v2):
    page = text(client.get("/payloads"))
    assert "<form" not in page.split('id="loggingStatus"')[1].split("Captured Traffic")[0] and "Payload logging is <strong>off</strong>" in page
    assert "/settings?tab=logging" in page
    settings.set_value("payload_logging_enabled", "1")
    settings.set_value("log_service_radarr", "1")
    assert "is <strong>on</strong> for Radarr" in text(client.get("/payloads"))


def test_the_duplicate_notification_test_is_gone_from_tools(client, v2):
    page = text(client.get("/tools"))
    assert "Send test notification" not in page and "/activity" in page
    assert client.post("/tools/test-notification").status_code == 404
    assert "Send test notification" in text(client.get("/settings"))      # the test sits next to the setting


# ---------------------------------------------------------------- People
def test_people_has_profiles_and_access_on_one_page(client, v2, conn):
    pid = conn.execute("INSERT INTO profile (name) VALUES ('Alice')").lastrowid
    conn.commit()
    page = text(client.get("/people"))
    assert 'id="profiles"' in page and 'id="access"' in page and f'id="person-{pid}"' in page and f'href="/people/{pid}"' in page
    assert "Add Profile" in page and "Guests and PINs" in page and "Remembering a PIN" in page
    person = text(client.get(f"/people/{pid}"))                  # identity, access and notifications for one person, together
    assert all(x in person for x in ('id="pane-identity"', 'id="pane-access"', 'id="pane-notifications"', 'id="adminAlerts"'))


def test_saving_or_deleting_a_profile_returns_to_people(client, v2, conn):
    r = client.post("/settings/profiles/save", data={"profile_name": "Alice", "letterboxd_username": "alice"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/people")
    pid = conn.execute("SELECT id FROM profile").fetchone()["id"]
    assert client.post(f"/settings/profiles/delete/{pid}").headers["Location"].endswith("/people")


# ---------------------------------------------------------------- Workers and Jobs
def test_jobs_are_their_own_tab_and_the_workers_list_holds_the_hardware(client, v2):
    with v2.connection() as conn:
        worker_id, _ = workers.create(conn, "encoder-1")
        job_id = jobs.create(conn, "library", title="Heat")
        conn.execute("UPDATE job SET status = 'needs_review', review_reason = 'low confidence' WHERE id = ?", (job_id,))
    workers_page, fragment, jobs_fragment = (text(client.get(path)) for path in ("/workers", "/workers/fragment", "/jobs/fragment"))
    assert 'id="workerTable"' in fragment and 'id="jobTable"' not in fragment and "data-pane=" not in fragment and f'data-status-worker="{worker_id}"' in fragment
    assert 'id="jobTable"' in jobs_fragment and "1 waiting for your decision" in jobs_fragment
    assert 'id="media-tab-jobs"' in workers_page and 'id="media-tab-workers"' in workers_page and 'id="media-pane-jobs"' in workers_page
    assert text(client.get("/jobs")).count('class="nav-link active" id="media-tab-jobs"') == 1


# ---------------------------------------------------------------- headings match the menu
@pytest.mark.parametrize("path, heading", [("/tools", "Tools &amp; Operations"), ("/logs", "Logs"), ("/payloads", "Payloads"), ("/ddns", "Dynamic DNS"),
                                           ("/settings", "Settings"), ("/people", "People"), ("/activity", "What it&#39;s doing now"), ("/jellyfin-events", "Jellyfin events")])
def test_each_page_is_headed_with_its_menu_name(client, v2, path, heading):
    page = text(client.get(path))
    wanted = htmllib.unescape(heading)
    h2 = htmllib.unescape(re.sub(r"<[^>]+>", "", re.search(r"<h2[^>]*>(.*?)</h2>", page, re.S).group(1))).strip()
    title = htmllib.unescape(re.search(r"<title>(.*?) \| SyncHub</title>", page, re.S).group(1)).strip()
    assert h2 == wanted and title == wanted


# ---------------------------------------------------------------- the scheduler switch
@pytest.mark.parametrize("value, started", [("0", False), (None, True), ("1", True)])
def test_synchub_scheduler_zero_starts_the_app_without_background_jobs(v2, monkeypatch, value, started):
    from synchub import scheduler
    from synchub.web import create_app
    calls = []
    monkeypatch.setattr(scheduler, "start", lambda: calls.append("started"))
    monkeypatch.delenv("SYNCHUB_SCHEDULER", raising=False)
    if value is not None:
        monkeypatch.setenv("SYNCHUB_SCHEDULER", value)
    app = create_app(start_scheduler=True, csrf=False)
    assert bool(calls) is started and app.config["SCHEDULER_OFF"] is (not started)
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "0")
    assert ("Scheduled jobs are switched off" in text(browser.get("/tools"))) is (not started)


# ---------------------------------------------------------------- Watch now
@pytest.fixture
def heat(v2, conn):
    mid = conn.execute("INSERT INTO media (kind, title, tmdb_id, year) VALUES ('movie', 'Heat', '949', 1995)").lastrowid
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES ('jfheat', 'movie', 'a', 'a')")
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', 'jfheat', 'x', 1.0)", (mid,))
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (mid,))
    conn.commit()
    settings.set_value("jellyfin_url", "http://jf:8096")
    return mid


def test_every_place_that_offered_watch_on_jellyfin_now_offers_watch_now(client, heat):
    from config import BASE_DIR
    link = "http://jf:8096/web/index.html#/details?id=jfheat"
    slot = lambda size: f'data-watch-now data-link="{link}" data-item="jfheat" data-title="Heat" data-size="{size}"'
    assert slot("lg") in text(client.get(f"/movie/{heat}"))
    assert slot("row") in text(client.get("/"))
    for page in (f"/movie/{heat}", "/"):
        assert "Watch on Jellyfin" not in text(client.get(page))
    for script in ("static/js/dashboard.js", "static/js/watch.js"):
        assert "Watch on Jellyfin" not in (BASE_DIR / script).read_text()
    assert "js/watch.js" in text(client.get("/"))


def test_the_page_tells_watch_now_whether_the_tvs_may_be_offered(client, v2):
    with v2.connection() as conn:
        conn.execute("INSERT INTO profile (name, pin_hash) VALUES ('Admin', 'x')")        # limits only apply once some profile has a PIN
    config = lambda: json.loads(re.search(r'id="jellyfin-app-config">(.*?)</script>', text(client.get("/")), re.S).group(1))
    assert config()["tv"] is True and config()["tv_setup"] is True                       # everything is open by default
    settings.set_value("access_guest_permissions", "dashboard,tv")
    assert config()["tv"] is True and config()["tv_setup"] is False
    settings.set_value("access_guest_permissions", "dashboard")
    assert config()["tv"] is False


def test_the_tv_list_and_the_play_call_behind_watch_now_are_allowed_by_the_tv_permission(client, v2):
    with v2.connection() as conn:
        conn.execute("INSERT INTO profile (name, pin_hash) VALUES ('Admin', 'x')")        # limits only apply once some profile has a PIN
    settings.set_value("access_guest_permissions", "dashboard,tv")
    assert client.get("/api/tv/config").status_code == 200
    assert client.post("/api/tv/devices/nothing/play", json={"itemIds": ["x"], "playCommand": "PlayNow"}).status_code != 403
    settings.set_value("access_guest_permissions", "dashboard")
    assert client.get("/api/tv/config").status_code == 403
    assert client.post("/api/tv/devices/nothing/play", json={"itemIds": ["x"], "playCommand": "PlayNow"}).status_code == 403


def test_choosing_a_tv_in_watch_now_opens_that_tvs_remote_which_starts_the_title():
    from config import BASE_DIR
    watch = (BASE_DIR / "static/js/watch.js").read_text()
    assert "/tv/remote?${params}" in watch and "tv: button.dataset.watchTv, play: wrap.dataset.item, title: wrap.dataset.title" in watch
    assert "/api/tv/devices/${encodeURIComponent(tv.id)}/play" not in watch                    # the remote page makes the call and shows the steps
    remote = (BASE_DIR / "static/tv/remote.js").read_text()
    assert "q.get('play')" in remote and "history.replaceState(null, '', location.pathname)" in remote and "onStart: startFromLink" in remote
    common = (BASE_DIR / "static/tv/common.js").read_text()
    assert "get('tv')" in common and "hook('onStart')" in common


# ---------------------------------------------------------------- small fixes
def test_the_tools_page_shows_the_match_and_update_title_as_typed(client, v2):
    page = text(client.get("/tools"))
    assert "Match &amp; update" in page and "Match &amp;amp; update" not in page


def test_every_script_that_posts_sends_the_csrf_token():
    # (the TV pages post through the shared api() in tv/common.js, which sends it)
    """The Clear History button on Payloads failed with 'CSRF token missing' because its fetch sent no token."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "static"
    for script in [*root.glob("js/*.js"), *root.glob("tv/*.js")]:
        source = script.read_text()
        if re.search(r"method:\s*['\"](POST|PUT|DELETE)['\"]", source):
            assert "csrf" in source.lower(), f"{script.name} posts without a CSRF token"


def test_the_menu_bar_sits_above_the_dashboard_controls():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "static/css/synchub.css").read_text()
    controls = int(re.search(r"#controls\.panel\s*{[^}]*z-index:\s*(\d+)", css).group(1))
    bar = int(re.search(r"\.navbar\s*{[^}]*z-index:\s*(\d+)", css).group(1))
    assert bar > controls
