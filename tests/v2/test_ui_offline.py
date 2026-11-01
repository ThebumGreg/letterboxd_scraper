"""The pages work with no internet (Bootstrap and its icons are served by SyncHub itself) and a few small UI rules."""
import re
from pathlib import Path

from synchub import status

STATIC = Path(__file__).resolve().parents[2] / "static"


def test_a_page_loads_no_script_style_or_font_from_another_site(client):
    page = client.get("/").get_data(as_text=True)
    assert not re.findall(r'<(?:link|script)[^>]+(?:href|src)="https?://', page)
    for path in re.findall(r'(?:href|src)="(/static/vendor/[^"?]+)', page):
        assert (STATIC.parent / path.lstrip("/")).is_file(), path
    assert "/static/vendor/bootstrap/bootstrap.min.css" in page and "/static/vendor/bootstrap/bootstrap.bundle.min.js" in page


def test_the_icon_fonts_the_icon_stylesheet_names_are_present():
    css = (STATIC / "vendor/bootstrap-icons/bootstrap-icons.min.css").read_text()
    for name in set(re.findall(r'url\("?\./?(fonts/[^"?)]+)', css)):
        assert (STATIC / "vendor/bootstrap-icons" / name).is_file(), name


def test_the_attention_line_reads_correctly_for_one_item(monkeypatch):
    monkeypatch.setattr(status, "_connection", lambda *a, **k: {"label": "X", "state": status.BAD, "detail": "", "href": "/"})
    monkeypatch.setattr(status, "CONNECTIONS", [("Jellyfin", "jellyfin", 1)])
    monkeypatch.setattr(status, "_dvr_items", lambda c: [])
    monkeypatch.setattr(status, "_worker_items", lambda c: [])
    monkeypatch.setattr(status, "_queue_items", lambda c: [])
    monkeypatch.setattr(status, "_disk_items", lambda: [])
    assert status.snapshot(None)["summary"] == "1 thing needs attention"


def test_no_badge_is_dark_on_the_dark_page():
    templates = Path(__file__).resolve().parents[2] / "synchub/web/templates"
    bad = [f.name for f in templates.glob("*.html") if re.search(r'class="badge bg-dark"|bg-\{\{ \'dark\'', f.read_text())]
    assert not bad, bad


def test_the_jobs_summary_counts_cancelled_jobs_and_a_finished_job_says_finished(client, v2):
    from synchub import jobs
    with v2.connection() as conn:
        done, cancelled = jobs.create(conn, "library", title="Done one"), jobs.create(conn, "library", title="Cancelled one")
        conn.execute("UPDATE job SET status = 'done', stage = 'done', message = 'Verifying: 1016 MB of 1.1 GB' WHERE id = ?", (done,))
        conn.execute("UPDATE job SET status = 'cancelled' WHERE id = ?", (cancelled,))
    page = client.get("/jobs").get_data(as_text=True)
    assert "1 done" in page and "1 cancelled" in page
    assert "Finished." in page and "Verifying: 1016 MB" not in page


def test_wide_tables_stack_on_a_phone(client):
    page = client.get("/jobs").get_data(as_text=True)
    assert 'class="table table-hover align-middle mb-0 stack-sm" id="jobTable"' in page
    assert "stack-sm" in (STATIC / "css/synchub.css").read_text()
