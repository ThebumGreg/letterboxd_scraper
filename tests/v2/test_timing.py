"""Page timing: the Server-Timing header, the in-memory report, the Tools card and the pages that are left out."""
from synchub import timing


def test_pages_carry_a_server_timing_header_and_show_in_the_report(client, v2):
    timing.reset()
    response = client.get("/library")
    header = response.headers["Server-Timing"]
    assert "total;dur=" in header and "db;dur=" in header and "queries" in header
    report = client.get("/api/tools/timings").get_json()
    page = next(p for p in report["pages"] if p["path"] == "/library")
    assert page["count"] == 1 and page["avg_queries"] > 0 and page["max_ms"] > 0
    assert report["slowest"][0]["status"] == 200


def test_static_files_and_worker_polling_are_not_timed(client, v2):
    timing.reset()
    client.get("/static/does-not-exist.css")
    client.post("/api/worker/v1/heartbeat", json={})
    assert timing.report()["count"] == 0


def test_a_slow_page_is_logged_and_clear_empties_the_report(client, v2, monkeypatch, caplog):
    timing.reset()
    monkeypatch.setattr(timing, "SLOW_MS", 0)
    with caplog.at_level("WARNING"):
        client.get("/")
    assert any("Slow page" in r.getMessage() for r in caplog.records)
    assert timing.report()["count"] == 1
    assert client.post("/api/tools/timings/reset", json={}).get_json()["ok"]
    assert [r["path"] for r in timing.report()["slowest"]] == ["/api/tools/timings/reset"]      # only the request that cleared it


def test_the_tools_page_has_the_timings_card(client, v2):
    assert "Page timings" in client.get("/tools").get_data(as_text=True)
