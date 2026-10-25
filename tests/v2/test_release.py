"""Things the V1.1 release depends on: one version number, an image that carries adb, workflows and an Unraid template that point at the same image."""
import re
import subprocess
from pathlib import Path

import pytest

import synchub
from synchub.tv.devices.androidtv import AndroidTVDriver

ROOT = Path(__file__).resolve().parents[2]


def test_the_version_is_shown_on_every_page(client):
    assert f"SyncHub v{synchub.__version__}" in client.get("/").get_data(as_text=True)
    assert re.fullmatch(r"\d+\.\d+\.\d+", synchub.__version__)


def test_adb_keeps_its_key_in_the_tv_folder_so_a_replaced_container_is_still_trusted(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, b"ok", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    tv = AndroidTVDriver({"id": "t", "host": "10.0.0.5"}, logbook=type("L", (), {"add": lambda *a, **k: None})(), options={"adb": "adb", "adb_home": str(tmp_path / "adb")})
    tv._adb("devices", scoped=False, quiet=True)
    assert seen["env"]["HOME"] == str(tmp_path / "adb") and (tmp_path / "adb").is_dir()


def test_the_image_installs_adb_and_labels_itself():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "adb" in dockerfile and "org.opencontainers.image.version" in dockerfile and "HEALTHCHECK" in dockerfile


def test_the_workflows_and_the_unraid_template_name_the_same_image():
    publish = (ROOT / ".github/workflows/docker-publish.yml").read_text()
    template = (ROOT / "unraid/synchub.xml").read_text()
    assert "ghcr.io/${{ github.repository_owner }}/synchub" in publish and "type=raw,value=latest" in publish
    assert "<Repository>ghcr.io/thebumgreg/synchub:latest</Repository>" in template
    assert 'Target="/data"' in template and 'Target="PUID"' in template and "<ExtraParams>--init</ExtraParams>" in template


def test_the_unraid_template_is_valid_xml():
    import xml.dom.minidom
    xml.dom.minidom.parse(str(ROOT / "unraid/synchub.xml"))
