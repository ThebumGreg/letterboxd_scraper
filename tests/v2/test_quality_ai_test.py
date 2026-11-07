"""The AI quality test card: Standard, AI or Both on one file, side by side. The AI half only goes to a worker that reports the quality_ai feature."""
import json

import pytest

from synchub import ai_test, jobs, quality, workers

from .test_quality import clean_metrics, finish_job, fixture_file


def add_ai_worker(conn, features=("quality", "quality_ai")):
    worker_id = conn.execute("INSERT INTO worker (name, token_hash, capabilities, last_seen) VALUES ('WTwo', 'x', ?, ?)",
                             (json.dumps({"ops": ["scan", "quality", "quality_ai"], "features": list(features), "ai_quality": {"device": "cpu", "models": ["fast_vqa"]}}),
                              workers.db.now())).lastrowid
    conn.commit()
    return worker_id


def test_standard_runs_without_an_ai_worker_but_ai_and_both_do_not(conn):
    file_id = fixture_file(conn)
    assert ai_test.ai_workers(conn) == []
    with pytest.raises(ai_test.AiTestError):
        ai_test.run(conn, file_id, "ai")
    with pytest.raises(ai_test.AiTestError):
        ai_test.run(conn, file_id, "both")
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0           # nothing half-started
    test_id = ai_test.run(conn, file_id, "standard", by="Greg")
    row = ai_test.recent(conn)[0]
    assert row["id"] == test_id and row["standard"]["status"] == "queued" and row["ai"] is None and row["status"] == "queued"


def test_ai_and_both_make_a_quality_ai_job_that_shows_on_the_jobs_page(conn):
    add_ai_worker(conn)
    file_id = fixture_file(conn)
    assert [w["name"] for w in ai_test.ai_workers(conn)] == ["WTwo"]
    test_id = ai_test.run(conn, file_id, "both", by="Greg")
    row = conn.execute("SELECT * FROM quality_ai_test WHERE id = ?", (test_id,)).fetchone()
    job = jobs.get(conn, row["ai_job_id"])
    assert job["kind"] == "scan" and job["title"].startswith("AI quality test:") and job["plan"]["op"] == "quality_ai" and job["plan"]["needs"] == ["quality_ai"]
    assert row["standard_check_id"] and quality.get(conn, row["standard_check_id"])["status"] == "queued"
    # a worker without the feature never gets it
    assert not jobs.can_run({"capabilities": {"ops": ["scan", "quality"], "features": ["quality"]}}, job)
    assert jobs.can_run({"capabilities": {"ops": ["scan", "quality_ai"], "features": ["quality_ai"]}}, job)


def test_the_results_of_both_halves_are_read_side_by_side(conn):
    add_ai_worker(conn)
    file_id = fixture_file(conn)
    test_id = ai_test.run(conn, file_id, "both")
    row = conn.execute("SELECT * FROM quality_ai_test WHERE id = ?", (test_id,)).fetchone()
    finish_job(conn, row["standard_check_id"], clean_metrics())
    result = {"op": "quality_ai", "device": "cpu", "elapsed": 120.0, "models": {"fast_vqa": {"mean": 71.24, "raw_mean": 0.7124}}, "errors": {"dover": "weights missing"}}
    conn.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?", (json.dumps(result), row["ai_job_id"]))
    conn.commit()
    got = ai_test.recent(conn)[0]
    assert got["status"] == "done" and got["standard"]["score"] == 100 and got["ai"]["device"] == "cpu"
    assert got["ai"]["models"][0] == {"key": "fast_vqa", "label": "FAST-VQA", "score": 71.2, "version": None}
    assert got["ai"]["models"][1]["error"] == "weights missing"


def test_the_card_and_its_endpoints(client, conn):
    file_id = fixture_file(conn)
    page = client.get("/library/options").get_data(as_text=True)
    assert 'id="aiTest"' in page and "No online worker has the AI test yet" in page and 'value="ai" disabled' in page
    found = client.get("/api/library/quality/ai/files?q=heat").get_json()["files"]
    assert [f["id"] for f in found] == [file_id]
    assert not client.post("/api/library/quality/ai/run", json={"file_id": file_id, "mode": "ai"}).get_json()["ok"]
    assert not client.post("/api/library/quality/ai/run", json={"mode": "standard"}).get_json()["ok"]
    assert client.post("/api/library/quality/ai/run", json={"file_id": file_id, "mode": "standard"}).get_json()["ok"]
    assert "Heat (1995)" in client.get("/library/quality/ai").get_data(as_text=True)
    add_ai_worker(conn)
    assert 'value="ai" disabled' not in client.get("/library/options").get_data(as_text=True)
    assert client.post("/api/library/quality/ai/run", json={"file_id": file_id, "mode": "both"}).get_json()["ok"]


def test_the_resume_menu_opens_above_the_search_bar():
    import re
    css = open("static/css/synchub.css", encoding="utf-8").read()
    z = lambda selector: int(re.search(re.escape(selector) + r"[^{]*\{[^}]*z-index:\s*(\d+)", css).group(1))
    assert z("#continueWatching") > z("#controls.panel") and z("#continueWatching") < z(".navbar")
