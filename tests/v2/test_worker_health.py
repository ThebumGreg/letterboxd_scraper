"""Worker health: temperatures from the agent, the 14 day history, heat and low-space alerts, the cool-down guard and drive health."""
import json

from synchub import jobs, workers
from synchub.notifications import watch

from .agent_module import agent
from .test_notification_rules import admin, make_worker, notes

T0 = "2026-11-03 10:00:00"
GB = 10 ** 9


def minutes(n, base=T0):
    return jobs._later(base, n * 60)


def status(temp=60.0, free=200 * GB, cpu=20.0):
    return {"work_free_bytes": free, "gpu_encodes": 1, "host": {"cpu_percent": cpu, "temps": {"cpu": temp, "max": temp + 3}, "mem_available_bytes": 8 * GB, "gpu_mhz": 900}}


def checkin(conn, w, at, **kw):
    workers.touch(conn, w["id"], status(**kw), now=at)
    conn.commit()                                                           # the watcher opens its own connection


# ---------------------------------------------------------------- reading the sensors (agent)
def sysfs(tmp_path, sensors):
    for i, (name, values) in enumerate(sensors.items()):
        hw = tmp_path / "class" / "hwmon" / f"hwmon{i}"
        hw.mkdir(parents=True)
        (hw / "name").write_text(name + "\n")
        for j, milli in enumerate(values, 1):
            (hw / f"temp{j}_input").write_text(f"{milli}\n")
    return str(tmp_path)


def test_temperatures_are_grouped_and_the_hottest_of_each_is_kept(tmp_path):
    root = sysfs(tmp_path, {"coretemp": [61000, 64000, 58000], "nvme": [41850], "acpitz": [27800], "amdgpu": [55000]})
    assert agent.read_temperatures(root) == {"cpu": 64.0, "nvme": 41.9, "other": 27.8, "gpu": 55.0, "max": 64.0}


def test_nonsense_and_missing_sensors_give_nothing_and_never_raise(tmp_path):
    assert agent.read_temperatures(str(tmp_path)) == {}
    assert agent.read_temperatures(sysfs(tmp_path, {"coretemp": [0, 250000]})) == {}                      # zero and impossible readings are ignored
    assert agent.read_gpu_clock(str(tmp_path)) == {}


def test_the_gpu_clock_is_read_from_the_i915_card(tmp_path):
    card = tmp_path / "class" / "drm" / "card0"
    card.mkdir(parents=True)
    (card / "gt_cur_freq_mhz").write_text("1050\n")
    (card / "gt_max_freq_mhz").write_text("1150\n")
    assert agent.read_gpu_clock(str(tmp_path)) == {"gpu_mhz": 1050, "gpu_max_mhz": 1150}


# ---------------------------------------------------------------- history
def test_a_row_is_kept_every_five_minutes_and_old_rows_go(conn):
    w = make_worker(conn)
    for n in range(0, 12):
        checkin(conn, w, minutes(n))                                         # one check-in a minute
    assert conn.execute("SELECT COUNT(*) FROM worker_stat").fetchone()[0] == 3     # minutes 0, 5 and 10
    far = "2026-11-25 10:00:00"
    checkin(conn, w, far)
    assert [r["at"] for r in conn.execute("SELECT at FROM worker_stat")] == [far]  # the rest are over 14 days old
    pts = workers.history(conn, w["id"], 24, now=far)
    assert pts == [{"at": far, "cpu": 20.0, "temp": 60.0, "free_gb": 200, "running": 0, "gpu_encodes": 1}]


def test_history_is_averaged_down_to_the_points_asked_for(conn):
    w = make_worker(conn)
    for n in range(0, 200, 5):
        checkin(conn, w, minutes(n), temp=50.0 + (n % 20), cpu=10.0)
    pts = workers.history(conn, w["id"], 24, points=10, now=minutes(200))
    assert 0 < len(pts) <= 10 and all(p["cpu"] == 10.0 for p in pts)
    assert max(p["temp"] for p in pts) <= 69.0 and workers.history(conn, w["id"], 1, now=minutes(200))[0]["at"] >= minutes(140)


def test_a_status_without_sensors_still_checks_in(conn):
    w = make_worker(conn)
    workers.touch(conn, w["id"], {"work_free_bytes": 5 * GB, "host": {}}, now=T0)
    assert workers.history(conn, w["id"], 1, now=T0)[0]["temp"] is None and workers.get(conn, w["id"])["last_seen"] == T0


# ---------------------------------------------------------------- heat: alert and the cool-down guard
def test_staying_hot_raises_one_alert_and_cooling_raises_one_all_clear(conn):
    admin(conn)
    w = make_worker(conn)
    checkin(conn, w, T0, temp=70.0)
    assert watch.check(now=T0) == 0
    for n in range(1, 16):
        checkin(conn, w, minutes(n), temp=87.0)
        if n == 5:
            assert not workers.heat_state(conn, w["id"])["alerted"]                  # hot for five minutes: not yet (limit is ten)
    assert workers.heat_state(conn, w["id"])["alerted"]
    checkin(conn, w, minutes(16), temp=87.0)
    assert watch.check(now=minutes(16)) == 1
    checkin(conn, w, minutes(17), temp=87.0)
    assert watch.check(now=minutes(17)) == 0
    assert "87 C for 10 minutes" in notes(conn, "worker_hot")[0]["body"]
    checkin(conn, w, minutes(20), temp=70.0)
    assert watch.check(now=minutes(20)) == 1
    assert len(notes(conn, "worker_cooled")) == 1


def test_a_short_spike_does_nothing(conn):
    admin(conn)
    w = make_worker(conn)
    checkin(conn, w, T0, temp=60.0)
    watch.check(now=T0)
    checkin(conn, w, minutes(1), temp=95.0)
    checkin(conn, w, minutes(3), temp=60.0)
    state = workers.heat_state(conn, w["id"])
    checkin(conn, w, minutes(4), temp=60.0)
    assert not state["alerted"] and not state["throttled"] and watch.check(now=minutes(4)) == 0


def test_too_hot_stops_new_jobs_until_it_is_well_under_the_limit(conn):
    w = make_worker(conn)
    job = jobs.create(conn, "library", title="A", now=T0)
    for n in range(0, 7):
        checkin(conn, w, minutes(n), temp=92.0)                                    # over 90 for more than five minutes
    assert workers.throttled(conn, w["id"]) is True
    assert jobs.claim(conn, workers.get(conn, w["id"]), 1, now=minutes(7)) == []
    assert jobs.get(conn, job)["status"] == "queued"
    checkin(conn, w, minutes(8), temp=85.0)                                        # under the limit but not under 80: still cooling
    assert workers.throttled(conn, w["id"]) is True
    checkin(conn, w, minutes(10), temp=78.0)
    assert workers.throttled(conn, w["id"]) is False
    assert [j["id"] for j in jobs.claim(conn, workers.get(conn, w["id"]), 1, now=minutes(11))] == [job]


def test_the_guard_can_be_switched_off_per_worker(conn):
    w = make_worker(conn)
    workers.update(conn, w["id"], settings={"throttle": False})
    for n in range(0, 10):
        checkin(conn, w, minutes(n), temp=99.0)
    assert workers.throttled(conn, w["id"]) is False


def test_the_heat_limits_are_settings_with_sane_ranges(conn):
    w = make_worker(conn)
    workers.update(conn, w["id"], settings={"temp_alert_c": 80, "temp_alert_minutes": 3, "disk_low_gb": 50})
    got = workers.get(conn, w["id"])
    assert (workers.setting(got, "temp_alert_c"), workers.setting(got, "temp_alert_minutes"), workers.setting(got, "disk_low_gb"), workers.setting(got, "throttle_c")) == (80, 3, 50, 90)
    for bad in ({"temp_alert_c": 500}, {"throttle_minutes": 0}):
        try:
            workers.update(conn, w["id"], settings=bad)
        except workers.WorkerError:
            continue
        raise AssertionError(f"{bad} was accepted")
    assert "temp_alert_c" not in workers.agent_settings(got)                        # SyncHub's own limits are never sent to the agent


# ---------------------------------------------------------------- space and drives
def test_low_work_space_is_told_once_and_again_only_after_it_recovers(conn):
    admin(conn)
    w = make_worker(conn)
    checkin(conn, w, T0, free=100 * GB)
    watch.check(now=T0)
    checkin(conn, w, minutes(1), free=20 * GB)
    assert watch.check(now=minutes(1)) == 1 and "20 GB free" in notes(conn, "worker_disk_low")[0]["body"]
    checkin(conn, w, minutes(2), free=31 * GB)                                      # just over the limit: inside the 10% slack, still low
    assert watch.check(now=minutes(2)) == 0
    checkin(conn, w, minutes(3), free=100 * GB)
    assert watch.check(now=minutes(3)) == 0
    checkin(conn, w, minutes(4), free=10 * GB)
    assert watch.check(now=minutes(4)) == 1


def test_a_failing_drive_is_told_once_and_a_healthy_one_never(conn):
    admin(conn)
    w = make_worker(conn)
    checkin(conn, w, T0)
    workers.store_disks(conn, w["id"], {"smart": [{"device": "/dev/sda", "model": "Fine", "passed": True, "wear_pct": 4},
                                                  {"device": "/dev/sdb", "model": "Old", "passed": False}, {"device": "/dev/nvme0n1", "model": "Worn", "passed": True, "wear_pct": 93}]}, now=T0)
    conn.commit()
    checkin(conn, w, minutes(1))
    assert watch.check(now=minutes(1)) == 2
    checkin(conn, w, minutes(2))
    assert watch.check(now=minutes(2)) == 0
    bodies = " ".join(n["body"] for n in notes(conn, "drive_health"))
    assert "drive /dev/sdb Old failed its health check" in bodies and "93% worn out" in bodies and "Fine" not in bodies


def test_a_health_report_keeps_the_last_drive_list_and_a_drive_scan_keeps_the_last_health(conn):
    w = make_worker(conn)
    workers.store_disks(conn, w["id"], {"work_dir": "/mnt/a", "disks": [{"device": "/dev/sdb1", "name": "sdb1", "mount": "/mnt/a", "mounted": True, "size": 10, "free": 5}]}, now=T0)
    workers.store_disks(conn, w["id"], {"smart": [{"device": "/dev/sdb", "model": "SSD", "passed": True}], "sleep": True}, now=minutes(60))
    got = workers.disks_of(conn, w["id"])
    assert [d["name"] for d in got["disks"]] == ["sdb1"] and got["at"] == T0 and got["sleep"] is True and got["smart"][0]["passed"] is True
    workers.store_disks(conn, w["id"], {"disks": []}, now=minutes(90))
    assert workers.disks_of(conn, w["id"])["smart"][0]["model"] == "SSD"


# ---------------------------------------------------------------- the page
def test_the_workers_page_shows_temperatures_the_guard_and_the_stats_window(client, v2):
    with v2.connection() as conn:
        w = make_worker(conn)
        conn.execute("UPDATE worker SET capabilities = ? WHERE id = ?", (json.dumps({"host": {"cpu": {"model": "i5"}}}), w["id"]))
        for n in range(0, 7):
            checkin(conn, w, jobs._later(db_now(), -3600 + n * 60), temp=92.0)
    page = client.get("/workers").get_data(as_text=True)
    assert "Temperature" in page and "CPU 92 °C" in page and "Cooling down" in page
    assert 'data-worker-stats="' in page and 'id="workerStatsModal"' in page and 'id="setThrottleC"' in page
    got = client.get(f"/api/workers/{w['id']}/history?hours=24").get_json()
    assert got["ok"] and got["points"] and got["limits"] == {"alert_c": 85, "throttle_c": 90} and got["heat"]["throttled"] is True
    assert client.get("/api/workers/999/history").status_code == 404


def db_now():
    from synchub import db
    return db.now()
