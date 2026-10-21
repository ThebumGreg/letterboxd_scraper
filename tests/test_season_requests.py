import automation


class FakeSonarr:
    def __init__(self, series):
        self.series = series
        self.updated = None
        self.searches = []
        self.added = None

    def install(self, monkeypatch):
        monkeypatch.setattr(automation.sonarr_client, "get_series_by_tvdb", lambda tvdb: self.series)
        monkeypatch.setattr(automation.sonarr_client, "update_series", lambda sid, payload: setattr(self, "updated", (sid, payload)) or payload)
        monkeypatch.setattr(automation.sonarr_client, "search_season", lambda sid, sn: self.searches.append((sid, sn)) or {"status": "success"})
        monkeypatch.setattr(automation.sonarr_client, "add_series", lambda **kw: setattr(self, "added", kw) or {"status": "added"})


def _series():
    return {"id": 42, "monitored": False, "seasons": [
        {"seasonNumber": 0, "monitored": False},
        {"seasonNumber": 1, "monitored": True},
        {"seasonNumber": 2, "monitored": False},
        {"seasonNumber": 3, "monitored": True},
    ]}


def test_requesting_a_season_keeps_other_monitored_seasons(monkeypatch):
    fake = FakeSonarr(_series()); fake.install(monkeypatch)

    result = automation.request_tv_seasons("123", [2])

    assert result["status"] == "updated"
    monitored = {s["seasonNumber"]: s["monitored"] for s in fake.updated[1]["seasons"]}
    assert monitored == {0: False, 1: True, 2: True, 3: True}
    assert fake.updated[1]["monitored"] is True


def test_requested_seasons_are_searched(monkeypatch):
    fake = FakeSonarr(_series()); fake.install(monkeypatch)

    automation.request_tv_seasons("123", [2, "3"])

    assert fake.searches == [(42, 2), (42, 3)]


def test_search_failure_does_not_fail_the_request(monkeypatch):
    fake = FakeSonarr(_series()); fake.install(monkeypatch)
    monkeypatch.setattr(automation.sonarr_client, "search_season", lambda sid, sn: {"status": "error", "message": "HTTP 500"})

    result = automation.request_tv_seasons("123", [2])

    assert result["status"] == "updated" and result["search_errors"] == ["S2: HTTP 500"]


def test_new_series_is_added_with_only_requested_seasons(monkeypatch):
    fake = FakeSonarr(None); fake.install(monkeypatch)

    automation.request_tv_seasons("123", [2, 1])

    assert fake.added["monitored_seasons"] == [1, 2] and fake.added["tvdb_id"] == "123"


def test_invalid_season_numbers_are_rejected(monkeypatch):
    fake = FakeSonarr(_series()); fake.install(monkeypatch)
    assert automation.request_tv_seasons("123", ["x"])["status"] == "error"
    assert fake.updated is None


def test_sonarr_client_search_season_payload(monkeypatch):
    import sonarr_client
    sent = {}

    class Resp:
        status_code = 201
        def json(self): return {"id": 9}

    monkeypatch.setattr(sonarr_client, "_conn", lambda: ("http://sonarr", {"X-Api-Key": "k"}))
    monkeypatch.setattr(sonarr_client, "logged_request", lambda service, event, method, url, **kw: sent.update(method=method, url=url, json=kw["json"]) or Resp())

    assert sonarr_client.search_season("42", 2) == {"status": "success", "command_id": 9}
    assert sent == {"method": "POST", "url": "http://sonarr/api/v3/command", "json": {"name": "SeasonSearch", "seriesId": 42, "seasonNumber": 2}}
