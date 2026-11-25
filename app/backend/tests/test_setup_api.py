"""First-run setup API behaviour.

The wizard must be useful with a dead network and no model runtime, must never claim Ollama is
present when it is not, and must record a completion decision that survives an upgrade.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa

from creatoros.api.routes import setup as setup_routes
from creatoros.db.session import session_scope
from creatoros.services.setup import detection as detection_module
from creatoros.services.setup import state as state_service
from creatoros.services.setup import verify as verify_service
from creatoros.services.setup.manifest import ManifestError


@pytest.fixture()
def no_runtime(monkeypatch):
    """A machine with no Ollama and no previously downloaded installer."""
    monkeypatch.setattr(
        detection_module,
        "probe_ollama",
        lambda endpoint="http://127.0.0.1:11434": {
            "reachable": False,
            "version": None,
            "models": [],
            "error": "ConnectError: connection refused",
        },
    )
    monkeypatch.setattr(detection_module, "_find_ollama_install", lambda: None)
    monkeypatch.setattr(verify_service, "find_candidate", lambda component, directories: None)


def test_status_describes_an_empty_machine_without_inventing_ai(no_runtime, client):
    response = client.get("/setup/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["setup_complete"] is False
    assert body["first_run"]["completed"] is False
    assert body["ollama"]["installed"] is False
    assert body["ollama"]["models"] == []
    assert body["disk"]["writable"] is True
    ids = {check["id"]: check["state"] for check in body["checks"]}
    assert ids["data.root"] == "ok"
    assert ids["ai.ollama"] == "warn"
    assert ids["prerequisites.manifest"] == "ok"
    # The GPL question must stay visible rather than being quietly dropped.
    assert ids["license.ffmpeg"] == "warn"


def test_status_lists_pinned_prerequisites_without_claiming_they_are_verified(no_runtime, client):
    body = client.get("/setup/status").json()
    by_id = {item["id"]: item for item in body["prerequisites"]}
    assert set(by_id) == {"ollama", "ffmpeg"}
    assert by_id["ollama"]["state"] == "missing"
    assert by_id["ollama"]["verified"] is False
    assert by_id["ollama"]["download_url"].startswith("https://github.com/ollama/ollama/releases/")
    assert by_id["ollama"]["redistribution_approved"] is False
    assert by_id["ffmpeg"]["redistribution_approved"] is False


def test_prerequisite_catalog_reports_the_manifest_it_used(no_runtime, client):
    response = client.get("/setup/prerequisites")
    assert response.status_code == 200
    body = response.json()
    assert body["manifest_schema"] == "creatoros.prerequisites.v1"
    assert body["updated"] == "2026-11-25"
    assert len(body["components"]) == 2


def test_completion_is_recorded_once_and_reported_on_the_next_status(client):
    first = client.post("/setup/complete", json={"skipped_ai": True, "notes": "no model wanted"})
    assert first.status_code == 200, first.text
    assert first.json()["setup_complete"] is True
    assert first.json()["already_completed"] is False

    second = client.post("/setup/complete", json={"skipped_ai": False})
    assert second.status_code == 200
    assert second.json()["already_completed"] is True
    assert second.json()["first_run"]["skipped_ai"] is False

    status_body = client.get("/setup/status").json()
    assert status_body["setup_complete"] is True
    assert status_body["first_run"]["completed_at"]

    with session_scope() as db:
        recorded = state_service.read_state(db)
        assert recorded is not None and recorded["completed"] is True
        actions = {
            row[0]
            for row in db.execute(
                sa.text("select action from audit_event where resource_type = 'setup'")
            )
        }
    assert "setup.first_run.complete" in actions


def test_verifying_an_unknown_component_is_a_404_with_a_stable_code(client):
    response = client.post("/setup/prerequisites/not-a-component/verify", json={})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "UNKNOWN_COMPONENT"
    assert "not-a-component" in body["message"]


def test_verifying_without_a_local_file_is_a_409(no_runtime, client):
    response = client.post("/setup/prerequisites/ollama/verify", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "NO_LOCAL_FILE"


def test_a_relative_verify_path_is_rejected(no_runtime, client):
    response = client.post("/setup/prerequisites/ffmpeg/verify", json={"path": "somewhere/ffmpeg.exe"})
    assert response.status_code == 422
    assert response.json()["code"] == "PATH_NOT_ABSOLUTE"


def test_verifying_a_file_that_is_not_the_pinned_build_reports_the_mismatch(no_runtime, client, tmp_path: Path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"not really ffmpeg")
    response = client.post("/setup/prerequisites/ffmpeg/verify", json={"path": str(fake)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["sha256"] == hashlib.sha256(b"not really ffmpeg").hexdigest()
    states = {check["id"]: check["state"] for check in body["checks"]}
    assert states["file.exists"] == "pass"
    assert states["file.sha256"] == "fail"


def test_verifying_the_real_ffmpeg_build_passes_byte_and_size_checks(no_runtime, client):
    from creatoros.core.settings import get_settings
    from creatoros.services.setup.manifest import load_manifest

    binary = get_settings().ffmpeg_bin / "ffmpeg.exe"
    if not binary.is_file():
        pytest.skip("this environment has no FFmpeg build on the expected path")
    pinned = load_manifest().get("ffmpeg")
    if pinned.size_bytes is not None and binary.stat().st_size != pinned.size_bytes:
        # CI installs whatever gyan.dev currently publishes; only the pinned build can be
        # asserted byte-for-byte, and asserting on a different build would be a false failure.
        pytest.skip("the FFmpeg on the expected path is not the pinned build")
    response = client.post("/setup/prerequisites/ffmpeg/verify", json={"path": str(binary)})
    assert response.status_code == 200, response.text
    body = response.json()
    states = {check["id"]: check["state"] for check in body["checks"]}
    assert states["file.sha256"] == "pass"
    assert states["file.size"] == "pass"
    assert body["ok"] is True


def test_a_broken_manifest_degrades_the_status_instead_of_breaking_the_wizard(client, monkeypatch):
    def explode(path=None):
        raise ManifestError("components[0] is missing 'sha256'")

    monkeypatch.setattr(setup_routes, "load_manifest", explode)
    status_response = client.get("/setup/status")
    assert status_response.status_code == 200
    body = status_response.json()
    assert "sha256" in body["manifest_error"]
    assert body["prerequisites"] == []
    states = {check["id"]: check["state"] for check in body["checks"]}
    assert states["prerequisites.manifest"] == "fail"

    catalog = client.get("/setup/prerequisites")
    assert catalog.status_code == 503
    catalog_body = catalog.json()
    assert catalog_body["code"] == "MANIFEST_INVALID"
    assert "sha256" in catalog_body["message"]
