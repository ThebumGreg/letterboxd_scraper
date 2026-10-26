"""New-episode alerts: later episodes of a followed show, the choice of when and for which shows, muting one show, and the 'aired' notice."""
from synchub import db
from synchub.notifications import arrivals, store

from .test_notification_rules import notes, put_in_library, recompute
from .test_notifications import add_channel, add_profile, push  # noqa: F401


def show_with_episodes(conn, pid, count=3, air="2020-01-01"):
    sid = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'Severance', '9')").lastrowid
    conn.execute("INSERT INTO manual_request (media_id, profile_id) VALUES (?, ?)", (sid, pid))
    eps = [conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date) VALUES (?, 1, ?, ?)", (sid, n, air)).lastrowid for n in range(1, count + 1)]
    conn.commit()
    return sid, eps


def following(conn, name="Greg"):
    pid = add_profile(conn, name)
    add_channel(conn, pid)
    return pid


def test_later_episodes_of_a_followed_show_are_one_notice_per_pass(v2, conn):
    pid = following(conn)
    sid, (e1, e2, e3) = show_with_episodes(conn, pid)
    recompute()
    put_in_library(conn, "episode", e1, "ep1")
    recompute()
    assert len(notes(conn, "request_available")) == 1 and notes(conn, "new_episode") == []
    put_in_library(conn, "episode", e2, "ep2")
    put_in_library(conn, "episode", e3, "ep3")
    recompute()
    got = notes(conn, "new_episode")
    assert len(got) == 1 and got[0]["profile_id"] == pid and "S1E2 and S1E3" in got[0]["body"] and "Severance" in got[0]["title"]
    recompute()
    assert len(notes(conn, "new_episode")) == 1                      # not again


def test_a_person_can_switch_the_notice_off_or_mute_one_show(v2, conn):
    pid = following(conn)
    sid, (e1, e2, e3) = show_with_episodes(conn, pid)
    recompute()
    put_in_library(conn, "episode", e1, "ep1")
    recompute()
    store.set_muted(conn, pid, sid, True)
    conn.commit()
    put_in_library(conn, "episode", e2, "ep2")
    recompute()
    assert notes(conn, "new_episode") == []
    store.set_muted(conn, pid, sid, False)
    store.set_rule(conn, pid, "new_episode", False)
    conn.commit()
    put_in_library(conn, "episode", e3, "ep3")
    recompute()
    assert notes(conn, "new_episode") == []


def test_only_the_chosen_source_counts(v2, conn):
    pid = following(conn)
    sid, (e1, e2, e3) = show_with_episodes(conn, pid)           # the show is a request, not on a watchlist
    store.set_episode_pref(conn, pid, "library", "watchlist")
    conn.commit()
    recompute()
    put_in_library(conn, "episode", e1, "ep1")
    recompute()
    put_in_library(conn, "episode", e2, "ep2")
    recompute()
    assert notes(conn, "new_episode") == []
    store.set_episode_pref(conn, pid, "library", "requested")
    conn.commit()
    put_in_library(conn, "episode", e3, "ep3")
    recompute()
    assert len(notes(conn, "new_episode")) == 1


def test_airing_notices_go_to_those_who_chose_them_and_only_once(v2, conn):
    pid = following(conn)
    quiet = following(conn, "Bob")
    sid, (e1, e2, e3) = show_with_episodes(conn, pid, air="2026-11-03")
    conn.execute("INSERT INTO manual_request (media_id, profile_id) VALUES (?, ?)", (sid, quiet))
    store.set_episode_pref(conn, pid, "aired", "both")
    conn.commit()
    now = "2026-11-03 12:00:00"
    assert arrivals.announce_aired(conn, now=now) == 1               # Bob keeps the default: tell me when it reaches the library
    got = notes(conn, "episode_aired")
    assert len(got) == 1 and got[0]["profile_id"] == pid and "3 new episodes" in got[0]["body"] and "not in the library yet" in got[0]["body"]
    assert arrivals.announce_aired(conn, now="2026-11-03 18:00:00") == 0
    assert arrivals.announce_aired(conn, now="2026-11-09 12:00:00") == 0      # too long ago to be news


def test_the_choices_are_checked_and_saved(app, client, conn):
    pid = add_profile(conn, "Greg")
    sid, _ = show_with_episodes(conn, pid)
    conn.commit()
    client.set_cookie("synchub_profile", str(pid))
    assert client.post("/api/notifications/episodes", json={"episode_when": "never", "episode_source": "both"}).status_code == 400
    assert client.post("/api/notifications/episodes", json={"episode_when": "both", "episode_source": "requested"}).get_json()["ok"]
    with db.connection() as c:
        assert store.pref(c, pid)["episode_when"] == "both" and store.pref(c, pid)["episode_source"] == "requested"
    page = client.get(f"/show/{sid}").data.decode()
    assert 'id="episodeAlertsSwitch"' in page and "checked" in page.split('id="episodeAlertsSwitch"')[1][:40]
    assert client.post(f"/api/notifications/shows/{sid}/mute", json={"muted": "yes"}).status_code == 400
    assert client.post("/api/notifications/shows/9999/mute", json={"muted": True}).status_code == 404
    assert client.post(f"/api/notifications/shows/{sid}/mute", json={"muted": True}).get_json()["muted"] is True
    assert "checked" not in client.get(f"/show/{sid}").data.decode().split('id="episodeAlertsSwitch"')[1][:40]
    with db.connection() as c:
        assert store.is_muted(c, pid, sid)


def test_a_persons_page_holds_their_profile_access_and_notifications(app, client, conn):
    pid = following(conn)
    other = add_profile(conn, "Bob")
    conn.commit()
    client.set_cookie("synchub_profile", str(pid))
    mine = client.get(f"/people/{pid}").data.decode()
    assert all(x in mine for x in ("pane-identity", "pane-access", "pane-notifications", 'id="episodesPanel"', 'id="quietPanel"', 'id="adminAlerts"'))
    theirs = client.get(f"/people/{other}").data.decode()
    assert 'id="episodesPanel"' not in theirs and "delivery methods" in theirs and "pane-access" in theirs
    assert client.get("/people/9999").status_code == 302
    assert client.get("/my-profile").headers["Location"] == f"/people/{pid}"
