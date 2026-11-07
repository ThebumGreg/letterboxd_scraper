"""The AI quality test card: Standard, AI or Both on one file, side by side. The AI half only goes to a worker that reports the quality_ai feature."""
import json

import pytest

from synchub import ai_test, jobs, quality, workers

from .test_quality import clean_metrics, finish_job, fixture_file


def add_ai_worker(conn, features=("quality", "quality_ai")):
    worker_id = conn.execute("INSERT INTO worker (name, token_hash, capabilities, last_seen) VALUES ('WTwo', 'x', ?, ?)",
                             (json.dumps({"ops": ["scan", "quality", "quality_ai"], "features": list(features), "ai_quality": {"device": "cpu", "models": ["musiq"]}}),
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
    result = {"op": "quality_ai", "device": "cpu", "elapsed": 120.0, "models": {"musiq": {"mean": 71.24, "raw_mean": 71.24}}, "errors": {"topiq_nr": "weights missing"}}
    conn.execute("UPDATE job SET status = 'done', result = ? WHERE id = ?", (json.dumps(result), row["ai_job_id"]))
    conn.commit()
    got = ai_test.recent(conn)[0]
    assert got["status"] == "done" and got["standard"]["score"] == 100 and got["ai"]["device"] == "cpu"
    assert got["ai"]["models"][0] == {"key": "musiq", "label": "MUSIQ", "score": 71.2, "version": None}
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


# ---------------------------------------------------------------- the worker's side (agent/synchub_agent.py), with stand-ins for torch and pyiqa
import contextlib
import shutil
import subprocess
import sys
import types

from .agent_module import agent


def install_fake_ai(monkeypatch, fail=()):
    """torch and pyiqa stand-ins: each metric 'scores' the picture file it is given, so a missing or empty frame fails the test."""
    torch = types.ModuleType("torch")
    torch.__version__ = "0-test"
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.device = lambda name: name
    torch.no_grad = contextlib.nullcontext
    pyiqa = types.ModuleType("pyiqa")
    pyiqa.__version__ = "0-test"
    values = {"musiq": 60.0, "topiq_nr": 0.5, "clipiqa": 0.25}

    def create_metric(name, device=None):
        if name in fail:
            raise RuntimeError("weights missing")
        import os
        return lambda path: values[name] if os.path.getsize(path) > 100 else (_ for _ in ()).throw(ValueError("empty picture"))

    pyiqa.create_metric = create_metric
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "pyiqa", pyiqa)
    monkeypatch.setattr(agent, "_AI", {})


def test_a_worker_without_torch_does_not_offer_the_ai_test(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)             # an import of it fails, as on a worker built without the AI layer
    monkeypatch.setattr(agent, "_AI", {})
    ok, info = agent.ai_probe()
    assert not ok and "reason" in info


def test_a_worker_with_the_models_reports_the_feature_and_device(monkeypatch):
    install_fake_ai(monkeypatch)
    ok, info = agent.ai_probe()
    assert ok and info["device"] == "cpu" and info["models"] == ["musiq", "topiq_nr", "clipiqa"]
    monkeypatch.setenv("SYNCHUB_AI_DEVICE", "xpu")
    monkeypatch.setattr(agent, "_AI", {})
    assert agent.ai_probe()[1]["device"] == "xpu"


def test_frames_are_spread_inside_each_part_and_scores_land_on_0_to_100():
    assert agent.ai_frame_times([(100.0, 30.0), (400.0, 30.0)], 3) == [105.0, 115.0, 125.0, 405.0, 415.0, 425.0]
    assert agent.ai_scale("musiq", 61.234) == 61.23 and agent.ai_scale("topiq_nr", 0.5) == 50.0 and agent.ai_scale("clipiqa", 1.7) == 100.0 and agent.ai_scale("musiq", -3) == 0.0
    out = agent.ai_summarise({"musiq": [60, 70], "topiq_nr": [0.5, None], "clipiqa": [], "other": [1]}, version="v")
    assert out == {"musiq": {"mean": 65.0, "raw_mean": 65.0, "scores": [60.0, 70.0], "version": "v"},
                   "topiq_nr": {"mean": 50.0, "raw_mean": 0.5, "scores": [50.0], "version": "v"}}


def test_the_frame_command_scales_down_and_tonemaps_only_hdr():
    sdr = agent.ai_frame_command("ffmpeg", "/m/f.mkv", 12.5, "/w/frame.png")
    hdr = agent.ai_frame_command("ffmpeg", "/m/f.mkv", 12.5, "/w/frame.png", tonemap=True)
    assert "tonemap" not in " ".join(sdr) and "min(1920,iw)" in sdr[sdr.index("-vf") + 1]
    assert hdr[hdr.index("-vf") + 1].startswith("zscale=t=linear") and "min(1920,iw)" in hdr[hdr.index("-vf") + 1]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_ai_job_scores_real_frames_and_keeps_going_when_one_model_fails(tmp_path, monkeypatch):
    install_fake_ai(monkeypatch, fail=("clipiqa",))
    film = tmp_path / "share" / "Heat (1995).mkv"
    film.parent.mkdir()
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=12", "-c:v", "libx264", str(film)], check=True)
    monkeypatch.setattr(agent.media_analysis, "run_ffprobe", lambda settings, src: {})
    monkeypatch.setattr(agent, "media_duration", lambda probe: 12.0)
    work = tmp_path / "work"
    work.mkdir()
    progress = []
    runner = types.SimpleNamespace(
        job={"plan": {"op": "quality_ai", "samples": 2, "seconds": 10, "hdr": "sdr", "seed": 1, "models": ["musiq", "topiq_nr", "clipiqa"], "source_size": film.stat().st_size},
             "source": {"root": "media", "rel": "Heat (1995).mkv"}},
        cfg=types.SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe", work_dir=str(work)), folder_in=lambda root, rel="": str(film),
        media_settings=lambda plan: None, guard=lambda: None, report_progress=lambda *a, **k: progress.append(a),
        log=types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
    result = agent.JobRunner.do_quality_ai(runner)
    assert result["op"] == "quality_ai" and result["device"] == "cpu" and result["frames"] >= 3 and result["tonemapped"] is False
    assert result["models"]["musiq"]["mean"] == 60.0 and result["models"]["topiq_nr"]["mean"] == 50.0 and "clipiqa" not in result["models"]
    assert "weights missing" in result["errors"]["clipiqa"] and progress[-1][1] == 1.0
    assert list(work.iterdir()) == []                            # the frames were cleaned up


def test_an_ai_job_on_a_worker_without_the_models_fails_without_retrying(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(agent, "_AI", {})
    with pytest.raises(agent.JobFailure) as err:
        agent.JobRunner.do_quality_ai(types.SimpleNamespace(job={"plan": {}, "source": None}))
    assert err.value.retryable is False
