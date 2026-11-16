"""The dashboard keeps the counts; the health line and file breakdown live on System > Library status; SyncHub storage tracks what the data folder holds;
the TV morning screen has a What's new strip."""
import json
import os
import re
import socketserver
import tempfile
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler

import pytest

from synchub import catalog, data_usage, dbinfo, settings, watch
from synchub.dashboard import data as dash_data
from synchub.ingest import jellyfin as jf

from .test_date_added import movie
from .test_ingest_jellyfin import FakeJellyfin, episode as jf_episode, season, series

USER = "aaaabbbbccccdddd1111222233334444"


def media(conn, kind, title, tmdb, year=2000):
    return conn.execute("INSERT INTO media (kind, title, tmdb_id, year) VALUES (?, ?, ?, ?)", (kind, title, tmdb, year)).lastrowid


def in_jellyfin(conn, media_id, item):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('media', ?, 'jellyfin', ?, 'x', 1.0)", (media_id, item))


def library_file(conn):
    """One film that is in the library, so there are counts and a file to break down."""
    film = media(conn, "movie", "Heat", "10", 1995)
    in_jellyfin(conn, film, "m1")
    jf.sync(conn, FakeJellyfin([movie("m1", "Heat", "2026-10-21T18:04:11Z")]), "movies", now="2026-11-01 00:00:00")
    conn.execute("INSERT INTO item_state (target_type, target_id, state_code) VALUES ('movie', ?, 'in_library')", (film,))
    conn.commit()
    return film


# ---------------------------------------------------------------- dashboard and Library status
def test_the_dashboard_keeps_the_counts_and_the_breakdown_moves_to_library_status(client, v2, conn):
    library_file(conn)
    settings.set_value("jellyfin_url", "http://jf.test")
    dash = client.get("/").get_data(as_text=True)
    assert "Library at a glance" in dash and "In Library" in dash
    assert 'id="fileBreakdown"' not in dash and "Jellyfin movies:" not in dash and 'id="libraryHealth"' not in dash
    page = client.get("/system/library")
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "Library status" in text and "Jellyfin movies: 1 item = 1 title here" in text
    assert 'id="libraryHealth"' in text and 'id="libraryCounts"' in text


def test_library_status_links_back_to_the_dashboard_with_a_status_chosen(client, v2, conn):
    film = library_file(conn)
    in_jellyfin(conn, film, "m2")                                       # a second Jellyfin item of the same film
    jf.sync(conn, FakeJellyfin([movie("m1", "Heat", "2026-10-21T18:04:11Z"), movie("m2", "Heat", "2026-10-22T18:04:11Z")]), "movies", now="2026-11-02 00:00:00")
    conn.commit()
    text = client.get("/system/library").get_data(as_text=True)
    assert "/?status=extra_versions" in text or "status=extra_versions" in text


def test_the_health_dot_is_in_the_top_bar_of_every_page_for_someone_who_can_read_the_logs(client, v2):
    for path in ("/", "/system/library", "/discover"):
        text = client.get(path).get_data(as_text=True)
        assert 'id="healthDot"' in text and 'href="/system/status"' in text and "All systems OK" in text


def test_a_guest_without_the_logs_permission_gets_neither_the_pages_nor_the_dot(app, conn):
    conn.execute("INSERT INTO profile (name, permissions, pin_hash) VALUES ('Kid', 'dashboard', 'x')")
    conn.commit()
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "0")
    settings.set_value("access_guest_permissions", "dashboard")
    assert browser.get("/system/library").status_code == 403 and browser.get("/system/storage").status_code == 403
    assert browser.get("/api/system/storage/docker").status_code == 403
    assert 'id="healthDot"' not in browser.get("/").get_data(as_text=True)


# ---------------------------------------------------------------- the watchlist row and the sorts
def test_the_dashboard_shows_my_watchlist_split_into_ready_and_missing(app, v2, conn):
    settings.set_value("jellyfin_url", "http://jf.test")
    ready, absent = library_file(conn), media(conn, "movie", "Alien", "11", 1979)
    pid = conn.execute("INSERT INTO profile (name, jellyfin_user_id) VALUES ('Ana', ?)", (USER,)).lastrowid
    for m in (ready, absent):
        conn.execute("INSERT INTO watchlist_item (profile_id, media_id, source, added_at) VALUES (?, ?, 'manual', '2026-10-20 10:00:00')", (pid, m))
    conn.commit()
    ana = app.test_client()
    ana.set_cookie("synchub_profile", str(pid))
    page = ana.get("/").get_data(as_text=True)
    assert 'id="myWatchlist"' in page
    assert re.search(r'data-watchlist-group="ready".*?data-watchlist-item="%d"' % ready, page, re.S)
    assert re.search(r'data-watchlist-group="missing".*?data-watchlist-item="%d"' % absent, page, re.S)
    guest = app.test_client()
    guest.set_cookie("synchub_profile", "0")
    assert 'id="myWatchlist"' not in guest.get("/").get_data(as_text=True)


def test_an_empty_watchlist_shows_no_row(app, v2, conn):
    library_file(conn)
    pid = conn.execute("INSERT INTO profile (name) VALUES ('Ana')").lastrowid
    conn.commit()
    ana = app.test_client()
    ana.set_cookie("synchub_profile", str(pid))
    assert 'id="myWatchlist"' not in ana.get("/").get_data(as_text=True)


def test_the_sort_box_offers_the_six_orders_and_rows_carry_what_they_sort_on(client, v2, conn):
    film = library_file(conn)
    conn.execute("INSERT INTO src_jellyfin_userdata (jellyfin_user_id, item_id, played, last_played_at, first_seen, last_seen) VALUES (?, 'm1', 1, '2026-10-30 20:00:00', 'x', 'x')", (USER,))
    conn.commit()
    page = client.get("/").get_data(as_text=True)
    sort = re.search(r'<select id="sortOrder".*?</select>', page, re.S).group(0)
    assert re.findall(r'<option value="([^"]+)">([^<]+)</option>', sort) == [
        ("users-desc", "Most wanted"), ("title-asc", "Title (A-Z)"), ("year-desc", "Year (newest)"), ("status-asc", "Status"), ("added-desc", "Recently added"), ("watched-desc", "Recently watched")]
    row = re.search(r'<tr class="item-row[^>]*data-id="%d"[^>]*>' % film, page).group(0)
    assert 'data-watched="2026-10-30 20:00:00"' in row and "data-state-rank=" in row
    js = open("static/js/dashboard.js").read()
    assert "watched-desc" in js and "status-asc" in js


def test_last_played_is_per_user_or_for_everyone_and_a_show_is_as_recent_as_its_latest_episode(conn):
    film, show = media(conn, "movie", "Heat", "10"), media(conn, "show", "Show", "20")
    in_jellyfin(conn, film, "m1")
    eid = conn.execute("INSERT INTO episode (media_id, season_number, episode_number) VALUES (?, 1, 1)", (show,)).lastrowid
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES ('episode', ?, 'jellyfin_episode', 'e1', 'x', 1.0)", (eid,))
    for user, item, at in ((USER, "m1", "2026-10-01 10:00:00"), ("other", "m1", "2026-10-05 10:00:00"), (USER, "e1", "2026-10-09 10:00:00")):
        conn.execute("INSERT INTO src_jellyfin_userdata (jellyfin_user_id, item_id, played, last_played_at, first_seen, last_seen) VALUES (?, ?, 1, ?, 'x', 'x')", (user, item, at))
    conn.commit()
    assert watch.last_played(conn, USER) == {film: "2026-10-01 10:00:00", show: "2026-10-09 10:00:00"}
    assert watch.last_played(conn) == {film: "2026-10-05 10:00:00", show: "2026-10-09 10:00:00"}


# ---------------------------------------------------------------- SyncHub storage
def put(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """The data folder the storage numbers are measured in: database beside previews, backups, logs and posters."""
    from synchub import db
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "synchub_test.db"))
    db.migrate()
    for name, folder in (("DATA_DIR", tmp_path), ("LOGS_DIR", tmp_path / "logs"), ("POSTER_DIR", tmp_path / "posters"), ("BACKUP_DIR", tmp_path / "backups")):
        monkeypatch.setattr(data_usage, name, folder)
    return tmp_path


def test_measure_adds_up_each_part_of_the_data_folder(data_dir, v2):
    put(data_dir / "previews/7/encoded.mp4", 3000)
    put(data_dir / "previews/7/source.mp4", 2000)
    put(data_dir / "backups/synchub-20261101-033000.db", 4000)
    put(data_dir / "backups/synchub-20261101-033000.tv.zip", 500)
    put(data_dir / "logs/app.log", 700)
    put(data_dir / "posters/1.jpg", 300)
    put(data_dir / "tv/devices.json", 100)
    put(data_dir / "elsewhere/notes.txt", 50)
    with v2.connection() as conn:
        got = data_usage.measure(conn, force=True)
    assert got["previews"]["bytes"] == 5000 and got["previews"]["count"] == 1
    assert got["backups"]["bytes"] == 4500 and got["backups"]["count"] == 1 and got["backups"]["newest_bytes"] == 4000
    assert (got["logs_bytes"], got["posters_bytes"], got["tv_bytes"]) == (700, 300, 100)
    assert got["database"]["file"] > 0 and got["data_bytes"] >= got["database"]["total"] + 5000 + 4500 + 700 + 300 + 100 + 50
    assert got["other_bytes"] >= 50 and got["volume"]["free"] > 0


def test_measure_includes_what_each_worker_reports_for_its_work_folder(data_dir, v2, conn):
    wid = conn.execute("INSERT INTO worker (name) VALUES ('wtwo')").lastrowid
    gone = conn.execute("INSERT INTO worker (name, revoked_at) VALUES ('old', '2026-11-01 00:00:00')").lastrowid
    conn.commit()
    settings.set_value(f"worker_usage:{wid}", json.dumps({"used_bytes": 9000, "jobs_bytes": 6000, "originals_bytes": 3000, "at": "2026-11-16T05:00:00Z"}))
    settings.set_value(f"worker_usage:{gone}", json.dumps({"used_bytes": 1}))
    with v2.connection() as c:
        got = data_usage.measure(c, force=True)
    assert [(w["name"], w["bytes"], w["jobs"], w["originals"]) for w in got["work"]] == [("wtwo", 9000, 6000, 3000)] and got["work_bytes"] == 9000


def test_one_reading_a_day_is_kept_90_days_and_growth_is_read_from_it(data_dir, v2):
    with v2.connection() as conn:
        base = date(2026, 11, 16)
        for back in (100, 91, 45, 1, 0):
            day = (base - timedelta(days=back)).isoformat()
            conn.execute("INSERT OR REPLACE INTO storage_history (day, data_bytes, free_bytes) VALUES (?, ?, ?)", (day, 1000 - back, 500 + back))
        reading = data_usage.measure(conn, force=True)
        data_usage.record(conn, base.isoformat(), reading)
        data_usage.record(conn, base.isoformat(), reading)                       # the same day twice is one row
        days = [r["day"] for r in conn.execute("SELECT day FROM storage_history ORDER BY day")]
    assert days == [(base - timedelta(days=n)).isoformat() for n in (45, 1, 0)]
    rows = [{"day": "2026-11-01", "data_bytes": 100}, {"day": "2026-11-11", "data_bytes": 350}]
    assert data_usage.growth(rows) == (250, 10) and data_usage.growth(rows[:1]) is None


def test_the_first_visit_of_the_day_takes_a_reading_and_a_chart_needs_two_days(data_dir, v2):
    with v2.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM storage_history").fetchone()[0] == 0
        data_usage.ensure_today(conn)
        data_usage.ensure_today(conn)
        assert conn.execute("SELECT COUNT(*) FROM storage_history").fetchone()[0] == 1
    assert data_usage.chart_points([{"day": "2026-11-01", "data_bytes": 5}], "data_bytes") == ""
    pts = data_usage.chart_points([{"day": "2026-11-01", "data_bytes": 5}, {"day": "2026-11-03", "data_bytes": 10}], "data_bytes", width=100, height=50, pad=0).split()
    assert pts == ["0.0,50.0", "100.0,0.0"]


def test_the_daily_job_is_scheduled_and_never_raises(data_dir, v2, monkeypatch):
    from synchub import scheduler
    scheduler.configure()
    assert scheduler.scheduler.get_job("job_storage_history") is not None
    monkeypatch.setattr(data_usage, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone")))
    data_usage.record_today()                                                      # logged, not raised


def test_the_storage_page_shows_every_part_and_the_menu_links_it(client, data_dir, v2):
    put(data_dir / "backups/synchub-20261101-033000.db", 4000)
    put(data_dir / "logs/app.log", 700)
    page = client.get("/system/storage")
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "SyncHub storage" in text
    for part in ("database", "previews", "backups", "logs", "posters", "tv"):
        assert f'data-part="{part}"' in text
    assert 'id="biggestTables"' in text and 'id="workFolders"' in text and "reading so far" in text
    assert "Biggest database tables" in text and 'href="/system/storage"' in text
    assert 'id="btnCompact"' in text and 'id="btnPreviews"' in text


def test_delete_old_previews_frees_the_clips_of_finished_ones_and_keeps_current_ones(client, data_dir, v2, conn):
    old = conn.execute("INSERT INTO preview (subject_type, subject_id, seconds, status, expires_at) VALUES ('library_file', 1, 45, 'ready', '2020-01-01 00:00:00')").lastrowid
    new = conn.execute("INSERT INTO preview (subject_type, subject_id, seconds, status, expires_at) VALUES ('library_file', 2, 45, 'ready', '2099-01-01 00:00:00')").lastrowid
    conn.commit()
    put(data_dir / f"previews/{old}/encoded.mp4", 3000)
    put(data_dir / f"previews/{new}/encoded.mp4", 1000)
    out = client.post("/api/tools/previews-clear", json={}).get_json()
    assert out["ok"] and out["cleared"] == 1 and out["freed_bytes"] == 3000 and "Deleted the clips of 1 old preview" in out["message"]
    assert not (data_dir / f"previews/{old}").exists() and (data_dir / f"previews/{new}/encoded.mp4").exists()
    assert client.post("/api/tools/previews-clear", json={}).get_json()["message"] == "No old preview clips to delete."


def test_human_sizes():
    assert [data_usage.human(n) for n in (0, 999, 1500, 2_500_000, 3_200_000_000, 4_100_000_000_000)] == ["0 B", "999 B", "1.5 KB", "2.5 MB", "3.2 GB", "4.1 TB"]


# ---------------------------------------------------------------- Docker, optional
def test_without_the_docker_socket_the_sizes_are_simply_left_out(client, tmp_path, monkeypatch):
    got = data_usage.docker_sizes(str(tmp_path / "missing.sock"))
    assert got["available"] is False and "not mounted" in got["reason"]
    monkeypatch.setattr(data_usage, "DOCKER_SOCKET", str(tmp_path / "missing.sock"))
    out = client.get("/api/system/storage/docker").get_json()
    assert out["ok"] and out["available"] is False


class _FakeDocker(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _FakeDocker.seen.append(self.path)
        body = {"/containers/abc123/json?size=1": {"Name": "/SyncHub", "Image": "sha256:img", "SizeRw": 12_000_000, "SizeRootFs": 900_000_000},
                "/images/sha256:img/json": {"RepoTags": ["ghcr.io/thebumgreg/synchub:1.9.2"], "Size": 888_000_000},
                "/images/json": [{"Size": 888_000_000}, {"Size": 112_000_000}]}.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body if body is not None else {}).encode())

    def log_message(self, *args):
        pass


class _UnixServer(socketserver.UnixStreamServer):
    def get_request(self):
        request, _ = super().get_request()
        return request, ("localhost", 0)


def test_with_the_socket_the_image_and_container_sizes_are_read_with_get_requests_only():
    folder = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(folder, "d.sock")
    server = _UnixServer(path, _FakeDocker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        got = data_usage.docker_sizes(path, hostname="abc123")
    finally:
        server.shutdown()
        server.server_close()
        os.remove(path)
        os.rmdir(folder)
    assert got == {"available": True, "container": "SyncHub", "image_tags": ["ghcr.io/thebumgreg/synchub:1.9.2"], "image_bytes": 888_000_000, "writable_bytes": 12_000_000,
                   "root_bytes": 900_000_000, "images_count": 2, "images_bytes": 1_000_000_000}
    assert len(_FakeDocker.seen) == 3


def test_the_unraid_template_offers_the_docker_socket_read_only_and_empty_by_default():
    import xml.etree.ElementTree as ET
    configs = {c.get("Target"): c for c in ET.parse("unraid/synchub.xml").getroot().iter("Config")}
    socket_line = configs["/var/run/docker.sock"]
    assert socket_line.get("Mode") == "ro" and socket_line.get("Required") == "false" and socket_line.get("Default") == "" and (socket_line.text or "") == ""


# ---------------------------------------------------------------- the TV What's new strip
def recent_library(conn, today):
    """A film added a few days ago, a show whose newest episode is recent, a film added months ago, and a film that Jellyfin no longer has."""
    films = {name: media(conn, "movie", name, str(i)) for i, name in enumerate(("Fresh", "Old", "Gone"), 100)}
    show = media(conn, "show", "Series", "200")
    for m, key in ((films["Fresh"], "m1"), (films["Old"], "m2"), (films["Gone"], "m3"), (show, "s1")):
        in_jellyfin(conn, m, key)
    fresh = (today - timedelta(days=3)).isoformat()
    jf.sync(conn, FakeJellyfin([movie("m1", "Fresh", f"{fresh}T10:00:00Z"), movie("m2", "Old", "2025-01-01T10:00:00Z"), movie("m3", "Gone", f"{fresh}T11:00:00Z"),
                                series("s1", "Series"), season("sea1", "s1", 1), jf_episode("e1", "s1", "sea1", 1, 1, DateCreated=f"{(today - timedelta(days=1)).isoformat()}T09:00:00Z")]),
            "all", now=f"{today.isoformat()} 00:00:00")
    conn.execute("UPDATE src_jellyfin_item SET removed_at = '2026-11-15 00:00:00' WHERE item_id = 'm3'")
    conn.commit()
    return films, show


def test_recent_titles_are_the_newest_within_the_window_and_skip_what_jellyfin_dropped(conn):
    today = date(2026, 11, 16)
    films, show = recent_library(conn, today)
    got = catalog.recently_added_titles(conn, limit=6, days=30, today=today.isoformat())
    assert [(t["title"], t["added"]) for t in got] == [("Series", "2026-11-15"), ("Fresh", "2026-11-13")]
    assert [t["title"] for t in catalog.recently_added_titles(conn, limit=1, days=30, today=today.isoformat())] == ["Series"]
    assert catalog.recently_added_titles(conn, limit=6, days=2, today=today.isoformat())[0]["title"] == "Series" and len(catalog.recently_added_titles(conn, limit=6, days=2, today=today.isoformat())) == 1


def test_the_tv_data_carries_the_strip_only_while_the_switch_is_on(v2, conn):
    recent_library(conn, date.today())
    assert settings.DEFAULTS["dash_new_on"] == "1"
    on = dash_data.whats_new()
    assert [t["title"] for t in on] == ["Series", "Fresh"] and on[0]["poster"].startswith("/poster/") and set(on[0]) == {"id", "title", "year", "kind", "poster", "added"}
    settings.set_value("dash_new_on", "0")
    assert dash_data.whats_new() == []


def test_the_tv_page_draws_the_strip_and_the_settings_page_has_the_switch(client, v2):
    from synchub.dashboard import config
    page = client.get(f"/dash/{config.token()}").get_data(as_text=True)
    assert 'id="new"' in page and "drawNew" in page and "What’s new" in page
    settings_page = client.get("/settings").get_data(as_text=True)
    assert 'name="dash_new_on"' in settings_page
    client.post("/tools/update-settings", data={"dash_new_on": ["0", "0"], "tab": "dashboard"})
    assert settings.get("dash_new_on") == "0"


def test_build_failing_to_read_the_titles_leaves_the_strip_out_and_the_rest_alone(v2, monkeypatch):
    monkeypatch.setattr(catalog, "recently_added_titles", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db busy")))
    assert dash_data.whats_new() == []
