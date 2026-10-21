import logging

import letterboxd_scraper

HTML = """<html><body>
<div class="react-component" data-item-slug="heat-1995" data-item-full-display-name="Heat (1995)"></div>
</body></html>"""


class FakeResponse:
    status_code = 200
    text = HTML


def test_scraper_logs_instead_of_printing(db, monkeypatch, caplog, capsys):
    monkeypatch.setattr(letterboxd_scraper.requests, "get", lambda *a, **k: FakeResponse())

    with caplog.at_level(logging.INFO):
        result = letterboxd_scraper.scrape_letterboxd_page("https://letterboxd.com/u/watchlist/", "Watchlist", "u", None)

    assert result == "end_of_pagination"
    assert capsys.readouterr().out == ""
    messages = [r.getMessage() for r in caplog.records]
    assert any("Found movie" in m and "heat-1995" in m for m in messages)
    assert any("Successfully processed 1 movies" in m for m in messages)


def test_blocked_response_is_logged_as_warning(db, monkeypatch, caplog):
    class Blocked:
        status_code = 429
        text = ""
    monkeypatch.setattr(letterboxd_scraper.requests, "get", lambda *a, **k: Blocked())

    with caplog.at_level(logging.WARNING):
        assert letterboxd_scraper.scrape_letterboxd_page("https://letterboxd.com/u/films/", "Watched", "u", None) == "abort_scrape"

    assert any(r.levelno == logging.WARNING and "429" in r.getMessage() for r in caplog.records)


def test_removed_watchlist_items_are_kept(db, monkeypatch):
    """Items removed from Letterboxd stay in the database (intended)."""
    monkeypatch.setattr(letterboxd_scraper.requests, "get", lambda *a, **k: FakeResponse())
    letterboxd_scraper.scrape_letterboxd_page("u", "Watchlist", "u", None)

    class Empty:
        status_code = 200
        text = "<html><body></body></html>"
    monkeypatch.setattr(letterboxd_scraper.requests, "get", lambda *a, **k: Empty())
    letterboxd_scraper.scrape_letterboxd_page("u", "Watchlist", "u", None)

    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_requests WHERE movie_slug = 'heat-1995'").fetchone()[0] == 1
