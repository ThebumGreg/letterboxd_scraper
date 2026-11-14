"""Poor quality on a movie or show page (synchub/poor_quality.py), and the links between jobs, records and titles (synchub/library/links.py)."""
import pytest

from synchub import downloads, jobs, poor_quality, quality, storage
from synchub.clients import ApiError
from synchub.library import links

from .library_helpers import add

NOW = "2026-01-01 00:00:00"


def release(title, resolution=1080, guid=None, indexer=3, seeders=20, full_season=False):
    return {"guid": guid or title, "indexerId": indexer, "title": title, "size": 8_000_000_000, "seeders": seeders, "indexer": "Tracker", "protocol": "torrent", "ageHours": 5,
            "quality": {"quality": {"name": f"WEBDL-{resolution}p", "resolution": resolution}}, "rejections": [], "downloadAllowed": True, "fullSeason": full_season}


def link(conn, source, key, entity_id, entity="media"):
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES (?,?,?,?, 'x', 1.0)", (entity, entity_id, source, key))


def jellyfin_item(conn, item_id, entity_id, source="jellyfin", entity="media", dvr=0, path=None):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen, is_dvr, path) VALUES (?, ?, ?, ?, ?, ?)", (item_id, 'movie' if entity == 'media' else 'episode', NOW, NOW, dvr, path))
    link(conn, source, item_id, entity_id, entity)


def movie(conn, in_radarr=True, title="Heat", tmdb="949"):
    media_id = conn.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('movie', ?, 1995, ?)", (title, tmdb)).lastrowid
    if in_radarr:
        conn.execute("INSERT INTO src_radarr_movie (radarr_id, first_seen, last_seen) VALUES ('7', ?, ?)", (NOW, NOW))
        link(conn, "radarr", "7", media_id)
    conn.commit()
    return media_id


def show(conn, episodes=3, in_sonarr=True):
    """A show with one season. Returns (media id, [episode ids]); Sonarr has a file for every episode, numbered 501, 502, ..."""
    media_id = conn.execute("INSERT INTO media (kind, title, tmdb_id, tvdb_id) VALUES ('show', 'Cold Case', '4', '5')").lastrowid
    if in_sonarr:
        conn.execute("INSERT INTO src_sonarr_series (sonarr_id, first_seen, last_seen) VALUES ('10', ?, ?)", (NOW, NOW))
        link(conn, "sonarr", "10", media_id)
    ids = []
    for n in range(1, episodes + 1):
        eid = conn.execute("INSERT INTO episode (media_id, season_number, episode_number, title) VALUES (?, 1, ?, ?)", (media_id, n, f"Part {n}")).lastrowid
        ids.append(eid)
        if in_sonarr:
            conn.execute("INSERT INTO src_sonarr_episode (sonarr_episode_id, sonarr_id, season_number, episode_number, has_file, first_seen, last_seen) VALUES (?, '10', 1, ?, 1, ?, ?)",
                         (str(500 + n), n, NOW, NOW))
            link(conn, "sonarr_episode", str(500 + n), eid, "episode")
    conn.commit()
    return media_id, ids


class FakeRadarr:
    def __init__(self, releases=(), history=None, fail_block=False):
        self.history = history if history is not None else [{"id": 11, "eventType": "grabbed", "sourceTitle": "Heat.1995.DVDRip", "date": "2020-01-01"}]
        self.release_list, self.fail_block, self.calls = list(releases), fail_block, []

    def movie_history(self, movie_id):
        self.calls.append(("history", str(movie_id)))
        return self.history

    def mark_history_failed(self, history_id):
        self.calls.append(("block", history_id))
        if self.fail_block:
            raise ApiError("Radarr MarkHistoryFailed failed: HTTP 500")

    def releases(self, movie_id):
        self.calls.append(("releases", str(movie_id)))
        return self.release_list

    def grab_release(self, guid, indexer_id):
        self.calls.append(("grab", guid, indexer_id))


class FakeSonarr:
    def __init__(self, releases=(), history=None):
        self.history = history if history is not None else [
            {"id": 21, "episodeId": 501, "eventType": "grabbed", "sourceTitle": "Cold.Case.S01E01.SD", "date": "2020-01-01"},
            {"id": 22, "episodeId": 502, "eventType": "grabbed", "sourceTitle": "Cold.Case.S01.SD.PACK", "date": "2020-01-02"},
            {"id": 23, "episodeId": 503, "eventType": "grabbed", "sourceTitle": "Cold.Case.S01.SD.PACK", "date": "2020-01-02"},
            {"id": 24, "episodeId": 501, "eventType": "downloadFolderImported", "sourceTitle": "Cold.Case.S01E01.SD", "date": "2020-01-03"}]
        self.release_list, self.calls = list(releases), []

    def series_history(self, series_id, season_number=None):
        self.calls.append(("history", str(series_id), season_number))
        return self.history

    def mark_history_failed(self, history_id):
        self.calls.append(("block", history_id))

    def releases(self, episode_id=None, series_id=None, season_number=None):
        self.calls.append(("releases", episode_id and str(episode_id), series_id and str(series_id), season_number))
        return self.release_list

    def grab_release(self, guid, indexer_id):
        self.calls.append(("grab", guid, indexer_id))


def requests(conn, media_id):
    return [dict(r) for r in conn.execute("SELECT * FROM media_quality_request WHERE media_id = ? ORDER BY id", (media_id,))]


# ---------------------------------------------------------------- films
def test_a_film_is_flagged_from_its_page_without_a_library_file(conn):
    media_id = movie(conn)
    radarr = FakeRadarr(releases=[release("Heat.720p", 720), release("Heat.1080p.A", 1080, seeders=5), release("Heat.1080p.B", 1080, guid="gB", indexer=4, seeders=50)])
    out = poor_quality.request(media_id, note="blurry", by="Greg", client=radarr)
    assert [c[0] for c in radarr.calls] == ["history", "releases", "block", "grab"]            # nothing is blocked before a replacement is known
    assert ("block", 11) in radarr.calls and ("grab", "gB", 4) in radarr.calls                  # the best seeded 1080p, never the 720p
    assert out["status"] == "grabbed" and "Blocked “Heat.1995.DVDRip”" in out["message"] and "old file is kept" in out["message"]
    row = requests(conn, media_id)[0]
    assert (row["service"], row["scope"], row["note"], row["blocked"], row["requested_by"], row["chosen_release"]) == ("radarr", "movie", "blurry", 1, "Greg", "Heat.1080p.B")


def test_nothing_is_blocked_when_no_release_qualifies_and_the_failure_is_kept(conn):
    media_id = movie(conn)
    radarr = FakeRadarr(releases=[release("Heat.720p", 720), release("Heat.1995.DVDRip", 1080)])        # the second is the release being replaced
    with pytest.raises(poor_quality.PoorQualityError, match="no other release of at least 1080p"):
        poor_quality.request(media_id, client=radarr)
    assert not [c for c in radarr.calls if c[0] in ("block", "grab")]
    assert requests(conn, media_id)[0]["status"] == "failed"


def test_show_releases_changes_nothing_and_a_pick_is_looked_up_again(conn):
    media_id = movie(conn)
    radarr = FakeRadarr(releases=[release("Heat.1080p.A", 1080, guid="gA")])
    shown = poor_quality.releases(media_id, client=radarr)
    assert shown["bad_releases"] == ["Heat.1995.DVDRip"] and [c["title"] for c in shown["candidates"]] == ["Heat.1080p.A"]
    assert not [c for c in radarr.calls if c[0] in ("block", "grab")] and requests(conn, media_id) == []
    with pytest.raises(poor_quality.PoorQualityError, match="not on offer any more"):
        poor_quality.grab(media_id, "gone", 3, client=radarr)
    out = poor_quality.grab(media_id, "gA", 3, by="Greg", client=radarr)
    assert out["status"] == "grabbed" and ("grab", "gA", 3) in radarr.calls
    assert [r["status"] for r in requests(conn, media_id)] == ["failed", "grabbed"]


def test_a_failed_block_is_reported_and_the_grab_still_happens(conn):
    media_id = movie(conn)
    radarr = FakeRadarr(releases=[release("Heat.1080p.A", 1080, guid="gA")], fail_block=True)
    out = poor_quality.request(media_id, client=radarr)
    assert "could not be blocked" in out["message"] and ("grab", "gA", 3) in radarr.calls
    assert requests(conn, media_id)[0]["blocked"] == 0


def test_a_film_radarr_does_not_have_is_added_and_searched(conn, monkeypatch):
    from synchub import automation
    media_id = movie(conn, in_radarr=False)
    monkeypatch.setattr(automation, "_add_movie", lambda client, tmdb_id: "Heat")
    out = poor_quality.request(media_id, by="Greg", client=FakeRadarr())
    assert out["status"] == "requested" and "added now" in out["message"]
    with pytest.raises(poor_quality.PoorQualityError, match="does not have this film yet"):
        poor_quality.releases(media_id, client=FakeRadarr())


def test_the_request_is_attached_to_the_films_file_when_one_is_known(conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media", "jellyfin_path": "/media"})
    file_id = add(conn, root="media", rel="Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")
    media_id = movie(conn)
    jellyfin_item(conn, "jf1", media_id, path="/media/Heat (1995)/Heat (1995).mkv")
    conn.commit()
    poor_quality.request(media_id, by="Greg", client=FakeRadarr(releases=[release("Heat.2160p", 2160, guid="g4")]))
    asked = quality.state_for(conn, dict(conn.execute("SELECT * FROM library_file WHERE id = ?", (file_id,)).fetchone()))["request"]
    assert asked["status"] == "grabbed" and asked["chosen_release"] == "Heat.2160p" and asked["service_item_id"] == "7"


def test_a_dvr_recording_of_a_film_is_never_flagged(conn):
    media_id = movie(conn)
    jellyfin_item(conn, "rec1", media_id, dvr=1)
    conn.commit()
    state = poor_quality.page_state(conn, media_id)
    assert not state["available"] and "DVR recording" in state["why"]
    radarr = FakeRadarr(releases=[release("Heat.1080p", 1080)])
    for call in (lambda: poor_quality.request(media_id, client=radarr), lambda: poor_quality.releases(media_id, client=radarr)):
        with pytest.raises(poor_quality.PoorQualityError, match="DVR recording"):
            call()
    assert radarr.calls == []
    jellyfin_item(conn, "lib1", media_id, dvr=0)                       # a library copy next to the recording: the library copy may be replaced
    conn.commit()
    assert poor_quality.page_state(conn, media_id)["available"]


# ---------------------------------------------------------------- shows
def test_one_episode_blocks_its_own_grab_and_never_takes_a_season_pack(conn):
    media_id, eps = show(conn)
    sonarr = FakeSonarr(releases=[release("Cold.Case.S01.1080p.PACK", 1080, guid="pack", full_season=True), release("Cold.Case.S01E01.1080p", 1080, guid="e1")])
    out = poor_quality.request(media_id, note="wrong language", by="Greg", episode_id=eps[0], client=sonarr)
    assert ("releases", "501", None, None) in sonarr.calls and ("block", 21) in sonarr.calls and ("grab", "e1", 3) in sonarr.calls
    assert not [c for c in sonarr.calls if c == ("grab", "pack", 3)]
    assert "Sonarr replaces it on import" in out["message"]
    row = requests(conn, media_id)[0]
    assert (row["service"], row["scope"], row["episode_id"], row["season_number"], row["note"]) == ("sonarr", "episode", eps[0], 1, "wrong language")


def test_a_season_takes_a_pack_and_blocks_each_old_release_once(conn):
    media_id, _ = show(conn)
    sonarr = FakeSonarr(releases=[release("Cold.Case.S01E01.1080p", 1080, guid="e1"), release("Cold.Case.S01.1080p.PACK", 1080, guid="pack", full_season=True)])
    out = poor_quality.request(media_id, season_number=1, client=sonarr)
    assert ("releases", None, "10", 1) in sonarr.calls and ("grab", "pack", 3) in sonarr.calls
    assert sorted(c[1] for c in sonarr.calls if c[0] == "block") == [21, 22]                   # the pack that covered two episodes is blocked once
    assert out["status"] == "grabbed" and requests(conn, media_id)[0]["blocked"] == 2
    with pytest.raises(poor_quality.PoorQualityError, match="no other season pack"):
        poor_quality.request(media_id, season_number=1, client=FakeSonarr(releases=[release("Cold.Case.S01E01.1080p", 1080)]))


def test_show_releases_for_a_show_lists_everything_but_the_automatic_pick_keeps_the_minimum(conn):
    media_id, eps = show(conn)
    only_sd = [release("Cold.Case.S01E02.480p", 480, guid="sd")]
    assert [c["title"] for c in poor_quality.releases(media_id, episode_id=eps[1], client=FakeSonarr(releases=only_sd))["candidates"]] == ["Cold.Case.S01E02.480p"]
    with pytest.raises(poor_quality.PoorQualityError, match="at least 1080p"):
        poor_quality.request(media_id, episode_id=eps[1], client=FakeSonarr(releases=only_sd))
    assert poor_quality.grab(media_id, "sd", 3, episode_id=eps[1], client=FakeSonarr(releases=only_sd))["status"] == "grabbed"      # a person may still choose it


def test_dvr_recordings_and_episodes_sonarr_did_not_download_are_left_out(conn):
    media_id, eps = show(conn)
    jellyfin_item(conn, "rec1", eps[0], "jellyfin_episode", "episode", dvr=1)                  # only a recording
    jellyfin_item(conn, "lib2", eps[1], "jellyfin_episode", "episode", dvr=0)
    conn.execute("UPDATE src_sonarr_episode SET has_file = 0 WHERE sonarr_episode_id = '503'")
    conn.commit()
    flags = {e["id"]: e["can_flag"] for e in poor_quality.episodes(conn, media_id)}
    assert flags == {eps[0]: False, eps[1]: True, eps[2]: False}
    assert poor_quality.page_state(conn, media_id)["seasons"] == [{"number": 1, "label": "Season 1", "episodes": 1}]
    sonarr = FakeSonarr(releases=[release("Cold.Case.S01E01.1080p", 1080)])
    with pytest.raises(poor_quality.PoorQualityError, match="DVR recording"):
        poor_quality.request(media_id, episode_id=eps[0], client=sonarr)
    with pytest.raises(poor_quality.PoorQualityError, match="no downloaded file"):
        poor_quality.request(media_id, episode_id=eps[2], client=sonarr)
    assert sonarr.calls == []
    pack = FakeSonarr(releases=[release("Cold.Case.S01.1080p.PACK", 1080, guid="pack", full_season=True)])
    poor_quality.request(media_id, season_number=1, client=pack)
    assert [c[1] for c in pack.calls if c[0] == "block"] == [22]                               # only the downloaded, non-recorded episode's release


def test_a_show_sonarr_does_not_have_or_that_is_all_recordings_has_no_buttons(conn):
    media_id, eps = show(conn, in_sonarr=False)
    state = poor_quality.page_state(conn, media_id)
    assert not state["available"] and "Sonarr does not have this show" in state["why"]
    with pytest.raises(poor_quality.PoorQualityError, match="Sonarr does not have this show"):
        poor_quality.request(media_id, season_number=1, client=FakeSonarr())


# ---------------------------------------------------------------- the pages
def test_the_movie_page_has_the_buttons_and_the_api_refuses_a_recording(client, conn, monkeypatch):
    media_id = movie(conn)
    page = client.get(f"/movie/{media_id}").get_data(as_text=True)
    assert 'data-poor-open="request"' in page and 'data-poor-open="releases"' in page and f"/api/library/poor/{media_id}/" in page
    monkeypatch.setattr(poor_quality, "_client", lambda service, client=None: FakeRadarr(releases=[release("Heat.1080p", 1080, guid="g1")]))
    assert client.post(f"/api/library/poor/{media_id}/releases", json={}).get_json()["candidates"][0]["guid"] == "g1"
    done = client.post(f"/api/library/poor/{media_id}/request", json={"note": "soft picture"}).get_json()
    assert done["ok"] and done["status"] == "grabbed"
    assert "soft picture" in client.get(f"/movie/{media_id}").get_data(as_text=True)          # the page shows who flagged it and why
    assert client.post(f"/api/library/poor/{media_id}/grab", json={}).status_code == 400
    jellyfin_item(conn, "rec1", media_id, dvr=1)
    conn.commit()
    page = client.get(f"/movie/{media_id}").get_data(as_text=True)
    assert 'data-poor-open="' not in page and "DVR recording" in page
    refused = client.post(f"/api/library/poor/{media_id}/request", json={})
    assert refused.status_code == 400 and "DVR recording" in refused.get_json()["message"]


def test_the_show_page_offers_seasons_and_marks_the_episodes_that_can_be_flagged(client, conn):
    media_id, eps = show(conn)
    jellyfin_item(conn, "rec1", eps[0], "jellyfin_episode", "episode", dvr=1)
    conn.commit()
    page = client.get(f"/show/{media_id}").get_data(as_text=True)
    assert 'id="poorSeason"' in page and "Season 1 (2 downloaded episodes)" in page
    season = client.get(f"/api/show/{media_id}/season/1").get_json()["episodes"]
    assert [e["can_flag"] for e in season] == [False, True, True]


def test_the_buttons_need_the_library_permission(client, conn, v2):
    from synchub import settings
    media_id = movie(conn)
    settings.set_value("access_guest_permissions", "dashboard")
    with v2.connection() as c:
        c.execute("INSERT INTO profile (name, pin_hash) VALUES ('Owner', 'x')")              # restrictions only apply once a profile has a PIN
    assert 'data-poor-open="' not in client.get(f"/movie/{media_id}").get_data(as_text=True)
    assert client.post(f"/api/library/poor/{media_id}/request", json={}).status_code in (302, 403)


# ---------------------------------------------------------------- jobs, records and downloads point at each other
def test_every_kind_of_job_leads_to_its_files_record(conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media"})
    file_id = add(conn, root="media", rel="Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")
    key = conn.execute("SELECT path_key FROM library_file WHERE id = ?", (file_id,)).fetchone()[0]
    check = conn.execute("INSERT INTO quality_check (library_file_id, status) VALUES (?, 'queued')", (file_id,)).lastrowid
    intake = conn.execute("INSERT INTO download_import (service, root_name, relative_path, path_key, record_id) VALUES ('radarr', 'media', 'Heat (1995)/Heat (1995).mkv', ?, ?)", (key, file_id)).lastrowid
    clip = conn.execute("INSERT INTO preview (subject_type, subject_id, seconds) VALUES ('download_import', ?, 30)", (intake,)).lastrowid
    made = {ref: jobs.create(conn, kind, title=ref, ref=(ref, ref_id)) for kind, ref, ref_id in (
        ("library", "library_file", file_id), ("library", "library_hide", file_id), ("library", "library_source", file_id), ("scan", "quality_check", check),
        ("download", "download_import", intake), ("preview", "preview", clip))}
    other = [jobs.create(conn, "scan", title="Scan media", ref=("library_run:1", None)), jobs.create(conn, "library", title="gone", ref=("library_file", 99999)),
             jobs.create(conn, "library", title="Test copy", ref=("test", None))]
    conn.commit()
    for job_id in made.values():
        assert links.record_for_job(conn, jobs.get(conn, job_id)) == file_id
    for job_id in other:
        assert links.record_for_job(conn, jobs.get(conn, job_id)) is None                      # a scan, a deleted record, a test copy: no button
    assert [j["id"] for j in links.jobs_for_record(conn, file_id)] == sorted(made.values(), reverse=True)


def test_the_jobs_page_and_the_record_page_link_to_each_other(client, conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media"})
    file_id = add(conn, root="media", rel="Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")
    job_id = jobs.create(conn, "library", title="Convert: Heat (1995).mkv", ref=("library_file", file_id))
    conn.commit()
    assert client.get(f"/api/jobs/{job_id}").get_json()["job"]["record_id"] == file_id
    assert f'href="/library/record/{file_id}"' in client.get("/jobs").get_data(as_text=True)
    record = client.get(f"/library/record/{file_id}").get_data(as_text=True)
    assert "Jobs for this file" in record and f"/jobs?job={job_id}" in record
    assert 'class="nav nav-tabs mb-3 media-subnav"' in record and 'href="/jobs"' in record      # the Media tabs stay on a record page


def test_a_new_download_opens_its_record_or_the_titles_page(conn):
    storage.create(conn, {"name": "media", "protocol": "local", "share": "/mnt/media", "mount_point": "/mnt/media"})
    file_id = add(conn, root="media", rel="Heat (1995)/Heat (1995).mkv", name="Heat (1995).mkv")
    key = conn.execute("SELECT path_key FROM library_file WHERE id = ?", (file_id,)).fetchone()[0]
    media_id = movie(conn)
    conn.execute("INSERT INTO download_import (service, service_item_id, root_name, relative_path, path_key) VALUES ('radarr', 7, 'media', 'Heat (1995)/Heat (1995).mkv', ?)", (key,))
    conn.execute("INSERT INTO download_import (service, service_item_id, root_name, relative_path, path_key, record_id) VALUES ('radarr', 7, 'media', 'New/New.mkv', 'media|new/new.mkv', 4242)")
    conn.commit()
    newest, older = downloads.recent(conn, 5)
    assert older["record_id"] == file_id                                                      # found by its path although the row never got its record id
    assert newest["record_id"] is None and newest["media"] == {"id": media_id, "kind": "movie"}   # no record (the old id is gone): the film's page instead
