"""Notifications, phase 2: what each person wants to hear about, quiet hours, and the events that raise them."""
import json

import pytest

from synchub import db, jobs, settings, workers
from synchub.notifications import dispatch, emit, store, watch

from .test_notifications import USER, OTHER_USER, T0, add_channel, add_profile, push  # noqa: F401  (the helpers and the pretend Pushover)

NIGHT = "2026-11-03 04:30:00"       # 22:30 the evening before in America/Chicago (UTC-6 in November)
DAY = "2026-11-03 18:00:00"


def admin(conn, name="Greg", key=USER):
    pid = add_profile(conn, name)
    add_channel(conn, pid, key)
    settings.set_value("notify_admin_profiles", str(pid))
    return pid


def notes(conn, event_type=None):
    sql = "SELECT n.*, e.event_type FROM notification n JOIN notification_event e ON e.id = n.event_id"
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY n.id")]
    return [r for r in rows if event_type in (None, r["event_type"])]


# ---------------------------------------------------------------- rules
def test_events_follow_their_default_until_a_person_chooses(conn):
    pid = admin(conn)
    conn.commit()
    by = {r["event_type"]: r["enabled"] for r in store.rules(conn, pid)}
    assert by["job_failed"] is True and by["job_done"] is False and "test" not in by
    store.set_rule(conn, pid, "job_done", True)
    store.set_rule(conn, pid, "job_failed", False)
    conn.commit()
    by = {r["event_type"]: r["enabled"] for r in store.rules(conn, pid)}
    assert by["job_done"] is True and by["job_failed"] is False
    with pytest.raises(ValueError):
        store.set_rule(conn, pid, "test", True)


def test_an_event_a_person_switched_off_is_not_queued_for_them(conn):
    pid = admin(conn)
    store.set_rule(conn, pid, "job_failed", False)
    assert emit.notify(conn, "job_failed", "a", {"title": "x"}, now=T0) == []
    store.set_rule(conn, pid, "job_failed", True)
    assert len(emit.notify(conn, "job_failed", "b", {"title": "x"}, now=T0)) == 1


def test_admin_events_go_only_to_the_chosen_administrators(conn):
    chosen = admin(conn)
    other = add_profile(conn, "Bob")
    add_channel(conn, other, OTHER_USER)
    conn.commit()
    emit.notify(conn, "job_failed", "a", {"title": "x"}, now=T0)
    assert {n["profile_id"] for n in notes(conn)} == {chosen}


def test_nobody_is_told_when_no_administrator_is_chosen(conn):
    pid = add_profile(conn)
    add_channel(conn, pid)
    assert emit.notify(conn, "job_failed", "a", {"title": "x"}, now=T0) == []


# ---------------------------------------------------------------- quiet hours
CHICAGO = {"quiet_on": True, "quiet_start": "22:00", "quiet_end": "07:00", "timezone": "America/Chicago"}


def test_quiet_hours_that_cross_midnight():
    assert store.quiet_until(CHICAGO, NIGHT) == "2026-11-03 13:00:00"        # 07:00 Chicago
    assert store.quiet_until(CHICAGO, DAY) is None
    assert store.quiet_until({**CHICAGO, "quiet_on": False}, NIGHT) is None
    early = "2026-11-03 11:00:00"                                            # 05:00 Chicago: still the same night
    assert store.quiet_until(CHICAGO, early) == "2026-11-03 13:00:00"


def test_quiet_hours_within_one_day():
    p = {**CHICAGO, "quiet_start": "13:00", "quiet_end": "15:00"}
    assert store.quiet_until(p, "2026-11-03 20:00:00") == "2026-11-03 21:00:00"      # 14:00 Chicago
    assert store.quiet_until(p, DAY) is None                                        # 12:00 Chicago


def test_preferences_are_checked(conn):
    pid = add_profile(conn)
    store.set_pref(conn, pid, True, "21:30", "06:15", "Europe/London")
    assert store.pref(conn, pid)["timezone"] == "Europe/London"
    for args in ((True, "25:00", "06:00", "UTC"), (True, "22:00", "22:00", "UTC"), (True, "22:00", "06:00", "Nowhere/Land")):
        with pytest.raises(ValueError):
            store.set_pref(conn, pid, *args)


def test_ordinary_notices_wait_out_quiet_hours_but_urgent_ones_do_not(conn):
    pid = admin(conn)
    store.set_pref(conn, pid, True, "22:00", "07:00", "America/Chicago")
    emit.notify(conn, "job_failed", "a", {"title": "x"}, now=NIGHT)             # priority 0: waits
    emit.notify(conn, "worker_offline", "b", {"worker": "w"}, now=NIGHT)        # priority 1: now
    conn.commit()
    failed, offline = notes(conn, "job_failed")[0], notes(conn, "worker_offline")[0]
    assert failed["next_attempt_at"] == "2026-11-03 13:00:00" and failed["status"] == "pending"
    assert offline["next_attempt_at"] is None
    assert failed["expires_at"] > failed["next_attempt_at"]                      # it must not expire while it waits


def test_a_held_notice_is_not_sent_until_quiet_hours_end(conn, push):
    pid = admin(conn)
    store.set_pref(conn, pid, True, "22:00", "07:00", "America/Chicago")
    emit.notify(conn, "job_failed", "a", {"title": "x"}, now=NIGHT)
    conn.commit()
    settings.set_value("enable_notifications", "1")
    assert dispatch.run_due(now=NIGHT)["sent"] == 0 and push.sent == []
    assert dispatch.run_due(now="2026-11-03 13:01:00")["sent"] == 1 and len(push.sent) == 1


# ---------------------------------------------------------------- job events
def make_worker(conn):
    wid, _ = workers.create(conn, "w1", (), 1)
    return workers.get(conn, wid)


def test_a_failed_job_raises_one_notice_and_a_retry_does_not(conn):
    admin(conn)
    w = make_worker(conn)
    jid = jobs.create(conn, "library", title="Movie.mkv", max_attempts=2, now=T0)
    jobs.claim(conn, w, 1, now=T0)
    assert jobs.fail(conn, jid, w["id"], "transient", "boom", now=T0) == "queued"
    assert notes(conn) == []
    jobs.claim(conn, w, 1, now=jobs._later(T0, 3600))
    assert jobs.fail(conn, jid, w["id"], "transient", "boom again", now=jobs._later(T0, 3600)) == "failed"
    got = notes(conn, "job_failed")
    assert len(got) == 1 and "Movie.mkv" in got[0]["body"] and "boom again" in got[0]["body"]


def test_a_finished_job_is_quiet_by_default_and_loud_when_asked(conn):
    pid = admin(conn)
    w = make_worker(conn)
    jid = jobs.create(conn, "library", title="A.mkv", now=T0)
    jobs.claim(conn, w, 1, now=T0)
    jobs.complete(conn, jid, w["id"], now=T0)
    assert notes(conn, "job_done") == []
    store.set_rule(conn, pid, "job_done", True)
    jid = jobs.create(conn, "library", title="B.mkv", now=T0)
    jobs.claim(conn, w, 1, now=T0)
    jobs.complete(conn, jid, w["id"], now=T0)
    assert len(notes(conn, "job_done")) == 1


def test_a_job_that_needs_a_decision_raises_a_notice(conn):
    admin(conn)
    w = make_worker(conn)
    jid = jobs.create(conn, "library", title="C.mkv", now=T0)
    jobs.claim(conn, w, 1, now=T0)
    jobs.needs_review(conn, jid, w["id"], "destination exists", now=T0)
    assert len(notes(conn, "job_review")) == 1


# ---------------------------------------------------------------- the watcher
def report(conn, w, roots=None, seen=None):
    status = json.dumps({"roots": roots or {}})
    conn.execute("UPDATE worker SET last_seen = ?, status = ? WHERE id = ?", (seen or T0, status, w["id"]))
    conn.commit()


def test_a_worker_going_quiet_and_returning_is_reported_once_each(conn):
    admin(conn)
    w = make_worker(conn)
    report(conn, w)
    assert watch.check(now=T0) == 0                                              # the first look says nothing
    later = jobs._later(T0, 3600)
    assert watch.check(now=later) == 1 and watch.check(now=jobs._later(later, 60)) == 0
    report(conn, w, seen=jobs._later(later, 120))
    assert watch.check(now=jobs._later(later, 130)) == 1
    assert [n["event_type"] for n in notes(conn)] == ["worker_offline", "worker_online"]


def test_a_share_that_stops_working_is_reported_and_so_is_its_recovery(conn):
    admin(conn)
    w = make_worker(conn)
    report(conn, w, {"media": {"ok": True}})
    watch.check(now=T0)
    report(conn, w, {"media": {"ok": False, "state": "not_mounted"}})
    assert watch.check(now=jobs._later(T0, 30)) == 1
    assert "not mounted" in notes(conn, "share_problem")[0]["body"]
    report(conn, w, {"media": {"ok": True}}, seen=jobs._later(T0, 60))
    assert watch.check(now=jobs._later(T0, 65)) == 1
    assert len(notes(conn, "share_recovered")) == 1


def test_a_tv_that_has_not_accepted_the_key_is_reported_once(conn, monkeypatch):
    admin(conn)
    state = {"tv": ("Front Room TV", True)}
    monkeypatch.setattr(watch, "_tv_states", lambda: state)
    assert watch.check(now=T0) == 1 and watch.check(now=jobs._later(T0, 30)) == 0
    state["tv"] = ("Front Room TV", False)
    watch.check(now=jobs._later(T0, 60))
    state["tv"] = ("Front Room TV", True)
    assert watch.check(now=jobs._later(T0, 90)) == 1


# ---------------------------------------------------------------- the pages
def test_rules_and_quiet_hours_are_saved_from_the_page(app, client, conn):
    pid = admin(conn)
    conn.commit()
    client.set_cookie("synchub_profile", str(pid))
    page = client.get(f"/people/{pid}")
    assert b"What to tell me about" in page.data and b"A job failed" in page.data
    assert client.post("/api/notifications/rules", json={"event_type": "job_failed", "enabled": False}).get_json()["ok"]
    assert client.post("/api/notifications/rules", json={"event_type": "nope", "enabled": True}).status_code == 404
    bad = client.post("/api/notifications/quiet", json={"quiet_on": True, "quiet_start": "99:00", "quiet_end": "07:00", "timezone": "UTC"})
    assert bad.status_code == 400
    assert client.post("/api/notifications/quiet", json={"quiet_on": True, "quiet_start": "22:00", "quiet_end": "07:00", "timezone": "UTC"}).get_json()["ok"]
    with db.connection() as c:
        assert store.wants(c, pid, "job_failed") is False and store.pref(c, pid)["quiet_on"] is True


def test_a_profile_that_is_not_an_administrator_does_not_see_admin_choices(app, client, conn):
    admin(conn)
    other = add_profile(conn, "Bob")
    conn.commit()
    client.set_cookie("synchub_profile", str(other))
    assert b"A job failed" not in client.get("/notifications", follow_redirects=True).data


def test_administrators_are_chosen_on_the_persons_page(app, client, conn):
    pid = add_profile(conn, "Greg")
    conn.commit()
    assert client.post(f"/api/access/profile/{pid}/admin-alerts", json={"enabled": True}).get_json()["ok"]
    assert settings.get("notify_admin_profiles") == str(pid)
    assert client.post(f"/api/access/profile/{pid}/admin-alerts", json={"enabled": "yes"}).status_code == 400
    assert client.post("/api/access/profile/999/admin-alerts", json={"enabled": True}).status_code == 404
    assert client.post(f"/api/access/profile/{pid}/admin-alerts", json={"enabled": False}).get_json()["ok"]
    assert settings.get("notify_admin_profiles") == ""


# ---------------------------------------------------------------- a wanted title arrives
from synchub import states  # noqa: E402


def wanted_movie(conn, pid, title="Heat", tmdb="1"):
    mid = conn.execute("INSERT INTO media (kind, title, year, tmdb_id) VALUES ('movie', ?, 1995, ?)", (title, tmdb)).lastrowid
    conn.execute("INSERT INTO manual_request (media_id, profile_id) VALUES (?, ?)", (mid, pid))
    conn.commit()
    return mid


def put_in_library(conn, kind, entity_id, item_id):
    conn.execute("INSERT INTO src_jellyfin_item (item_id, type, first_seen, last_seen) VALUES (?, ?, ?, ?)", (item_id, kind, T0, T0))
    source = "jellyfin" if kind == "movie" else "jellyfin_episode"
    conn.execute("INSERT INTO link (entity_type, entity_id, source, source_key, method, confidence) VALUES (?, ?, ?, ?, 'x', 1.0)",
                 ("media" if kind == "movie" else "episode", entity_id, source, item_id))
    conn.commit()


def recompute(now=T0):
    with db.connection() as c:
        states.compute(c, now=now)


def test_a_wanted_movie_arriving_notifies_the_person_who_wanted_it(v2, conn):
    pid = add_profile(conn, "Greg")
    add_channel(conn, pid)
    other = add_profile(conn, "Bob")
    add_channel(conn, other, OTHER_USER)
    mid = wanted_movie(conn, pid)
    recompute()                                              # first sight: Missing, nothing said
    assert notes(conn) == []
    put_in_library(conn, "movie", mid, "jf1")
    recompute()
    got = notes(conn, "request_available")
    assert [n["profile_id"] for n in got] == [pid] and "Heat" in got[0]["title"]
    recompute()
    assert len(notes(conn, "request_available")) == 1        # not again


def test_a_title_already_in_the_library_on_first_sight_is_not_news(v2, conn):
    pid = add_profile(conn, "Greg")
    add_channel(conn, pid)
    mid = wanted_movie(conn, pid)
    put_in_library(conn, "movie", mid, "jf1")
    recompute()
    assert notes(conn) == []


def test_a_show_is_announced_for_its_first_episode_only(v2, conn):
    pid = add_profile(conn, "Greg")
    add_channel(conn, pid)
    sid = conn.execute("INSERT INTO media (kind, title, tmdb_id) VALUES ('show', 'Severance', '9')").lastrowid
    conn.execute("INSERT INTO manual_request (media_id, profile_id) VALUES (?, ?)", (sid, pid))
    e1 = conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date) VALUES (?, 1, 1, '2020-01-01')", (sid,)).lastrowid
    e2 = conn.execute("INSERT INTO episode (media_id, season_number, episode_number, air_date) VALUES (?, 1, 2, '2020-01-01')", (sid,)).lastrowid
    conn.commit()
    recompute()
    put_in_library(conn, "episode", e1, "ep1")
    recompute()
    put_in_library(conn, "episode", e2, "ep2")
    recompute()
    got = notes(conn, "request_available")
    assert len(got) == 1 and "first episode" in got[0]["body"]


def test_a_title_above_a_persons_rating_limit_is_not_announced_to_them(v2, conn):
    pid = add_profile(conn, "Kid")
    add_channel(conn, pid)
    mid = wanted_movie(conn, pid)
    conn.execute("UPDATE profile SET max_rating = 1 WHERE id = ?", (pid,))          # an unrated title is not allowed under a limit
    conn.commit()
    recompute()
    put_in_library(conn, "movie", mid, "jf1")
    recompute()
    assert notes(conn) == []


def test_a_bulk_arrival_is_treated_as_an_import(v2, conn):
    pid = add_profile(conn, "Greg")
    add_channel(conn, pid)
    ids = [wanted_movie(conn, pid, f"T{i}", str(i)) for i in range(60)]
    recompute()
    for i, mid in enumerate(ids):
        put_in_library(conn, "movie", mid, f"jf{i}")
    recompute()
    assert notes(conn) == []


# ---------------------------------------------------------------- grouping
def test_ordinary_notices_are_grouped_into_one_message(conn, push):
    pid = admin(conn)
    store.set_pref(conn, pid, False, "22:00", "07:00", "UTC", batch_minutes=60)
    for i in range(3):
        emit.notify(conn, "job_failed", f"j{i}", {"title": f"File{i}.mkv", "error": "boom"}, now="2026-11-03 10:05:00")
    emit.notify(conn, "worker_offline", "w", {"worker": "w1"}, now="2026-11-03 10:06:00")            # urgent: on its own, at once
    conn.commit()
    settings.set_value("enable_notifications", "1")
    assert dispatch.run_due(now="2026-11-03 10:30:00")["sent"] == 1 and len(push.sent) == 1          # only the urgent one
    assert dispatch.run_due(now="2026-11-03 11:00:00")["sent"] == 1 and len(push.sent) == 2
    message = push.sent[1]["data"]["message"] if "data" in push.sent[1] else json.dumps(push.sent[1])
    assert "File0.mkv" in message and "File2.mkv" in message
    merged = [n for n in notes(conn, "job_failed")]
    assert {n["status"] for n in merged} == {"held"} and len(merged) == 3


def test_a_single_waiting_notice_is_sent_as_it_is(conn, push):
    pid = admin(conn)
    store.set_pref(conn, pid, False, "22:00", "07:00", "UTC", batch_minutes=15)
    emit.notify(conn, "job_failed", "j", {"title": "Solo.mkv", "error": "boom"}, now="2026-11-03 10:01:00")
    conn.commit()
    settings.set_value("enable_notifications", "1")
    assert dispatch.run_due(now="2026-11-03 10:15:00")["sent"] == 1
    assert "Job failed" in json.dumps(push.sent[0])


def test_batch_choices_are_checked(conn):
    pid = add_profile(conn)
    with pytest.raises(ValueError):
        store.set_pref(conn, pid, False, "22:00", "07:00", "UTC", batch_minutes=7)
