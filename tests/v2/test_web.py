import html as htmllib
import json
import re

import pytest

from synchub import catalog, settings, tasks
from synchub.clients import ApiError

from . import test_end_to_end as e2e
from .fakes import ServarrSetup, SonarrSetup


@pytest.fixture
def app(v2):
    from synchub.web import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seeded(v2, app):
    """The Last Ship scenario from the end-to-end test, reconciled, plus a profile that wants it."""
    with v2.connection() as conn:
        e2e.load_sources(conn)
        conn.execute("INSERT INTO profile (name, serializd_username) VALUES ('Alice', 'alice_sz')")
    tasks.reconcile_now(e2e.tmdb())
    with v2.connection() as conn:
        mid = conn.execute("SELECT id FROM media").fetchone()[0]
        conn.execute("INSERT INTO src_serializd_entry (username, serializd_show_id, title, first_seen, last_seen) VALUES ('alice_sz', 's1', 'The Last Ship', 'a', 'a')")
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'serializd', 's1', 'tmdb_id', 1.0)", (mid,))
    return mid


def text(response):
    return response.get_data(as_text=True)


# ---------------------------------------------------------------- pages
@pytest.mark.parametrize("path", ["/", "/tools", "/settings", "/resolution", "/ddns", "/payloads", "/logs"])
def test_pages_render_empty(client, path):
    assert client.get(path).status_code == 200


def test_pages_render_with_data(client, seeded):
    for path in ("/", "/tools", "/settings", "/resolution", f"/show/{seeded}"):
        assert client.get(path).status_code == 200, path


def test_dashboard_lists_titles_with_state_attributes(client, seeded):
    html = text(client.get("/"))
    row = re.search(r'<tr class="item-row[^>]*data-id="%d"[^>]*>' % seeded, html).group(0)
    assert 'data-kind="show"' in row and 'data-state="partial"' in row and f'data-url="/show/{seeded}"' in row
    assert 'data-users="|alice|"' in row and 'data-user-count="1"' in row
    assert "1 of 6 episodes" in html or "1 of 6" in html


def _app_config(page):
    return json.loads(re.search(r'<script type="application/json" id="jellyfin-app-config">(.*?)</script>', page, re.S).group(1))


def test_pages_carry_what_the_browser_needs_to_open_the_jellyfin_app(client, v2):
    with v2.connection() as conn:
        conn.execute("INSERT INTO profile (name, letterboxd_username, jellyfin_user_id) VALUES ('Bob', 'bob', 'bbb222')")
        conn.execute("INSERT INTO profile (name, jellyfin_user_id) VALUES ('alice', 'aaa111')")
        conn.execute("INSERT INTO profile (name, letterboxd_username) VALUES ('No Jellyfin', 'nj')")
    page = text(client.get("/"))
    assert _app_config(page) == {"server_id": "", "users": [{"id": "aaa111", "name": "alice"}, {"id": "bbb222", "name": "Bob"}]}
    assert 'id="jellyfinUserModal"' not in page                                # no server ID yet (no Jellyfin sync): no chooser
    settings.set_value("jellyfin_server_id", "srv123")
    page = text(client.get("/"))
    assert _app_config(page)["server_id"] == "srv123" and 'id="jellyfinUserModal"' in page and 'js/jellyfin-app.js' in page


def test_jellyfin_links_carry_the_item_id_not_a_url(client, v2, seeded):
    with v2.connection() as conn:
        mid = conn.execute("INSERT INTO media (kind, title, tmdb_id, year) VALUES ('movie', 'Heat', '949', 1995)").lastrowid
        conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES ('jfheat', 'movie', 'a', 'a')")
        conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', 'jfheat', 'x', 1.0)", (mid,))
        conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (mid,))
    settings.set_value("jellyfin_url", "http://jf:8096")
    movie_page = text(client.get(f"/movie/{mid}"))
    assert re.search(r'<a href="http://jf:8096/web/index.html#/details\?id=jfheat"[^>]*data-jellyfin-item="jfheat"', movie_page)
    row = re.search(r'<tr class="item-row[^>]*data-id="%d"[^>]*>' % mid, text(client.get("/"))).group(0)
    assert 'data-jellyfin-item="jfheat"' in row and "jellyfin://" not in row and "data-app-link" not in row


def test_show_page_lists_seasons_with_specials_last_and_labelled(client, seeded):
    html = text(client.get(f"/show/{seeded}"))
    labels = re.findall(r'<span>(Season \d+|Specials \(Season 0\))</span>', html)
    assert labels == ["Season 1", "Season 3", "Specials (Season 0)"]
    assert "Request season" in html and "1 of 6 aired episodes in the library" in html
    special_block = html[html.index("collapse0"):]
    assert 'data-bs-parent' not in special_block.split(">")[0]          # opens independently of the regular seasons


def test_season_api_returns_states_for_every_episode_including_unaired(client, seeded):
    data = client.get(f"/api/show/{seeded}/season/3").get_json()
    assert data["label"] == "Season 3"
    by_ep = {e["episode_number"]: e for e in data["episodes"]}
    assert {n: e["state"] for n, e in by_ep.items()} == {9: "downloaded", 10: "in_library", 11: "downloading", 12: "not_aired"}
    assert by_ep[11]["progress_pct"] == 75 and by_ep[11]["state_label"] == "Downloading"
    assert by_ep[12]["can_request"] is False and by_ep[10]["can_request"] is False
    specials = client.get(f"/api/show/{seeded}/season/0").get_json()
    assert specials["label"] == "Specials (Season 0)" and [e["state"] for e in specials["episodes"]] == ["in_library"]


def test_movie_page_and_wrong_kind_redirects(client, v2, seeded):
    with v2.connection() as conn:
        mid = conn.execute("INSERT INTO media (kind, title, tmdb_id, year) VALUES ('movie', 'Heat', '949', 1995)").lastrowid
    html = text(client.get(f"/movie/{mid}"))
    assert "Heat" in html and "Request in Radarr" in html and "MOVIE" in html
    assert client.get(f"/show/{mid}").status_code == 302 and client.get(f"/movie/{seeded}").status_code == 302 and client.get("/movie/9999").status_code == 302


def test_poster_route(client, v2, tmp_path, monkeypatch):
    from synchub import posters
    monkeypatch.setattr(posters, "POSTER_DIR_OVERRIDE", tmp_path / "posters")
    with v2.connection() as conn:
        mid = conn.execute("INSERT INTO media (kind, title, tmdb_id, poster_path) VALUES ('movie', 'M', '1', '/p.jpg')").lastrowid
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: type("R", (), {"status_code": 200, "content": b"x" * 4000})())
    response = client.get(f"/poster/{mid}")
    assert response.status_code == 200 and len(response.data) == 4000
    monkeypatch.setattr(posters.requests, "get", lambda url, **kw: type("R", (), {"status_code": 404, "content": b""})())
    with v2.connection() as conn:
        other = conn.execute("INSERT INTO media (kind, title, tmdb_id, poster_path) VALUES ('movie', 'N', '2', '/q.jpg')").lastrowid
    assert client.get(f"/poster/{other}").status_code == 404


# ---------------------------------------------------------------- tasks
def test_trigger_starts_the_job_and_rejects_unknown_names(client, monkeypatch):
    started = []
    monkeypatch.setattr(tasks, "start_async", lambda fn: started.append(fn))
    r = client.post("/tools/trigger/radarr", follow_redirects=True)
    assert "Radarr sync started." in text(r) and len(started) == 1
    assert "Unknown task." in text(client.post("/tools/trigger/nope", follow_redirects=True)) and len(started) == 1


def test_api_tasks_lists_every_task(client, v2):
    tasks.set_status("radarr", "completed", "ok")
    data = client.get("/api/tasks").get_json()["tasks"]
    assert set(data) == set(tasks.TASK_NAMES) and data["radarr"]["message"] == "ok"


# ---------------------------------------------------------------- settings and profiles
def test_settings_validation_through_the_form(client, v2):
    r = client.post("/tools/update-settings", data={"radarr_interval_hours": "abc", "sonarr_interval_hours": "8", "tmdb_api_key": "KEYKEYKEY"}, follow_redirects=True)
    assert "Some values were not saved" in text(r) and "radarr_interval_hours" in text(r)
    assert settings.get("sonarr_interval_hours") == "8" and settings.get("tmdb_api_key") == "KEYKEYKEY"
    assert "KEYKEYKEY" not in text(client.get("/settings"))             # secrets are never echoed back


def test_profile_crud_and_duplicate_identities(client, v2):
    client.post("/settings/profiles/save", data={"profile_name": "Alice", "letterboxd_username": "alice", "serializd_username": "", "jellyfin_user_id": ""})
    r = client.post("/settings/profiles/save", data={"profile_name": "Bob", "letterboxd_username": "ALICE", "serializd_username": "", "jellyfin_user_id": ""}, follow_redirects=True)
    assert "already belongs to the profile 'Alice'" in htmllib.unescape(text(r))
    with v2.connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM profile")]
    assert [p["name"] for p in rows] == ["Alice"]
    pid = rows[0]["id"]
    client.post("/settings/profiles/save", data={"profile_id": pid, "profile_name": "Alice B", "letterboxd_username": "alice", "serializd_username": "alice_sz", "jellyfin_user_id": ""})
    assert "Alice B" in text(client.get("/settings")) and "alice_sz" in text(client.get("/settings"))
    assert client.post("/api/profiles/toggle", json={"id": pid, "is_active": 0}).get_json() == {"ok": True}
    assert client.post("/api/profiles/toggle", json={"id": 999, "is_active": 0}).status_code == 404
    assert client.post("/api/profiles/toggle", json={"nope": 1}).status_code == 400
    client.post(f"/settings/profiles/delete/{pid}")
    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] == 0


def test_jellyfin_import_only_sets_name_and_jellyfin_id(client, v2):
    client.post("/settings/profiles/save", data={"profile_name": "Alice", "letterboxd_username": "alice", "serializd_username": "", "jellyfin_user_id": ""})
    with v2.connection() as conn:
        pid = conn.execute("SELECT id FROM profile").fetchone()[0]
    client.post("/settings/profiles/save", data={"profile_id": pid, "profile_name": "Alice", "jellyfin_user_id": "jf-1"})     # import-style: no username fields
    with v2.connection() as conn:
        row = conn.execute("SELECT * FROM profile").fetchone()
    assert (row["letterboxd_username"], row["jellyfin_user_id"]) == ("alice", "jf-1")


# ---------------------------------------------------------------- requests
class FakeSonarrWeb(SonarrSetup):
    def __init__(self):
        self.calls = []

    def lookup(self, tvdb): return {"title": "S", "titleSlug": "s", "tvdbId": int(tvdb), "seasons": []}
    def add_series(self, *a, **k): self.calls.append(("add", k.get("monitored_seasons")))


def test_request_endpoints_report_clear_errors(client, seeded, monkeypatch):
    refreshed = []
    monkeypatch.setattr(tasks, "refresh_soon", lambda service, job: refreshed.append(service))
    import synchub.web.api as api
    from synchub.clients import ConfigError
    monkeypatch.setattr(api, "SonarrClient", lambda: (_ for _ in ()).throw(ConfigError("Sonarr URL or API key is not configured.")))
    r = client.post(f"/api/request/show/{seeded}/seasons", json={"seasons": [1]})
    assert r.status_code == 400 and r.get_json() == {"ok": False, "message": "Sonarr URL or API key is not configured."}
    assert client.post(f"/api/request/show/{seeded}/seasons", json={"seasons": "x"}).status_code == 400
    assert client.post(f"/api/request/show/{seeded}/episodes", json={}).status_code == 400
    assert refreshed == []


def test_request_season_success_triggers_a_refresh(client, seeded, v2, monkeypatch):
    import synchub.web.api as api
    refreshed = []
    monkeypatch.setattr(tasks, "refresh_soon", lambda service, job: refreshed.append(service))
    sonarr = e2e.FakeSonarr([], {})
    sonarr.lookup = lambda tvdb: {"title": "S", "titleSlug": "s", "tvdbId": int(tvdb), "seasons": []}
    sonarr.added = []
    sonarr.get_series = lambda sid: {"id": 10, "seasons": [{"seasonNumber": 1}, {"seasonNumber": 3}]}
    sonarr.update_series = lambda sid, payload: None
    searched = []
    sonarr.search_season = lambda sid, season: searched.append(season)
    monkeypatch.setattr(api, "SonarrClient", lambda: sonarr)
    r = client.post(f"/api/request/show/{seeded}/seasons", json={"seasons": [1]})
    assert r.get_json() == {"ok": True, "message": "Requested season(s) 1 in Sonarr."} and searched == [1] and refreshed == ["sonarr"]


def test_request_movie_upstream_error_is_reported_to_the_user(client, v2, monkeypatch):
    import synchub.web.api as api
    with v2.connection() as conn:
        mid = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'M', '5')").lastrowid

    class Radarr(ServarrSetup):
        def lookup(self, tmdb): raise ApiError("Radarr LookupMovie failed: HTTP 500")
    monkeypatch.setattr(api, "RadarrClient", lambda: Radarr())
    r = client.post(f"/api/request/movie/{mid}")
    assert r.status_code == 400 and r.get_json()["ok"] is False and "HTTP 500" in r.get_json()["message"]


def test_refresh_endpoint(client, v2, monkeypatch):
    with v2.connection() as conn:
        mid = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'S', '100')").lastrowid
    monkeypatch.setattr(tasks, "refresh_media", lambda media_id: f"Refreshed {media_id}")
    assert client.post(f"/api/show/{mid}/refresh").get_json() == {"ok": True, "message": f"Refreshed {mid}"}
    monkeypatch.setattr(tasks, "refresh_media", lambda media_id: (_ for _ in ()).throw(LookupError("x")))
    assert client.post("/api/show/5/refresh").status_code == 400


# ---------------------------------------------------------------- resolution
def test_resolution_flow(client, v2, monkeypatch):
    monkeypatch.setattr(tasks, "start_async", lambda fn: None)
    with v2.connection() as conn:
        conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, title, year, first_seen, last_seen) VALUES ('alice', 'obscure', 'watchlist', 'Obscure', 2001, 'a', 'a')")
        conn.execute("INSERT INTO src_letterboxd_entry (username, slug, list, title, year, first_seen, last_seen) VALUES ('alice', 'other', 'watchlist', 'Other', 2002, 'a', 'a')")
        conn.execute("INSERT INTO match_review (source, source_key, source_title, source_year, kind, candidate_tmdb_id, confidence, reason) VALUES ('letterboxd', 'obscure', 'Obscure', 2001, 'movie', '11', 0.85, 'search')")
    html = text(client.get("/resolution"))
    assert "Obscure" in html and "85% sure" in html and "Other" in html
    with v2.connection() as conn:
        rid = conn.execute("SELECT id FROM match_review").fetchone()[0]
    assert client.post("/api/resolution/approve", json={"review_id": rid}).get_json()["ok"]
    assert client.post("/api/resolution/approve", json={"review_id": rid}).status_code == 400            # already decided
    r = client.post("/api/resolution/link", json={"source": "letterboxd", "key": "other", "kind": "movie", "tmdb_id": "22"})
    assert r.get_json()["ok"]
    with v2.connection() as conn:
        got = {r["source_key"]: r["entity_id"] for r in conn.execute("SELECT * FROM link")}
        assert set(got) == {"obscure", "other"} and conn.execute("SELECT title FROM media WHERE tmdb_id = '22'").fetchone()[0] == "Other"
    assert "Everything is matched" in text(client.get("/resolution"))
    assert client.post("/api/resolution/link", json={"source": "letterboxd", "key": "x", "kind": "movie", "tmdb_id": "abc"}).status_code == 400
    assert client.post("/api/resolution/link", json={"source": "bogus", "key": "x", "kind": "movie", "tmdb_id": "1"}).status_code == 400


def test_dismiss(client, v2):
    with v2.connection() as conn:
        conn.execute("INSERT INTO match_review (source, source_key, source_title, kind, candidate_tmdb_id, confidence, reason) VALUES ('letterboxd', 'x', 'X', 'movie', '1', 0.7, 'y')")
        rid = conn.execute("SELECT id FROM match_review").fetchone()[0]
    assert client.post("/api/resolution/dismiss", json={"review_id": rid}).get_json()["ok"]
    assert client.post("/api/resolution/dismiss", json={"review_id": rid}).status_code == 400


def _suggest(conn, key, title, tmdb, confidence):
    conn.execute("INSERT INTO match_review (source, source_key, source_title, source_year, kind, candidate_tmdb_id, confidence, reason) "
                 "VALUES ('letterboxd', ?, ?, 2000, 'movie', ?, ?, 'search')", (key, title, str(tmdb), confidence))


def _suggestion_ids(conn):
    return {(r["source_key"], r["candidate_tmdb_id"]): r["id"] for r in conn.execute("SELECT * FROM match_review")}


def test_approve_all_takes_clear_winners_and_leaves_ties_and_unlisted(client, v2, monkeypatch):
    started = []
    monkeypatch.setattr(tasks, "start_async", lambda fn: started.append(fn))
    with v2.connection() as conn:
        _suggest(conn, "clear", "Clear", 1, 0.9)
        _suggest(conn, "clear", "Clear", 2, 0.8)
        _suggest(conn, "tie", "Tie", 3, 0.85)
        _suggest(conn, "tie", "Tie", 4, 0.85)
        _suggest(conn, "solo", "Solo", 5, 0.7)
        _suggest(conn, "unlisted", "Unlisted", 6, 0.9)
        ids = _suggestion_ids(conn)
    sent = [ids[("clear", "1")], ids[("tie", "3")], ids[("solo", "5")], 99999]
    body = client.post("/api/resolution/approve-all", json={"review_ids": sent}).get_json()
    assert body["ok"] and body["approved"] == [ids[("clear", "1")], ids[("solo", "5")]]
    assert body["skipped"] == [ids[("tie", "3")]] and body["stale"] == [99999] and body["failed"] == []
    assert "Approved 2." in body["message"] and "equally likely" in body["message"]
    with v2.connection() as conn:
        linked = {r["source_key"]: r["tmdb_id"] for r in conn.execute("SELECT l.source_key, m.tmdb_id FROM link l JOIN media m ON m.id = l.entity_id")}
        status = {k: conn.execute("SELECT status FROM match_review WHERE id = ?", (i,)).fetchone()[0] for k, i in ids.items()}
    assert linked == {"clear": "1", "solo": "5"}                       # the tie and the title that was not listed stay unlinked
    assert status[("clear", "1")] == "approved" and status[("clear", "2")] == "dismissed"
    assert status[("tie", "3")] == status[("tie", "4")] == status[("unlisted", "6")] == "pending"
    assert len(started) == 1                                             # one refresh for the whole batch


def test_approve_all_isolates_a_failing_title_and_skips_the_refresh_when_nothing_changed(client, v2, monkeypatch):
    from synchub import matching
    started = []
    monkeypatch.setattr(tasks, "start_async", lambda fn: started.append(fn))
    with v2.connection() as conn:
        _suggest(conn, "bad", "Bad", 1, 0.9)
        _suggest(conn, "good", "Good", 2, 0.9)
        ids = _suggestion_ids(conn)
    real = matching.approve_review

    def flaky(conn, review_id):
        if review_id == ids[("bad", "1")]:
            conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', 'half done', '999')")     # must be rolled back
            raise RuntimeError("boom")
        return real(conn, review_id)

    monkeypatch.setattr(matching, "approve_review", flaky)
    body = client.post("/api/resolution/approve-all", json={"review_ids": list(ids.values())}).get_json()
    assert body["approved"] == [ids[("good", "2")]] and body["failed"] == [ids[("bad", "1")]] and "could not be linked" in body["message"]
    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media WHERE tmdb_id = '999'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM match_review WHERE id = ?", (ids[("bad", "1")],)).fetchone()[0] == "pending"
    started.clear()
    body = client.post("/api/resolution/approve-all", json={"review_ids": [ids[("good", "2")]]}).get_json()
    assert body["approved"] == [] and body["stale"] == [ids[("good", "2")]] and started == []


def test_approve_all_rejects_a_bad_body(client):
    for bad in (None, {}, {"review_ids": "1,2"}, {"review_ids": [1, "2"]}):
        assert client.post("/api/resolution/approve-all", json=bad).status_code == 400


def test_resolution_page_shows_approve_all_only_when_there_is_something_to_approve(client, v2):
    def button(page):
        return re.search(r'<button[^>]*id="approve-all"[^>]*>', page).group(0)

    assert "d-none" in button(text(client.get("/resolution")))
    with v2.connection() as conn:
        _suggest(conn, "a", "A", 1, 0.9)
        _suggest(conn, "b", "B", 2, 0.9)
        _suggest(conn, "b", "B", 3, 0.8)
    page = text(client.get("/resolution"))
    assert "d-none" not in button(page) and "Approve all (<span data-review-count>2</span>)" in page
    assert len(re.findall(r'<div class="col" data-review-card>', page)) == 2 and len(re.findall(r"data-candidate>", page)) == 3


# ---------------------------------------------------------------- connection tests
def test_connection_test_never_sends_the_saved_key_to_a_different_url(client, v2, monkeypatch):
    settings.set_value("radarr_url", "http://radarr:7878")
    settings.set_value("radarr_api_key", "SAVEDKEY")
    import synchub.web.api as api
    made = []
    monkeypatch.setattr(api, "RadarrClient", lambda url, key: made.append((url, key)) or type("C", (ServarrSetup,), {"status": lambda self: {"appName": "Radarr", "version": "5"}})())
    r = client.post("/api/test-connection/radarr", json={"fields": {"radarr_url": "http://evil.example", "radarr_api_key": ""}})
    assert "different URL" in r.get_json()["message"] and made == []
    ok = client.post("/api/test-connection/radarr", json={"fields": {"radarr_url": "", "radarr_api_key": ""}}).get_json()
    assert ok == {"ok": True, "message": "Connected to Radarr 5. The quality profile and root folder are valid."} and made == [("http://radarr:7878", "SAVEDKEY")]
    typed = client.post("/api/test-connection/radarr", json={"fields": {"radarr_url": "http://other:7878", "radarr_api_key": "NEWKEY"}}).get_json()
    assert typed["ok"] and made[-1] == ("http://other:7878", "NEWKEY")
    assert client.post("/api/test-connection/bogus", json={}).status_code == 404


def test_connection_test_also_checks_the_chosen_profile_and_root_folder(client, v2, monkeypatch):
    settings.set_value("radarr_url", "http://radarr:7878")
    settings.set_value("radarr_api_key", "SAVEDKEY")
    import synchub.web.api as api
    monkeypatch.setattr(api, "RadarrClient", lambda url, key: type("C", (ServarrSetup,), {"status": lambda self: {"appName": "Radarr", "version": "5"}})())

    def test(**fields):
        return client.post("/api/test-connection/radarr", json={"fields": {"radarr_url": "", "radarr_api_key": "", **fields}}).get_json()

    bad_folder = test(radarr_quality_profile_id="4", radarr_root_folder="/movies/nope")
    assert bad_folder["ok"] is False and bad_folder["message"].startswith("Connected to Radarr 5, but adding titles would fail:")
    assert "root folder '/movies/nope'" in bad_folder["message"] and "/movies, /films" in bad_folder["message"]
    bad_profile = test(radarr_quality_profile_id="9", radarr_root_folder="/films")
    assert bad_profile["ok"] is False and "no quality profile with ID 9" in bad_profile["message"]
    assert test(radarr_quality_profile_id="4", radarr_root_folder="/films")["ok"] is True       # what is typed in the form is what is checked
    assert test()["ok"] is True                                                                   # nothing typed: the saved defaults (1 and /movies) exist


def test_servarr_lists_refuse_saved_key_for_a_different_url(client, v2):
    settings.set_value("sonarr_url", "http://sonarr:8989")
    settings.set_value("sonarr_api_key", "SAVEDKEY")
    r = client.post("/api/servarr/sonarr/quality-profiles", json={"url": "http://evil.example", "api_key": ""})
    assert r.status_code == 400 and "different URL" in r.get_json()["error"]
    assert client.post("/api/servarr/other/root-folders", json={}).status_code == 404


# ---------------------------------------------------------------- webhooks, logs, payloads, export, ddns
def test_webhook_triggers_a_coalesced_refresh_and_announces_downloads(client, v2, monkeypatch):
    import synchub.web.api as api
    refreshed, heard = [], api.announcer.listen()
    monkeypatch.setattr(tasks, "refresh_soon", lambda service, job: refreshed.append(service))
    r = client.post("/api/webhook/radarr", json={"eventType": "Grab", "movie": {"tmdbId": 949}})
    assert r.get_json() == {"status": "received", "service": "radarr"} and refreshed == ["radarr"]
    message = json.loads(heard.get_nowait().split("data: ")[1])
    assert message == {"service": "radarr", "event": "Grab", "tmdb_id": 949, "tvdb_id": None, "queue_status": "Downloading"}
    for body in (None, [1, 2], {"series": None, "eventType": "Download"}):
        assert client.post("/api/webhook/sonarr", json=body).status_code == 200
    assert client.post("/api/webhook/jellyfin", json={"NotificationType": "ItemAdded"}).status_code == 200
    assert refreshed.count("sonarr") == 3 and "jellyfin" in refreshed
    api.announcer.forget(heard)


def test_logs_endpoint_redacts_secrets(client, v2, tmp_path, monkeypatch):
    import synchub.web.admin as admin
    log = tmp_path / "app.log"
    log.write_text("line one\nGET /x?api_key=abc123456&x=1\ncalled with SECRETVALUE99 inside\n")
    monkeypatch.setattr(admin, "LOG_FILE_PATH", log)
    settings.set_value("tmdb_api_key", "SECRETVALUE99")
    lines = client.get("/api/logs?tail=10").get_json()["logs"]
    assert lines == ["line one", "GET /x?api_key=***&x=1", "called with *** inside"]
    assert client.get("/api/logs?tail=1").get_json()["logs"] == ["called with *** inside"]


def test_payload_log_endpoints(client, v2):
    with v2.connection() as conn:
        for code in (200, 500, 0):
            conn.execute("INSERT INTO api_payload_log (direction, service, event_type, status_code) VALUES ('OUT', 'radarr', 'x', ?)", (code,))
        conn.execute("INSERT INTO api_payload_log (direction, service, event_type, status_code) VALUES ('IN', 'sonarr', 'y', 200)")
    assert len(client.get("/api/payload-logs").get_json()) == 4
    assert len(client.get("/api/payload-logs?service=sonarr").get_json()) == 1
    assert [r["status_code"] for r in client.get("/api/payload-logs?errors_only=1").get_json()] == [0, 500]
    assert len(client.get("/api/payload-logs?limit=abc").get_json()) == 4
    assert client.post("/api/payload-logs/clear").get_json() == {"ok": True} and client.get("/api/payload-logs").get_json() == []


def test_csv_export_neutralises_formulas(client, v2, seeded):
    with v2.connection() as conn:
        conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('movie', '=HYPERLINK(\"http://x\")', '77')")
    body = text(client.get("/tools/export-csv"))
    lines = body.splitlines()
    assert lines[0].startswith("title,year,type,state") and any(l.startswith("The Last Ship,") and ",partial," in l for l in lines)
    assert "'=HYPERLINK" in body and ",=HYPERLINK" not in body and not any(l.startswith("=") for l in lines)


def test_ddns_page_and_actions(client, v2, monkeypatch):
    from synchub import ddns
    settings.set_value("dynu_api_key", "KEEPME")
    monkeypatch.setattr(ddns, "fetch_metadata", lambda domain, key: None)        # never touch the network from a test
    client.post("/ddns/update-settings", data={"dynu_hostname": "home.dynu.net", "dynu_api_key": "", "dynu_enabled": "1"})
    assert settings.get("dynu_api_key") == "KEEPME" and settings.get("dynu_domain") == "home.dynu.net"
    monkeypatch.setattr(ddns, "run_update", lambda force=False: (True, "Updated."))
    assert "Updated." in text(client.post("/ddns/trigger-update", follow_redirects=True))
    assert "KEEPME" not in text(client.get("/ddns"))


def test_scheduler_is_reconfigured_when_settings_change(client, v2):
    from synchub import scheduler
    client.post("/tools/update-settings", data={"radarr_interval_hours": "0", "sonarr_interval_hours": "3"})
    ids = {j.id: j for j in scheduler.scheduler.get_jobs()}
    assert "job_radarr" not in ids and ids["job_sonarr"].trigger.interval.total_seconds() == 3 * 3600
