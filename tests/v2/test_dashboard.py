"""The morning TV dashboard: settings, weather, quote, calendars, school week, Wi-Fi presence, the schedule, and the pages."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from settings_validation import validate_setting
from synchub import settings, unifi_network
from synchub.dashboard import calendars, config, data, quote, school, show, weather

TZ = timezone(timedelta(hours=-5))


def at(y, m, d, hh=7, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


class Answer:
    def __init__(self, body, status=200, text=""):
        self.body, self.status_code, self.text, self.content = body, status, text or (body if isinstance(body, str) else ""), b"x"

    def json(self):
        return self.body


# ------------------------------------------------------------------ settings
def test_people_are_cleaned_and_checked():
    ok, text = config.clean_people(json.dumps([
        {"name": " Greg ", "kind": "calendar", "ics": "webcal://example.com/a.ics", "color": "bad"},
        {"name": "Sam", "kind": "school", "mac": "AA-BB-CC-DD-EE-FF", "week": {"mon": {"leave": "07:20", "start": "08:00", "end": "15:10"}, "tue": {"leave": "", "start": ""}}}]))
    assert ok
    greg, sam = json.loads(text)
    assert greg["name"] == "Greg" and greg["ics"] == "https://example.com/a.ics" and greg["color"].startswith("#")
    assert sam["mac"] == "aa:bb:cc:dd:ee:ff" and list(sam["week"]) == ["mon"]


@pytest.mark.parametrize("bad", [
    [{"name": ""}],
    [{"name": "A"}, {"name": "a"}],
    [{"name": "A", "kind": "calendar", "ics": "ftp://x"}],
    [{"name": "A", "kind": "school", "week": {"mon": {"leave": "07:20"}}}],
    [{"name": "A", "kind": "school", "mac": "12:34"}],
])
def test_bad_people_are_refused_with_a_message(bad):
    ok, message = config.clean_people(json.dumps(bad))
    assert not ok and message


def test_no_school_lines(v2):
    ok, text = config.clean_no_school("2026-11-26 Thanksgiving\n\n2026-12-21 - 2027-01-03 Winter break")
    assert ok and text.splitlines() == ["2026-11-26 Thanksgiving", "2026-12-21 to 2027-01-03 Winter break"]
    assert not config.clean_no_school("next tuesday")[0]
    assert not config.clean_no_school("2026-02-31")[0]
    assert not config.clean_no_school("2026-12-05 to 2026-12-01")[0]
    settings.set_value("dash_no_school", text)
    assert config.no_school_on(date(2026, 11, 26)) == "Thanksgiving"
    assert config.no_school_on(date(2027, 1, 1)) == "Winter break"
    assert config.no_school_on(date(2026, 11, 27)) is None


def test_validation_of_the_simple_settings():
    assert validate_setting("dash_start", "06:30") == (True, "06:30")
    assert not validate_setting("dash_end", "25:00")[0]
    assert validate_setting("dash_days", "fri,mon,xyz") == (True, "mon,fri")
    assert validate_setting("dash_zip", "10001")[0] and validate_setting("dash_zip", "40.71, -74.00")[0] and validate_setting("dash_zip", "")[0]
    assert not validate_setting("dash_zip", "1000")[0]
    assert not validate_setting("dash_units", "kelvin")[0]
    assert not validate_setting("dash_app", "not a package")[0]
    assert validate_setting("unifi_net_host", "https://192.168.1.1/")[1] == "192.168.1.1"
    assert not validate_setting("dash_presence_grace", "0")[0]


# ------------------------------------------------------------------ weather
def forecast_body():
    hourly_times = [f"2026-11-09T{h:02d}:00" for h in range(24)]
    return {"current": {"time": "2026-11-09T06:45", "temperature_2m": 38.4, "apparent_temperature": 31.2, "weather_code": 61, "wind_speed_10m": 9.6},
            "hourly": {"time": hourly_times, "temperature_2m": [40] * 24, "precipitation_probability": [30] * 24, "weather_code": [3] * 24},
            "daily": {"time": ["2026-11-09", "2026-11-10"], "weather_code": [61, 0], "temperature_2m_max": [44.2, 50.0], "temperature_2m_min": [33.0, 35.5],
                      "precipitation_probability_max": [70, 5], "sunrise": ["2026-11-09T06:40"], "sunset": ["2026-11-09T16:50"]}}


def test_weather_looks_up_the_zip_once_and_caches(v2, monkeypatch):
    calls = []

    def fake(service, event, method, url, **kw):
        calls.append(url)
        if "zippopotam" in url:
            return Answer({"places": [{"latitude": "40.75", "longitude": "-73.99", "place name": "New York", "state abbreviation": "NY"}]})
        if "air-quality" in url:
            return Answer({"current": {"us_aqi": 42}})
        assert kw["params"]["temperature_unit"] == "fahrenheit" and kw["params"]["wind_speed_unit"] == "mph"
        return Answer(forecast_body())
    monkeypatch.setattr(weather.http, "request", fake)
    monkeypatch.setitem(weather._cache, "at", 0)
    settings.set_value("dash_zip", "10001")
    got = weather.get()
    assert got["place"] == "New York, NY" and got["unit"] == "°F"
    assert got["now"] == {"temp": 38, "feels": 31, "words": "Light rain", "symbol": "🌦️", "wind": 10}
    assert got["days"][0]["high"] == 44 and got["days"][0]["rain"] == 70 and got["days"][1]["words"] == "Clear"
    assert len(got["hours"]) == 6
    weather.get()
    assert len(calls) == 4 and "air-quality" in calls[2] and "api.weather.gov" in calls[3] and got["aqi"] == 42    # a ZIP lookup, the forecast, the air quality and the alerts; the second get() came from the cache


def test_weather_failure_gives_a_message_or_the_last_good_answer(v2, monkeypatch):
    settings.set_value("dash_zip", "40.71, -74.00")
    monkeypatch.setitem(weather._cache, "key", None)

    def down(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(weather.http, "request", down)
    assert "error" in weather.get()
    monkeypatch.setattr(weather.http, "request", lambda *a, **k: Answer(forecast_body()))
    weather._cache["at"] = 0
    assert "error" not in weather.get()
    monkeypatch.setattr(weather.http, "request", down)
    weather._cache["at"] = 0
    assert weather.get()["stale"] is True
    settings.set_value("dash_zip", "")
    assert "ZIP" in weather.get()["error"]


def test_metric_units_are_asked_for(v2, monkeypatch):
    seen = {}
    monkeypatch.setattr(weather.http, "request", lambda s, e, m, url, **kw: seen.update(kw["params"]) or Answer(forecast_body()))
    settings.set_value("dash_zip", "40.71, -74.00")
    weather.fetch("40.71, -74.00", True)
    assert seen["temperature_unit"] == "celsius" and seen["wind_speed_unit"] == "kmh"


# ------------------------------------------------------------------ quote
def test_quote_is_fetched_once_a_day_and_falls_back(v2, monkeypatch):
    monkeypatch.setattr(quote.http, "request", lambda *a, **k: Answer([{"q": "Keep going.", "a": "Someone"}]))
    first = quote.today(date(2026, 11, 9))
    assert first["text"] == "Keep going." and first["source"] == "zenquotes.io"
    monkeypatch.setattr(quote.http, "request", lambda *a, **k: 1 / 0)
    assert quote.today(date(2026, 11, 9))["text"] == "Keep going."          # kept all day
    other = quote.today(date(2026, 11, 10))
    assert other["source"] == "built-in" and other["text"] == quote.fallback(date(2026, 11, 10))["text"]


def test_a_rate_limit_sentence_is_not_shown_as_a_quote(v2, monkeypatch):
    monkeypatch.setattr(quote.http, "request", lambda *a, **k: Answer([{"q": "Too many requests. Obtain an auth key for unlimited access.", "a": "zenquotes.io"}]))
    assert quote.today(date(2026, 11, 9))["source"] == "built-in"


# ------------------------------------------------------------------ calendars
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:1
DTSTART:20261109T130000Z
DTEND:20261109T140000Z
SUMMARY:Dentist
LOCATION:Main St
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART:20261102T140000Z
DTEND:20261102T143000Z
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART;VALUE=DATE:20261109
DTEND;VALUE=DATE:20261110
SUMMARY:Trash day
END:VEVENT
BEGIN:VEVENT
UID:4
DTSTART:20261109T150000Z
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:5
DTSTART:20261111T150000Z
DTEND:20261111T160000Z
SUMMARY:Later in the week
END:VEVENT
END:VCALENDAR
"""


def test_events_include_repeats_all_day_and_local_times():
    start = at(2026, 11, 9, 0, 0)
    found = calendars.events_between(ICS, start, start + timedelta(days=1))
    assert [e["title"] for e in found] == ["Trash day", "Dentist", "Standup"]
    assert found[0]["all_day"] and not found[1]["all_day"]
    assert found[1]["start"] == "2026-11-09T08:00:00-05:00" and found[1]["where"] == "Main St"


def test_a_person_sees_what_is_left_today_or_the_next_day_with_events(monkeypatch):
    monkeypatch.setattr(calendars, "download", lambda url: (ICS, False))
    morning = calendars.for_person("https://x/y.ics", at(2026, 11, 9, 6, 30))
    assert [e["title"] for e in morning["today"]] == ["Trash day", "Dentist", "Standup"] and morning["next"] is None
    evening = calendars.for_person("https://x/y.ics", at(2026, 11, 9, 20, 0))
    assert [e["title"] for e in evening["today"]] == ["Trash day"]               # the timed ones are over
    assert evening["next"]["date"] == "2026-11-11" and evening["next"]["events"][0]["title"] == "Later in the week"


def test_download_uses_the_last_copy_when_the_address_stops_answering(monkeypatch):
    calendars._cache.clear()
    good = Answer(None, text=ICS)
    good.content = ICS.encode()
    monkeypatch.setattr(calendars.http, "request", lambda *a, **k: good)
    assert calendars.download("webcal://h/c.ics") == (ICS, False)
    calendars._cache["https://h/c.ics"]["at"] = 0
    monkeypatch.setattr(calendars.http, "request", lambda *a, **k: Answer(None, 500))
    assert calendars.download("webcal://h/c.ics") == (ICS, True)
    with pytest.raises(calendars.CalendarError):
        calendars.download("https://never/seen.ics")
    monkeypatch.setattr(calendars.http, "request", lambda *a, **k: Answer(None, text="<html>"))
    with pytest.raises(calendars.CalendarError, match="not a calendar"):
        calendars.download("https://other/page")


# ------------------------------------------------------------------ school
KID = {"name": "Sam", "kind": "school", "mac": "aa:bb:cc:dd:ee:ff", "week": {"mon": {"leave": "07:20", "start": "08:00", "end": "15:10"}}}


def test_school_day_plan(v2):
    monday = at(2026, 11, 9)
    plan = school.day_plan(KID, monday)
    assert plan["state"] == "school" and plan["leave"].startswith("2026-11-09T07:20") and plan["end"].startswith("2026-11-09T15:10")
    assert school.day_plan(KID, at(2026, 11, 10))["state"] == "none"
    settings.set_value("dash_no_school", "2026-11-09 Teacher day")
    assert school.day_plan(KID, monday) == {"state": "holiday", "label": "Teacher day"}


def test_leaving_needs_seen_first_a_gap_and_the_right_time():
    earliest = at(2026, 11, 9, 6, 30)
    entry = school.settle(None, False, at(2026, 11, 9, 6, 50), earliest, 3)
    assert entry["state"] == "unknown"                                       # never seen at home: nothing to claim
    entry = school.settle(entry, True, at(2026, 11, 9, 6, 55), earliest, 3)
    assert entry["state"] == "home"
    entry = school.settle(entry, False, at(2026, 11, 9, 7, 0), earliest, 3)
    assert entry["state"] == "home" and entry["gone_since"]                  # just dropped off, not "left" yet
    entry = school.settle(entry, True, at(2026, 11, 9, 7, 1), earliest, 3)     # phone woke up again
    assert entry["gone_since"] is None
    entry = school.settle(entry, False, at(2026, 11, 9, 7, 5), earliest, 3)
    entry = school.settle(entry, False, at(2026, 11, 9, 7, 9), earliest, 3)
    assert entry["state"] == "left" and entry["left_at"].startswith("2026-11-09T07:05")
    entry = school.settle(entry, True, at(2026, 11, 9, 7, 30), earliest, 3)    # still "left" if the phone shows up again
    assert entry["state"] == "left"


def test_away_before_the_earliest_time_does_not_count():
    entry = school.settle({"state": "home", "gone_since": None}, False, at(2026, 11, 9, 6, 10), at(2026, 11, 9, 6, 30), 3)
    assert entry["state"] == "home" and not entry.get("gone_since")


class FakeNetwork:
    def __init__(self, macs):
        self.macs = macs

    def online_macs(self):
        return set(self.macs)


def test_poll_records_a_departure_and_keeps_it_for_the_day(v2):
    settings.set_value("dash_enabled", "1")
    settings.set_value("unifi_net_host", "10.0.0.1")
    settings.set_value("unifi_net_api_key", "k")
    settings.set_value("dash_people", json.dumps([KID]))
    home, gone = FakeNetwork({KID["mac"]}), FakeNetwork(set())
    school.poll(at(2026, 11, 9, 6, 40), home)
    school.poll(at(2026, 11, 9, 7, 10), gone)
    state = school.poll(at(2026, 11, 9, 7, 14), gone)
    assert state["kids"]["Sam"]["state"] == "left"
    assert school.poll(at(2026, 11, 9, 7, 20), home)["kids"]["Sam"]["state"] == "left"
    page = school.for_page(KID, at(2026, 11, 9, 7, 30))
    assert page["presence"] == "left" and page["left_at"].startswith("2026-11-09T07:10")
    assert school.load_state(date(2026, 11, 10))["kids"] == {}                 # a new day starts clean
    assert school.poll(at(2026, 11, 9, 23, 0), home) is None                   # nothing outside the morning


def test_a_unifi_error_is_kept_for_the_page_and_does_not_raise(v2):
    settings.set_value("dash_enabled", "1")
    settings.set_value("unifi_net_host", "10.0.0.1")
    settings.set_value("unifi_net_api_key", "k")
    settings.set_value("dash_people", json.dumps([KID]))

    class Broken:
        def online_macs(self):
            raise unifi_network.NetworkError("rejected the API key")
    state = school.poll(at(2026, 11, 9, 7, 0), Broken())
    assert state["error"] == "rejected the API key"
    assert school.for_page(KID, at(2026, 11, 9, 7, 0))["presence_error"] == "rejected the API key"


# ------------------------------------------------------------------ UniFi Network client
def make_client(monkeypatch, routes):
    seen = []

    def fake(service, event, method, url, **kw):
        path = url.split("/proxy/network")[1]
        seen.append((path, kw.get("params"), kw["headers"]["X-API-KEY"]))
        return routes(path, kw.get("params") or {})
    monkeypatch.setattr(unifi_network.http, "request", fake)
    return unifi_network.NetworkClient("https://10.0.0.1/", "key", verify=True), seen


def test_clients_through_the_integration_api_with_paging(monkeypatch, v2):
    pages = {0: [{"id": "1", "macAddress": "AA:BB:CC:00:00:01", "name": "Sam's iPhone", "type": "WIRELESS", "ipAddress": "10.0.0.5"}],
             1: [{"id": "2", "macAddress": "aa:bb:cc:00:00:02", "name": "NAS", "type": "WIRED"}]}
    monkeypatch.setattr(unifi_network, "PAGE", 1)

    def routes(path, params):
        if path == "/integration/v1/sites":
            return Answer({"data": [{"id": "site1", "internalReference": "default"}]})
        return Answer({"data": pages.get(params["offset"], []), "totalCount": 2})
    client, seen = make_client(monkeypatch, routes)
    found = client.clients()
    assert [c["name"] for c in found] == ["Sam's iPhone", "NAS"] and found[0]["mac"] == "aa:bb:cc:00:00:01" and not found[0]["wired"]
    assert client.online_macs() == {"aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"}
    assert all(s[2] == "key" for s in seen)


def test_a_list_without_mac_addresses_is_completed_from_the_client_details(monkeypatch, v2):
    unifi_network._mac_by_id.clear()
    asked = []

    def routes(path, params):
        if path == "/integration/v1/sites":
            return Answer({"data": [{"id": "site1"}]})
        if path.endswith("/clients"):
            return Answer({"data": [{"id": "c1", "name": "Sam's iPhone", "type": "WIRELESS"}, {"id": "c2", "name": "Switch", "type": "WIRED", "macAddress": "aa:bb:cc:00:00:09"}], "totalCount": 2})
        asked.append(path)
        return Answer({"id": "c1", "type": "WIRELESS", "macAddress": "AA:BB:CC:00:00:07"})
    client, _ = make_client(monkeypatch, routes)
    assert client.online_macs() == {"aa:bb:cc:00:00:07", "aa:bb:cc:00:00:09"}
    client.online_macs()
    assert asked == ["/integration/v1/sites/site1/clients/c1"]                    # remembered, not asked again


def test_clients_fall_back_to_the_older_api(monkeypatch, v2):
    def routes(path, params):
        if path.startswith("/integration"):
            return Answer({}, 404)
        return Answer({"data": [{"mac": "AA:BB:CC:00:00:03", "hostname": "pixel", "is_wired": False, "ip": "10.0.0.9"}]})
    client, _ = make_client(monkeypatch, routes)
    assert client.clients()[0]["name"] == "pixel"


def test_network_errors_say_what_to_do(monkeypatch, v2):
    client, _ = make_client(monkeypatch, lambda path, params: Answer({}, 401))
    with pytest.raises(unifi_network.NetworkError, match="Network application's own"):
        client.clients()
    client, _ = make_client(monkeypatch, lambda path, params: Answer({}, 404))
    with pytest.raises(unifi_network.NetworkError, match="no UniFi Network API"):
        client.clients()
    with pytest.raises(unifi_network.NetworkError, match="not set"):
        unifi_network.NetworkClient("", "")


# ------------------------------------------------------------------ the schedule
def test_window_and_days(v2):
    settings.set_value("dash_days", "mon,tue,wed,thu,fri")
    assert show.in_window(at(2026, 11, 9, 6, 30)) and show.in_window(at(2026, 11, 9, 8, 29))
    assert not show.in_window(at(2026, 11, 9, 6, 29)) and not show.in_window(at(2026, 11, 9, 8, 30))
    assert not show.in_window(at(2026, 11, 8, 7, 0))                          # a Sunday
    settings.set_value("dash_days", "sun,mon")
    assert show.in_window(at(2026, 11, 8, 7, 0))


class FakeDevice:
    id, name, capabilities = "front", "Front Room", frozenset({"stream_view"})

    def __init__(self, power="standby", app=None):
        self.power_state, self.app, self.opened, self.closed, self.slept = power, app, [], [], []

    def status(self, quiet=False):
        return {"online": True, "power": self.power_state, "app": self.app}

    def view_stream(self, url, package):
        self.opened.append((url, package))
        self.app = package
        return "opened"

    def close_stream(self, package):
        self.closed.append(package)
        self.app = None
        return "closed"

    def power(self, on):
        self.slept.append(on)


class FakeLog:
    def add(self, *a, **k):
        pass


class FakeRuntime:
    def __init__(self, dev):
        self.dev, self.camera, self.logbook = dev, {}, FakeLog()

    def device(self, dev_id):
        return self.dev

    def jf(self):
        raise RuntimeError("no jellyfin")


@pytest.fixture
def tv(v2, monkeypatch):
    dev = FakeDevice()
    rt = FakeRuntime(dev)
    monkeypatch.setattr(show, "_runtime_device", lambda: (rt, dev))
    monkeypatch.setattr(show.orchestrator, "ensure_on", lambda d, step: step("woke") or {"power": "on"})
    for key, value in {"dash_enabled": "1", "dash_tv": "front", "synchub_url": "http://synchub:5000/", "dash_app": "com.phlox.tvwebbrowser", "dash_days": "mon,tue,wed,thu,fri"}.items():
        settings.set_value(key, value)
    monkeypatch.setattr(show, "RETRY_SECONDS", 0)
    show._last_try["morning"] = 0
    return dev


def test_the_dashboard_opens_in_the_window_once_and_closes_after(tv):
    show.tick(at(2026, 11, 9, 6, 20))
    assert tv.opened == []
    show.tick(at(2026, 11, 9, 6, 31))
    assert len(tv.opened) == 1 and tv.opened[0][0] == f"http://synchub:5000/dash/{config.token()}" and tv.opened[0][1] == "com.phlox.tvwebbrowser"
    show.tick(at(2026, 11, 9, 7, 0))
    assert len(tv.opened) == 1                                                  # not opened again
    show.tick(at(2026, 11, 9, 8, 31))
    assert tv.closed == ["com.phlox.tvwebbrowser"]


def test_the_tv_goes_back_to_sleep_only_if_the_dashboard_woke_it(tv):
    show.tick(at(2026, 11, 9, 6, 31))                       # it was in standby
    show.tick(at(2026, 11, 9, 8, 31))
    assert tv.slept == [False]
    tv.slept.clear(); tv.opened.clear()
    tv.power_state = "on"
    show.tick(at(2026, 11, 10, 6, 31))                      # already on (but nothing playing): stays on afterwards
    show.tick(at(2026, 11, 10, 8, 31))
    assert tv.opened and tv.slept == []


def test_a_tv_showing_something_else_at_the_end_is_not_touched(tv):
    show.tick(at(2026, 11, 9, 6, 31))
    tv.app = "com.netflix.ninja"
    show.tick(at(2026, 11, 9, 8, 31))
    assert tv.closed == [] and tv.slept == []


def test_a_dashboard_somebody_closed_stays_closed(tv):
    show.tick(at(2026, 11, 9, 6, 31))
    show.stop(manual=True, now=at(2026, 11, 9, 6, 40))
    show.tick(at(2026, 11, 9, 7, 0))
    assert len(tv.opened) == 1


def test_a_busy_tv_is_left_alone_for_the_whole_morning(tv, monkeypatch):
    monkeypatch.setattr(show, "busy", lambda r, d, p: '"Cars" is playing')
    show.tick(at(2026, 11, 9, 6, 31))
    show.tick(at(2026, 11, 9, 7, 0))
    assert tv.opened == []
    monkeypatch.setattr(show, "busy", lambda r, d, p: "")
    show.tick(at(2026, 11, 9, 7, 30))
    assert tv.opened == []                                                       # it was decided at 6:31


def test_a_failure_is_remembered_for_the_page_and_retried_a_few_times(tv, monkeypatch):
    settings.set_value("synchub_url", "")
    for minute in (31, 32, 33, 34, 35):
        show.tick(at(2026, 11, 9, 6, minute))
    problem = json.loads(settings.get("dash_problem"))
    assert "SyncHub address" in problem["message"]
    assert json.loads(settings.get("dash_state"))["tries"] == show.MAX_TRIES


def test_nothing_happens_when_switched_off(tv):
    settings.set_value("dash_enabled", "0")
    show.tick(at(2026, 11, 9, 6, 31))
    assert tv.opened == []


# ------------------------------------------------------------------ the page and the settings screen
def test_the_tv_page_needs_its_private_address(client, v2, monkeypatch):
    monkeypatch.setattr(weather.http, "request", lambda *a, **k: Answer(forecast_body()))
    token = config.token()
    assert client.get("/dash/wrong").status_code == 404
    assert client.get("/dash/wrong/data").status_code == 404
    page = client.get(f"/dash/{token}")
    assert page.status_code == 200 and b"/dash/" + token.encode() + b"/data" in page.data
    assert b"cdn" not in page.data.lower() and b"http://" not in page.data and b"https://" not in page.data          # nothing is loaded from outside
    body = client.get(f"/dash/{token}/data").get_json()
    assert set(body) >= {"now", "weather", "people", "quote", "metric"}


def test_data_survives_a_broken_calendar_and_weather(v2, monkeypatch):
    settings.set_value("dash_people", json.dumps([{"name": "Greg", "kind": "calendar", "ics": "https://bad/x.ics", "color": "#4dabf7"}, {"name": "Sam", "kind": "school", "week": {}, "color": "#69db7c"}]))
    monkeypatch.setattr(calendars.http, "request", lambda *a, **k: Answer(None, 404))
    monkeypatch.setattr(weather.http, "request", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(quote.http, "request", lambda *a, **k: 1 / 0)
    calendars._cache.clear()
    built = data.build(at(2026, 11, 9))
    assert "error" in built["people"][0] and built["people"][1]["school"]["state"] == "none"
    assert "error" in built["weather"] and built["quote"]["source"] == "built-in"


def test_settings_page_and_saving(client, v2):
    page = client.get("/settings")
    assert page.status_code == 200 and b'id="pane-dashboard"' in page.data and b"Welcome home" in page.data and b"UniFi Network (Wi-Fi presence)" in page.data
    assert page.data.index(b"unifiNetSettings") < page.data.index(b'id="pane-automation"') < page.data.index(b'id="pane-dashboard"') < page.data.index(b"dashSettings")    # UniFi stays in Connections
    people = json.dumps([{"name": "Sam", "kind": "school", "week": {"mon": {"leave": "07:20", "start": "08:00", "end": ""}}, "mac": "aa:bb:cc:dd:ee:ff"}])
    saved = client.post("/tools/update-settings", data={"tab": "dashboard", "dash_zip": "10001", "dash_units": "metric", "dash_people": people, "dash_start": "06:45"}, follow_redirects=True)
    assert saved.status_code == 200 and b'id="tab-dashboard"' in saved.data
    client.post("/tools/update-settings", data={"tab": "connections", "unifi_net_api_key": "secret-key"})
    assert settings.get("dash_zip") == "10001" and settings.get("dash_units") == "metric" and settings.get("dash_start") == "06:45"
    assert json.loads(settings.get("dash_people"))[0]["week"]["mon"]["leave"] == "07:20"
    assert settings.get("unifi_net_api_key") == "secret-key"
    assert b"secret-key" not in client.get("/settings").data
    client.post("/tools/update-settings", data={"tab": "dashboard", "dash_people": "[{\"name\": \"\"}]"}, follow_redirects=True)
    assert json.loads(settings.get("dash_people"))[0]["name"] == "Sam"          # a bad list is refused and the old one kept


def test_test_buttons(client, v2, monkeypatch):
    monkeypatch.setattr(unifi_network.http, "request", lambda s, e, m, url, **kw: Answer({"data": [{"id": "s"}]}) if url.endswith("/sites") else
                        Answer({"data": [{"macAddress": "aa:bb:cc:00:00:01", "name": "Phone", "type": "WIRELESS"}], "totalCount": 1}))
    reply = client.post("/api/test-connection/unifi_network", json={"fields": {"unifi_net_host": "10.0.0.1", "unifi_net_api_key": "k"}}).get_json()
    assert reply["ok"] and "1 Wi-Fi device" in reply["message"]
    monkeypatch.setattr(calendars.http, "request", lambda *a, **k: Answer(None, 403))
    assert not client.post("/api/dashboard/test_calendar", json={"url": "https://x/c.ics"}).get_json()["ok"]
    assert not client.post("/api/dashboard/test_calendar", json={}).get_json()["ok"]
    monkeypatch.setattr(unifi_network.http, "request", lambda *a, **k: Answer({}, 401))
    settings.set_value("unifi_net_host", "10.0.0.1")
    settings.set_value("unifi_net_api_key", "k")
    assert "Network application" in client.get("/api/dashboard/clients").get_json()["message"]


# ------------------------------------------------------------------ per-student no-school days and school calendars
SCHOOL_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//t//EN
BEGIN:VEVENT
UID:a
DTSTART;VALUE=DATE:20261109
DTEND;VALUE=DATE:20261110
SUMMARY:Student Holiday - No School
END:VEVENT
BEGIN:VEVENT
UID:b
DTSTART;VALUE=DATE:20261116
DTEND;VALUE=DATE:20261117
SUMMARY:Minimum Day
END:VEVENT
BEGIN:VEVENT
UID:c
DTSTART:20261117T150000Z
DTEND:20261117T160000Z
SUMMARY:Lunch break with the principal
END:VEVENT
END:VCALENDAR
"""


def test_a_students_own_no_school_days_do_not_close_the_others(v2):
    sam = {**KID, "no_school": "2026-11-09 Ponderosa day"}
    alex = {"name": "Alex", "kind": "school", "week": KID["week"]}
    assert school.day_plan(sam, at(2026, 11, 9)) == {"state": "holiday", "label": "Ponderosa day"}
    assert school.day_plan(alex, at(2026, 11, 9))["state"] == "school"
    settings.set_value("dash_no_school", "2026-11-09 Everyone")
    assert school.day_plan(alex, at(2026, 11, 9))["label"] == "Everyone"


def test_a_school_calendar_can_close_a_day_or_add_a_note(v2, monkeypatch):
    monkeypatch.setattr(calendars, "download", lambda url: (SCHOOL_ICS, False))
    kid = {**KID, "ics": "https://school/cal.ics", "week": {"mon": KID["week"]["mon"]}}
    assert school.day_plan(kid, at(2026, 11, 9)) == {"state": "holiday", "label": "Student Holiday - No School"}
    note = school.day_plan(kid, at(2026, 11, 16))
    assert note["state"] == "school" and note["note"] == "Minimum Day"
    assert school.day_plan(kid, at(2026, 11, 23))["note"] == ""
    monkeypatch.setattr(calendars, "download", lambda url: 1 / 0)
    assert school.day_plan(kid, at(2026, 11, 9))["state"] == "school"          # an unreadable calendar changes nothing


def test_a_timed_event_with_break_in_its_name_does_not_close_the_school(v2, monkeypatch):
    monkeypatch.setattr(calendars, "download", lambda url: (SCHOOL_ICS, False))
    kid = {**KID, "ics": "https://school/cal.ics", "week": {"tue": KID["week"]["mon"]}}
    assert school.day_plan(kid, at(2026, 11, 17))["state"] == "school"


def test_student_calendar_and_no_school_list_are_validated():
    ok, text = config.clean_people(json.dumps([{"name": "Sam", "kind": "school", "ics": "webcal://s.example/c.ics", "no_school": "2026-11-26 to 2026-11-27 Thanksgiving break"}]))
    assert ok and json.loads(text)[0]["ics"] == "https://s.example/c.ics" and json.loads(text)[0]["no_school"].startswith("2026-11-26 to")
    assert not config.clean_people(json.dumps([{"name": "Sam", "kind": "school", "no_school": "someday"}]))[0]
    assert not config.clean_people(json.dumps([{"name": "Sam", "kind": "school", "ics": "ftp://x"}]))[0]


# ------------------------------------------------------------------ extras: alerts, hints, countdowns, everyone left
def test_hint_and_alerts(v2, monkeypatch):
    assert weather.hint(38, [10], 3, False) == "Cold this morning: take a jacket"
    assert weather.hint(60, [10, 70], 3, False) == "Take an umbrella"
    assert weather.hint(20, [0], 73, False) == "Snow today: bundle up"
    assert weather.hint(95, [0], 0, False) == "Hot today: bring water"
    assert weather.hint(65, [10], 0, False) == "" and weather.hint(5, [], 0, True) == "Cold this morning: take a jacket"
    feats = {"features": [{"properties": {"event": "Wind Advisory", "severity": "Minor", "headline": "x"}},
                          {"properties": {"event": "Flood Warning", "severity": "Severe", "headline": "River flooding"}},
                          {"properties": {"event": "Red Flag Warning", "severity": "Moderate", "headline": "Fire weather"}}]}
    monkeypatch.setattr(weather.http, "request", lambda *a, **k: Answer(feats))
    got = weather.alerts({"lat": 33.8, "lon": -117.9})
    assert [a["event"] for a in got] == ["Flood Warning", "Red Flag Warning"]
    monkeypatch.setattr(weather.http, "request", lambda *a, **k: 1 / 0)
    assert weather.alerts({"lat": 1, "lon": 2}) == []


def test_countdowns(v2):
    ok, text = config.clean_countdowns("2026-12-21 Winter break\n03-14 Sam's birthday\n02-29 Leap day")
    assert ok and text.splitlines()[1] == "03-14 Sam's birthday"
    assert not config.clean_countdowns("sometime Soon")[0] and not config.clean_countdowns("13-45 Nope")[0]
    settings.set_value("dash_countdowns", "2026-11-12 Field trip\n11-09 Birthday\n2026-10-01 Past\n2027-03-01 Far away\n11-08 Today thing")
    got = config.countdowns(date(2026, 11, 8))
    assert [(c["label"], c["days"]) for c in got] == [("Today thing", 0), ("Birthday", 1), ("Field trip", 4)]
    settings.set_value("dash_countdowns", "02-29 Leap day")
    assert config.countdowns(date(2027, 12, 20), horizon=120)[0]["date"] == "2028-02-29"         # a yearly 29 February shows in leap years
    assert config.countdowns(date(2026, 12, 20), horizon=120) == []                                # and nothing breaks in the others


def test_everyone_left_and_closing_early(tv, monkeypatch):
    settings.set_value("dash_people", json.dumps([KID, {**KID, "name": "Alex", "mac": "aa:bb:cc:dd:ee:01"}]))
    morning = at(2026, 11, 9, 7, 0)
    school.save_state({"day": "2026-11-09", "kids": {"Sam": {"state": "left"}, "Alex": {"state": "home"}}, "error": ""})
    assert not school.all_left(morning)
    show.tick(at(2026, 11, 9, 6, 31))
    settings.set_value("dash_close_when_left", "1")
    show.tick(at(2026, 11, 9, 7, 0))
    assert tv.closed == []                                                          # Alex is still home
    school.save_state({"day": "2026-11-09", "kids": {"Sam": {"state": "left"}, "Alex": {"state": "left"}}, "error": ""})
    assert school.all_left(morning)
    show.tick(at(2026, 11, 9, 7, 5))
    assert tv.closed == ["com.phlox.tvwebbrowser"] and len(tv.opened) == 1
    show.tick(at(2026, 11, 9, 7, 10))
    assert len(tv.opened) == 1                                                      # not opened again this morning
    assert data.build(morning)["all_left"] is True


def test_the_tv_page_carries_the_new_pieces(client, v2, monkeypatch):
    monkeypatch.setattr(weather.http, "request", lambda *a, **k: Answer(forecast_body()))
    settings.set_value("dash_zip", "40.71, -74.00")
    settings.set_value("dash_countdowns", "11-09 Birthday")
    body = client.get(f"/dash/{config.token()}/data").get_json()
    assert "alerts" in body["weather"] and "hint" in body["weather"] and "countdowns" in body and "all_left" in body


# ------------------------------------------------------------------ phase 2: people with phones, Welcome home, the evening screen, extras
from synchub.dashboard import home, welcome          # noqa: E402

ADULT = {"name": "Alex", "kind": "calendar", "ics": "", "color": "#f783ac", "mac": "aa:bb:cc:dd:ee:02", "welcome": True, "welcome_text": "Dinner at 6"}


def test_people_get_a_phone_and_a_welcome_switch():
    ok, text = config.clean_people(json.dumps([{"name": "Alex", "kind": "calendar", "mac": "AA-BB-CC-DD-EE-02", "welcome_text": " Dinner at 6 "}, {"name": "Jo", "kind": "calendar", "welcome": False}]))
    saved = json.loads(text)
    assert ok and saved[0]["mac"] == "aa:bb:cc:dd:ee:02" and saved[0]["welcome"] is True and saved[0]["welcome_text"] == "Dinner at 6"
    assert saved[1]["welcome"] is False and saved[1]["mac"] == ""
    assert not config.clean_people(json.dumps([{"name": "Alex", "kind": "calendar", "mac": "12:34"}]))[0]


def test_quiet_hours_may_run_past_midnight(v2):
    assert config.quiet(at(2026, 11, 9, 23, 0)) and config.quiet(at(2026, 11, 9, 5, 59)) and not config.quiet(at(2026, 11, 9, 6, 0)) and not config.quiet(at(2026, 11, 9, 15, 0))
    settings.set_value("dash_welcome_quiet_on", "0")
    assert not config.quiet(at(2026, 11, 9, 23, 0))


def test_birthdays_come_from_the_countdowns(v2):
    settings.set_value("dash_countdowns", "11-09 Sam's birthday\n11-10 Alex's birthday\n11-09 Field trip")
    assert config.birthdays(date(2026, 11, 9)) == ["Sam"]


def test_arrival_needs_the_phone_to_have_been_away_long_enough():
    now = at(2026, 11, 9, 15, 0)
    rec, came = welcome.step(None, True, now, 15)
    assert rec["home"] is True and not came                                       # first sight: no welcome
    rec, came = welcome.step(rec, False, at(2026, 11, 9, 15, 5), 15)
    assert rec["home"] is False and rec["away_since"] and not came
    _, came = welcome.step(rec, True, at(2026, 11, 9, 15, 12), 15)
    assert not came                                                               # a nap of 7 minutes
    rec, came = welcome.step(rec, True, at(2026, 11, 9, 15, 21), 15)
    assert came and rec["away_since"] is None
    assert welcome.step({}, False, now, 15)[0].get("home") is None                # never seen: stays unknown


@pytest.fixture
def arrival(tv):
    """A TV in standby, UniFi set up, Alex with a phone, Welcome home on."""
    for key, value in {"dash_welcome_on": "1", "unifi_net_host": "10.0.0.1", "unifi_net_api_key": "k", "dash_people": json.dumps([ADULT]), "dash_zip": ""}.items():
        settings.set_value(key, value)
    return FakeNetwork({ADULT["mac"]}), FakeNetwork(set())


def arrive(tv_dev, here, away, leave=(8, 0), back=(16, 0)):
    welcome.poll(at(2026, 11, 9, 7, 30), here)
    welcome.poll(at(2026, 11, 9, *leave), away)
    return welcome.poll(at(2026, 11, 9, *back), here)


def test_coming_home_wakes_an_idle_tv_and_shows_the_welcome_then_puts_it_back(arrival, tv):
    here, away = arrival
    state = arrive(tv, FakeNetwork({"aa:bb:cc:dd:ee:02"}), away)
    assert len(tv.opened) == 1 and tv.opened[0][0] == f"http://synchub:5000/dash/{config.token()}"
    assert "Welcome home shown for Alex" in state["log"][0]["message"]
    page = welcome.page(at(2026, 11, 9, 16, 0, ), None)
    assert page["title"] == "Welcome home, Alex!" and page["people"][0]["lines"] == ["Dinner at 6"] and not page["birthday"]
    assert data.build(at(2026, 11, 9, 16, 0))["welcome"]["title"] == "Welcome home, Alex!"
    welcome.tick(at(2026, 11, 9, 16, 1))
    assert tv.closed == []                                                        # 2 minutes by default
    welcome.tick(at(2026, 11, 9, 16, 2, ))
    assert tv.closed == ["com.phlox.tvwebbrowser"] and tv.slept == [False] and welcome.current(at(2026, 11, 9, 16, 3)) is None
    assert data.build(at(2026, 11, 9, 16, 3))["welcome"] is None


def test_a_phone_that_never_left_does_not_trigger_a_welcome(arrival, tv):
    here, _ = arrival
    on = FakeNetwork({"aa:bb:cc:dd:ee:02"})
    for minute in (0, 5, 10):
        welcome.poll(at(2026, 11, 9, 16, minute), on)
    assert tv.opened == []


def test_welcome_respects_quiet_hours_the_gap_the_person_and_the_switch(arrival, tv):
    on, away = FakeNetwork({"aa:bb:cc:dd:ee:02"}), FakeNetwork(set())
    state = arrive(tv, on, away, leave=(20, 0), back=(23, 0))
    assert tv.opened == [] and "quiet hours" in state["log"][0]["message"]
    state = arrive(tv, on, away, leave=(10, 0), back=(10, 30))                    # shown at 10:30
    assert len(tv.opened) == 1
    welcome.tick(at(2026, 11, 9, 10, 33))
    welcome.poll(at(2026, 11, 9, 10, 35), away)
    state = welcome.poll(at(2026, 11, 9, 10, 55), on)                             # away 20 minutes, but shown 25 minutes ago
    assert len(tv.opened) == 1 and "less than 60 minutes ago" in state["log"][0]["message"]
    settings.set_value("dash_people", json.dumps([{**ADULT, "welcome": False}]))
    assert welcome.poll(at(2026, 11, 9, 13, 0), away) is None and welcome.poll(at(2026, 11, 9, 14, 0), on) is None      # not watched at all


def test_welcome_leaves_a_busy_tv_alone_and_can_be_told_not_to_wake_it(arrival, tv, monkeypatch):
    on, away = FakeNetwork({"aa:bb:cc:dd:ee:02"}), FakeNetwork(set())
    settings.set_value("dash_welcome_wake", "0")
    state = arrive(tv, on, away)
    assert tv.opened == [] and "waking it is switched off" in state["log"][0]["message"]
    settings.set_value("dash_welcome_wake", "1")
    monkeypatch.setattr(show, "busy", lambda r, d, p: '"Cars" is playing')
    welcome.poll(at(2026, 11, 9, 18, 0), away)
    state = welcome.poll(at(2026, 11, 9, 19, 0), on)
    assert tv.opened == [] and 'the TV is busy: "Cars" is playing' in state["log"][0]["message"]


def test_two_people_arriving_get_one_page_and_a_birthday_says_so(arrival, tv):
    sam = {**KID, "welcome": True, "week": {"mon": {"leave": "07:20", "start": "08:00", "end": ""}, "tue": {"leave": "07:20", "start": "08:00", "end": ""}}}
    settings.set_value("dash_people", json.dumps([ADULT, sam]))
    settings.set_value("dash_countdowns", "11-09 Sam's birthday")
    both = FakeNetwork({"aa:bb:cc:dd:ee:02", sam["mac"]})
    welcome.poll(at(2026, 11, 9, 7, 30), both)
    welcome.poll(at(2026, 11, 9, 8, 0), FakeNetwork(set()))
    welcome.poll(at(2026, 11, 9, 16, 0), both)
    assert len(tv.opened) == 1
    page = welcome.page(at(2026, 11, 9, 16, 0), None)
    assert page["title"] == "Welcome home, Alex and Sam!" and page["birthday"]
    assert page["people"][1]["lines"][0] == "Happy birthday, Sam!" and any("Tomorrow: leave by 7:20 AM" in line for line in page["people"][1]["lines"])
    assert data.build(at(2026, 11, 9, 16, 0))["birthdays"] == ["Sam"]


def test_a_welcome_on_top_of_the_morning_screen_leaves_the_screen_alone(arrival, tv):
    show.tick(at(2026, 11, 9, 6, 31))
    assert len(tv.opened) == 1
    settings.set_value("dash_welcome_quiet_on", "0")
    state = arrive(tv, FakeNetwork({"aa:bb:cc:dd:ee:02"}), FakeNetwork(set()), leave=(7, 0), back=(7, 30))
    assert len(tv.opened) == 1 and "Welcome home shown" in state["log"][0]["message"]
    welcome.tick(at(2026, 11, 9, 7, 33))
    assert tv.closed == []                                                        # the morning screen has its own end


def test_one_unifi_call_serves_both_the_school_rules_and_the_welcome(arrival, tv):
    calls = []

    class Counting:
        def online_macs(self):
            calls.append(1)
            return set()
    settings.set_value("dash_people", json.dumps([ADULT, KID]))
    home.poll(at(2026, 11, 9, 7, 0), Counting())
    assert len(calls) == 1


def test_welcome_test_button_and_status(client, arrival, tv):
    reply = client.post("/api/tv/dashboard/welcome_test").get_json()
    assert reply["ok"] and len(tv.opened) == 1
    status = client.get("/api/dashboard/status").get_json()
    assert status["shown"] is False and status["welcome_now"] == ["Alex"]
    assert client.post("/api/tv/dashboard/hide").get_json()["ok"]
    assert client.get("/api/dashboard/status").get_json()["welcome_now"] == []


# ------------------------------------------------------------------ the evening screen
def test_the_evening_screen_opens_in_its_own_window_and_is_separate_from_the_morning(tv):
    settings.set_value("dash_eve_on", "1")
    show.tick(at(2026, 11, 9, 19, 59))
    assert tv.opened == []
    show.tick(at(2026, 11, 9, 20, 1))
    assert len(tv.opened) == 1 and tv.opened[0][0].endswith("?mode=evening")
    assert show.current_mode(at(2026, 11, 9, 21, 0)) == "evening" and show.current_mode(at(2026, 11, 9, 7, 0)) == "morning"
    show.tick(at(2026, 11, 9, 21, 0))
    assert len(tv.opened) == 1
    show.tick(at(2026, 11, 9, 22, 1))
    assert tv.closed == ["com.phlox.tvwebbrowser"]
    tv.opened.clear()
    show.tick(at(2026, 11, 14, 20, 30))                                           # a Saturday: not an evening day
    assert tv.opened == []
    settings.set_value("dash_eve_on", "0")
    show.tick(at(2026, 11, 10, 20, 30))
    assert tv.opened == []


def test_the_evening_data_is_about_tomorrow(v2, monkeypatch):
    sam = {**KID, "week": {"mon": {"leave": "07:20", "start": "08:00", "end": ""}, "tue": {"leave": "07:00", "start": "07:45", "end": "14:30"}}}
    settings.set_value("dash_people", json.dumps([sam, {"name": "Greg", "kind": "calendar", "ics": "https://x/c.ics", "color": "#4dabf7"}]))
    settings.set_value("dash_zip", "40.71, -74.00")
    settings.set_value("dash_quote_on", "0")
    settings.set_value("dash_eve_on", "1")
    ics = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\nUID:a\nDTSTART:20261110T150000Z\nDTEND:20261110T160000Z\nSUMMARY:Dentist\nEND:VEVENT\nEND:VCALENDAR\n"
    monkeypatch.setattr(calendars, "download", lambda url: (ics, False))
    body = forecast_body()
    body["daily"]["precipitation_probability_max"] = [70, 80]
    monkeypatch.setattr(weather.http, "request", lambda *a, **k: Answer(body))
    monkeypatch.setitem(weather._cache, "key", None)
    built = data.build(at(2026, 11, 9, 20, 30))
    assert built["mode"] == "evening"
    assert built["people"][0]["school"]["leave"].startswith("2026-11-10T07:00") and built["people"][0]["school"]["presence"] == ""
    assert [e["title"] for e in built["people"][1]["tomorrow"]] == ["Dentist"]
    assert built["weather"]["tomorrow_hint"] == "Rain tomorrow: pack umbrellas" and built["weather"]["moon"]["name"]
    assert data.build(at(2026, 11, 9, 20, 30), mode="morning")["mode"] == "morning" and data.build(at(2026, 11, 9, 7, 0))["mode"] == "morning"


# ------------------------------------------------------------------ weather extras and the quote source
def test_moon_uv_and_air_quality_words():
    assert weather.moon(date(2000, 1, 6))["name"] == "New moon" and weather.moon(date(2000, 1, 21))["name"] == "Full moon"
    assert weather.moon(date(2026, 11, 9))["symbol"]
    assert weather.uv_note(7) == "UV high: sunscreen" and "very high" in weather.uv_note(9) and weather.uv_note(3) == "" and weather.uv_note(None) == ""
    assert weather.aqi_label(42) == "Good" and weather.aqi_label(120) == "Unhealthy for sensitive groups" and weather.aqi_label(None) == ""
    day = {"high": 60, "low": 40, "rain": 10, "code": 1}
    assert weather.tomorrow_hint(day, False) == "Cold tomorrow morning: jackets" and weather.tomorrow_hint({**day, "low": 50}, False) == "" and weather.tomorrow_hint(None, False) == ""
    assert weather.tomorrow_hint({**day, "code": 73}, False).startswith("Snow")


def test_the_line_at_the_bottom_can_be_a_joke_a_fact_history_or_a_rotation(v2, monkeypatch):
    day = date(2026, 11, 9)
    monkeypatch.setattr(quote.http, "request", lambda *a, **k: 1 / 0)
    for source in ("joke", "fact", "history"):
        got = quote.today(day, source)
        assert got["kind"] == source and got["source"] == "built-in" and got["label"]
    assert quote.today(day, "quote")["label"] == ""
    assert [quote.kind_for(date(2026, 11, d), "rotate") for d in (9, 10, 11, 12)].count("quote") == 1 and quote.kind_for(day, "bogus") == "quote"
    monkeypatch.setattr(quote.http, "request", lambda s, e, m, url, **k: Answer({"joke": "Why did the chicken cross?"}) if "dadjoke" in url else Answer({}, 500))
    assert quote.today(date(2026, 11, 10), "joke")["text"] == "Why did the chicken cross?"
    monkeypatch.setattr(quote.http, "request", lambda s, e, m, url, **k: Answer({"data": {"Events": [{"year": "1969", "text": "Apollo 11 landed."}]}}))
    assert quote.today(date(2026, 11, 11), "history")["text"] == "1969: Apollo 11 landed."
    settings.set_value("dash_quote_source", "fact")
    assert quote.today(date(2026, 11, 11))["kind"] == "fact"                      # the choice changed: not the saved joke


def test_the_new_settings_are_checked():
    assert validate_setting("dash_welcome_seconds", "120") == (True, "120") and not validate_setting("dash_welcome_seconds", "5")[0]
    assert not validate_setting("dash_welcome_away", "0")[0] and validate_setting("dash_welcome_gap", "0")[0]
    assert validate_setting("dash_eve_start", "20:00")[0] and not validate_setting("dash_eve_end", "9pm")[0] and not validate_setting("dash_welcome_quiet_from", "25:00")[0]
    assert validate_setting("dash_eve_days", "thu,sun,xyz") == (True, "thu,sun".replace("thu,sun", "thu,sun"))
    assert validate_setting("dash_quote_source", "rotate")[0] and not validate_setting("dash_quote_source", "news")[0]


def test_the_tab_saves_on_its_own_and_unifi_stays_with_connections(client, v2):
    page = client.get("/settings?tab=dashboard").data
    assert b'class="nav-link active" id="tab-dashboard"' in page and b"TV dashboard" in page
    client.post("/tools/update-settings", data={"tab": "dashboard", "dash_welcome_on": "1", "dash_welcome_seconds": "90", "dash_eve_on": "1", "dash_quote_source": "joke"})
    assert settings.get("dash_welcome_seconds") == "90" and settings.get("dash_welcome_on") == "1" and settings.get("dash_eve_on") == "1" and settings.get("dash_quote_source") == "joke"
    client.post("/tools/update-settings", data={"tab": "connections", "unifi_net_host": "10.0.0.5"})
    assert settings.get("dash_welcome_seconds") == "90"                           # the other tab's save did not touch it


def test_a_screen_shown_by_hand_outside_the_window_stays_up_for_a_while(tv):
    show.start(manual=True, now=at(2026, 11, 9, 6, 15))                      # before the 6:30 window
    show.tick(at(2026, 11, 9, 6, 16))
    show.tick(at(2026, 11, 9, 6, 29))
    assert tv.closed == []
    show.tick(at(2026, 11, 9, 6, 31))                                          # in the window now: no second opening, still up
    assert len(tv.opened) == 1 and tv.closed == []
    show.start(manual=True, now=at(2026, 11, 9, 14, 0))
    show.tick(at(2026, 11, 9, 14, 29))
    assert tv.closed == []
    show.tick(at(2026, 11, 9, 14, 31))
    assert tv.closed == ["com.phlox.tvwebbrowser"]


# ------------------------------------------------------------------ speed: a big calendar is read once, the page's answer is reused for a few seconds
def test_a_calendar_is_parsed_once_however_many_times_it_is_asked(monkeypatch):
    import icalendar
    calendars._parsed.clear()
    calendars._found.clear()
    reads = []
    real = icalendar.Calendar.from_ical
    monkeypatch.setattr(icalendar.Calendar, "from_ical", classmethod(lambda cls, text: reads.append(1) or real(text)))
    monkeypatch.setattr(calendars, "download", lambda url: (ICS, False))
    for _ in range(3):
        calendars.for_person("https://x/y.ics", at(2026, 11, 9, 6, 30))
        calendars.for_day("https://x/y.ics", at(2026, 11, 10, 6, 30).date(), at(2026, 11, 9).tzinfo)
    assert len(reads) == 1


def test_the_page_answer_is_reused_for_a_few_seconds_and_a_setting_change_shows_at_once(v2, monkeypatch):
    built = []
    monkeypatch.setattr(data, "build", lambda now=None, mode=None: built.append(1) or {"n": len(built)})
    data._last.clear()
    assert data.cached() == data.cached() == {"n": 1} and len(built) == 1
    settings.set_value("dash_zip", "90210")
    assert data.cached() == {"n": 2}


def test_trimming_a_big_calendar_gives_the_same_events_as_reading_all_of_it():
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for i in range(300):                                        # a few years of single events
        d = date(2023, 1, 1) + timedelta(days=i * 4)
        lines += ["BEGIN:VEVENT", f"UID:old{i}", f"DTSTART:{d:%Y%m%d}T150000Z", f"DTEND:{d:%Y%m%d}T160000Z", f"SUMMARY:Old {i}", "END:VEVENT"]
    lines += ["BEGIN:VEVENT", "UID:trip", "DTSTART;VALUE=DATE:20261105", "DTEND;VALUE=DATE:20261112", "SUMMARY:Trip across the day", "END:VEVENT",
              "BEGIN:VEVENT", "UID:long", "DTSTART:20261101T090000Z", "DURATION:P10D", "SUMMARY:Long duration", "END:VEVENT",
              "BEGIN:VEVENT", "UID:soccer", "DTSTART;TZID=America/Los_Angeles:20230103T170000", "DTEND;TZID=America/Los_Angeles:20230103T180000", "RRULE:FREQ=WEEKLY;BYDAY=MO", "SUMMARY:Practice", "END:VEVENT",
              "BEGIN:VEVENT", "UID:today", "DTSTART:20261109T180000Z", "DTEND:20261109T190000Z", "SUMMARY:Today", "END:VEVENT", "END:VCALENDAR"]
    text = "\r\n".join(lines)
    start = at(2026, 11, 9)
    end = start + timedelta(days=8)
    trimmed = calendars.trim_ics(text, start.date(), end.date())
    assert len(trimmed) < len(text) / 3 and "Trip across the day" in trimmed and "Long duration" in trimmed and "Practice" in trimmed and "Old 5" not in trimmed
    calendars._parsed.clear()
    calendars._trimmed.clear()
    fast = calendars._events_between(text, start, end)
    calendars._parsed.clear()
    original = calendars.trimmed
    calendars.trimmed = lambda t, s, e: t
    try:
        slow = calendars._events_between(text, start, end)
    finally:
        calendars.trimmed = original
        calendars._parsed.clear()
    assert fast == slow and {e["title"] for e in fast} >= {"Today", "Trip across the day", "Long duration", "Practice"}
