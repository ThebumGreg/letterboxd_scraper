"""The UI batch: Options and Jobs tabs, the Approved-and-queued group, Remote in the menu, and the Sessions page."""
import re

from synchub import dvr, jobs, settings, workers
from synchub.clients.jellyfin import JellyfinClient
from synchub.library import recommend as rc
from synchub.sessions import describe, live
from tests.v2.test_access import menu


def text(response):
    return response.get_data(as_text=True)


# ---------------------------------------------------------------- Options and Jobs tabs
def test_the_options_tab_holds_the_settings_and_profiles_and_the_library_tab_does_not(client, v2):
    page = text(client.get("/library/options"))
    for marker in ('id="librarySettingsForm"', 'id="profileForm"', 'id="optMessage"'):
        assert page.count(marker) == 1
    library_pane = page.split('id="media-pane-library"')[1].split('id="media-pane-')[0]
    assert 'id="scanForm"' in library_pane and 'id="librarySettingsForm"' not in library_pane and 'id="profileForm"' not in library_pane
    assert re.search(r'class="nav-link active" id="media-tab-options"', page)


def test_the_tab_order_puts_jobs_next_to_workers(client, v2):
    page = text(client.get("/library"))
    assert re.findall(r'id="media-tab-(\w+)"', page) == ["library", "versions", "originals", "workers", "players", "jobs", "storage", "options"]


def test_the_jobs_tab_and_the_workers_fragment_are_separate(client, v2):
    with v2.connection() as conn:
        workers.create(conn, "encoder-1")
        jobs.create(conn, "library", title="Heat")
    assert 'id="jobTable"' in text(client.get("/jobs/fragment")) and 'id="workerTable"' not in text(client.get("/jobs/fragment"))
    assert 'id="workerTable"' in text(client.get("/workers/fragment")) and 'id="jobTable"' not in text(client.get("/workers/fragment"))
    assert text(client.get("/jobs")).count('class="nav-link active" id="media-tab-jobs"') == 1


# ---------------------------------------------------------------- Approved and queued is not Done
def record(**extra):
    base = {"migration_status": "pending", "approval_status": "pending", "root_name": "TFiles", "file_name": "Heat (1995).mkv", "relative_path": "Heat (1995).mkv"}
    return {**base, **extra}


def test_an_approved_file_is_queued_not_done_until_the_job_finishes():
    group = lambda **extra: rc.recommend(record(**extra)).group           # noqa: E731
    assert group(approval_status="approved") == rc.G_QUEUED
    assert group(approval_status="approved", migration_status="in_progress") == rc.G_QUEUED
    assert group(migration_status="completed") == rc.G_DONE
    assert group(migration_status="failed") == rc.G_NEEDS and group(migration_status="manual_review") == rc.G_NEEDS
    assert rc.G_QUEUED in rc.GROUP_ORDER and rc.GROUP_ORDER.index(rc.G_QUEUED) < rc.GROUP_ORDER.index(rc.G_DONE)


# ---------------------------------------------------------------- the menu
def test_remote_sits_next_to_the_dashboard_and_has_a_button_in_the_mobile_bar(client, v2):
    page = text(client.get("/"))
    assert menu(page)["links"][:2] == ["Dashboard", "Remote"]
    bar = page.split('<div class="collapse navbar-collapse"')[0]
    assert 'href="/tv/remote"' in bar and "d-lg-none" in bar.split('href="/tv/remote"')[0].rsplit("<a", 1)[1]


# ---------------------------------------------------------------- Sessions
PLAYING = {"UserName": "Greg", "DeviceName": "Living room", "Client": "Jellyfin Roku", "LastActivityDate": "2026-10-30T20:00:00Z",
           "NowPlayingItem": {"Type": "Episode", "SeriesName": "Cold Case", "ParentIndexNumber": 2, "IndexNumber": 5, "Name": "Hubris", "RunTimeTicks": 2700 * 10_000_000},
           "PlayState": {"PositionTicks": 675 * 10_000_000, "IsPaused": False, "PlayMethod": "Transcode"},
           "TranscodingInfo": {"IsVideoDirect": False, "VideoCodec": "h264", "TranscodeReasons": ["VideoCodecNotSupported", "AudioChannelsNotSupported"]}}
IDLE = {"UserName": "Dana", "DeviceName": "iPhone", "Client": "Jellyfin iOS"}


def test_a_session_becomes_a_plain_row():
    row = describe(PLAYING)
    assert (row["user"], row["device"], row["title"]) == ("Greg", "Living room", "Cold Case: S02E05 Hubris")
    assert (row["state"], row["position"], row["length"], row["progress"], row["method"]) == ("Playing", "11:15", "45:00", 25, "Transcoding")
    assert row["why"] == "video re-encoded to h264, because of video codec, audio channels"
    idle = describe(IDLE)
    assert idle["playing"] is False and idle["title"] is None and describe({"Client": "SyncHub"}) is None


def test_people_playing_come_first():
    class Fake:
        def sessions(self):
            return [IDLE, PLAYING, {"Client": "SyncHub"}]
    assert [r["user"] for r in live(Fake())] == ["Greg", "Dana"]


def test_the_sessions_page_shows_who_watches_what_and_the_dvr(client, v2, monkeypatch):
    settings.set_value("jellyfin_url", "http://jf:8096")
    settings.set_value("jellyfin_api_key", "k")
    settings.set_value("dvr_enabled", "1")
    monkeypatch.setattr(JellyfinClient, "sessions", lambda self, active_within=960: [PLAYING, IDLE])
    with v2.connection() as conn:
        for tid, name, status, start, end in (("t1", "News", "InProgress", "2026-10-30 19:00:00", "2026-10-30 21:00:00"), ("t2", "Late film", "New", "2026-10-31 02:00:00", "2026-10-31 04:00:00"),
                                              ("t3", "Next week", "New", "2026-11-08 02:00:00", "2026-11-08 04:00:00")):
            conn.execute("INSERT INTO dvr_timer (timer_id, kind, name, status, start_at, end_at, first_seen, last_seen) VALUES (?, 'timer', ?, ?, ?, ?, '2026-10-30 00:00:00', '2026-10-30 00:00:00')",
                         (tid, name, status, start, end))
    monkeypatch.setattr(dvr, "db", type("D", (), {"now": staticmethod(lambda: "2026-10-30 20:00:00")}))
    page = text(client.get("/system/sessions"))
    assert "Cold Case: S02E05 Hubris" in page and "Living room" in page and "Transcoding" in page and "Dana on iPhone" in page
    assert "News" in page and "Late film" in page and "Next week" not in page
    assert "Sessions" in menu(page)["dropdowns"]["System"]


def test_the_sessions_page_still_shows_the_dvr_when_jellyfin_is_unreachable(client, v2, monkeypatch):
    settings.set_value("jellyfin_url", "http://jf:8096")
    settings.set_value("jellyfin_api_key", "k")
    def boom(self, active_within=960):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(JellyfinClient, "sessions", boom)
    page = text(client.get("/system/sessions"))
    assert "Could not read Jellyfin" in page and "connection refused" in page and "Nobody is watching" not in page
