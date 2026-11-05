"""Which code is running: the commit beside the version on SyncHub's footer, the Workers page and the updater's lines."""
import importlib.util
from pathlib import Path

from synchub import buildinfo, workers
from synchub.web import fleet

ROOT = Path(__file__).resolve().parents[2]


def test_short_accepts_only_a_real_commit_id():
    assert buildinfo.short("061CD30aabbccdd") == "061cd30"
    assert buildinfo.short("unknown") == "" and buildinfo.short("") == "" and buildinfo.short(None) == ""


def test_the_image_commit_wins_and_the_label_has_both(monkeypatch):
    monkeypatch.setenv("SYNCHUB_COMMIT", "061cd30e6f1a2b3c4d5e6f708192a3b4c5d6e7f8")
    monkeypatch.setenv("SYNCHUB_BUILT", "2026-11-05T04:00:00Z")
    buildinfo.get.cache_clear()
    try:
        got = buildinfo.get()
        assert got["commit"] == "061cd30" and got["built"] == "2026-11-05T04:00:00Z" and got["label"].endswith("(061cd30)")
    finally:
        buildinfo.get.cache_clear()


def test_the_footer_shows_the_commit(client, monkeypatch):
    monkeypatch.setenv("SYNCHUB_COMMIT", "abcdef1234567")
    buildinfo.get.cache_clear()
    try:
        assert "(abcdef1)" in client.get("/").get_data(as_text=True)
    finally:
        buildinfo.get.cache_clear()


def test_a_worker_commit_is_kept_and_the_update_text_names_both_commits(conn):
    worker_id, _ = workers.create(conn, "wtwo", (), 1)
    workers.register(conn, worker_id, {"protocol": workers.PROTOCOL, "version": "1.4.0", "commit": "061cd30", "built": "2026-11-05T04:00:00Z", "capabilities": {}})
    got = fleet.describe_worker(workers.get(conn, worker_id))
    assert got["commit"] == "061cd30" and got["built"] == "2026-11-05T04:00:00Z"
    assert workers._build("1.4.0", "061cd30") == "1.4.0 (061cd30)" and workers._build("", "061cd30") == "061cd30" and workers._build("", "") == "unknown"
    shown = fleet.describe_update({"status": "ok", "version": "1.4.0", "commit": "061cd30", "at": None})
    assert "1.4.0 (061cd30)" in shown["text"]


def test_the_agent_reads_build_info_beside_itself(tmp_path, monkeypatch):
    for name in ("agent/synchub_agent.py", "player/synchub_player.py"):
        spec = importlib.util.spec_from_file_location("buildinfo_probe", ROOT / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        folder = tmp_path / Path(name).stem
        folder.mkdir()
        (folder / "BUILD_INFO").write_text("commit=061cd30e6f1a2b3c\nbuilt=2026-11-05T04:00:00Z\n")
        monkeypatch.setattr(module, "__file__", str(folder / "x.py"))
        module._BUILD = None
        assert module.build_info() == {"commit": "061cd30", "built": "2026-11-05T04:00:00Z"}
