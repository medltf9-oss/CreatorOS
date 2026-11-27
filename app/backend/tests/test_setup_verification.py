"""Verification records, safety gates and completion integrity.

These tests hold the setup API to the claims it makes: a component is "verified" only after a
recorded hash run whose fingerprint still matches, a failed run is recorded but never promoted, a
stale record says which fingerprint changed, and completion keeps the decision an upgrade must not
discard.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import types
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from creatoros.api.routes import setup as setup_routes
from creatoros.core.settings import get_settings
from creatoros.db.session import session_scope
from creatoros.services.setup import detection as detection_module
from creatoros.services.setup import state as state_service
from creatoros.services.setup import verify as verify_service
from creatoros.services.setup.manifest import Manifest, parse_manifest

SAMPLE_PAYLOAD = b"pinned-sample-bytes"


def _document(component: dict[str, object], updated: str = "2026-11-25") -> dict[str, object]:
    return {"schema": "creatoros.prerequisites.v1", "updated": updated, "source": "test", "components": [component]}


def _local_component(*, component_id: str = "sample", filename: str = "sample.bin", payload: bytes = SAMPLE_PAYLOAD, version: str = "1.0") -> dict[str, object]:
    return {
        "id": component_id,
        "kind": "local-tool",
        "display_name": f"{component_id} fixture",
        "version": version,
        "required": False,
        "acquisition": {
            "mode": "local",
            "filename": filename,
            "relative_to": "temp",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "signature": {"required": False, "expected_subject": None, "timestamp_required": False},
        "consent": {"required": False, "network_required": False, "summary": "fixture"},
        "license": {"name": "MIT"},
        "redistribution": {"approved_for_bundling": False, "detail": "fixture is not approved"},
    }


def _manifest(component: dict[str, object]) -> Manifest:
    return parse_manifest(_document(component))


def _sample_file(tmp_path: Path, payload: bytes = SAMPLE_PAYLOAD) -> Path:
    path = tmp_path / "sample.bin"
    path.write_bytes(payload)
    return path


def _install_manifest(monkeypatch, manifest: Manifest) -> None:
    monkeypatch.setattr(setup_routes, "load_manifest", lambda path=None: manifest)


def _fake_stat(*, mode: int, size: int = 0, mtime_ns: int = 0, reparse: int = 0) -> object:
    return types.SimpleNamespace(st_mode=mode, st_size=size, st_mtime_ns=mtime_ns, st_reparse_tag=reparse)


def _verifications() -> dict:
    with session_scope() as db:
        return state_service.read_verifications(db)


@pytest.fixture()
def quiet_machine(monkeypatch):
    """No live Ollama probe and a fake-but-consistent FFmpeg presence for completion/status calls."""
    monkeypatch.setattr(
        detection_module,
        "detect_ollama",
        lambda endpoint=detection_module.DEFAULT_ENDPOINT: detection_module.OllamaFacts(
            installed=False,
            install_path=None,
            version=None,
            endpoint=endpoint,
            reachable=False,
            models=(),
            error="ConnectError: connection refused",
        ),
    )
    monkeypatch.setattr(
        detection_module,
        "detect_ffmpeg",
        lambda settings: detection_module.FfmpegFacts(True, True, "C:/tools/ffmpeg.exe", "ffmpeg version test", None),
    )
    monkeypatch.setattr(verify_service, "find_candidate", lambda component, directories: None)


def _test_settings(monkeypatch, version: str) -> None:
    base = get_settings()
    monkeypatch.setattr(setup_routes, "get_settings", lambda: base.model_copy(update={"version": version}))


# --- verification reachable, then stale -------------------------------------------------------


def test_verify_records_a_verification_and_status_reports_it_verified(tmp_path: Path, monkeypatch, client):
    manifest = _manifest(_local_component())
    _install_manifest(monkeypatch, manifest)
    target = _sample_file(tmp_path)

    response = client.post("/setup/prerequisites/sample/verify", json={"path": str(target)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["verification"]["ok"] is True
    assert body["verification"]["current"] is True
    assert body["verification"]["sha256"] == hashlib.sha256(SAMPLE_PAYLOAD).hexdigest()
    assert body["verification"]["size_bytes"] == len(SAMPLE_PAYLOAD)
    datetime.fromisoformat(body["verification"]["verified_at"])

    status_body = client.get("/setup/status").json()
    component = {item["id"]: item for item in status_body["prerequisites"]}["sample"]
    assert component["state"] == "verified"
    assert component["verified"] is True
    assert component["verification"]["current"] is True
    assert "recorded verification" in component["detail"]
    assert "not a live re-hash" in component["detail"]

    catalog = client.get("/setup/prerequisites").json()
    assert {item["id"]: item for item in catalog["components"]}["sample"]["state"] == "verified"


def test_verification_goes_stale_when_the_file_changes(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    target = _sample_file(tmp_path)
    assert client.post("/setup/prerequisites/sample/verify", json={"path": str(target)}).json()["ok"] is True

    target.write_bytes(SAMPLE_PAYLOAD + b"tampered")

    component = {item["id"]: item for item in client.get("/setup/status").json()["prerequisites"]}["sample"]
    assert component["state"] == "unverified"
    assert component["verified"] is False
    assert component["verification"]["current"] is False
    assert "size changed" in component["detail"]
    # The record is kept and named, not silently dropped.
    assert component["verification"]["sha256"] == hashlib.sha256(SAMPLE_PAYLOAD).hexdigest()


def test_a_changed_modified_time_alone_makes_the_record_stale(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    target = _sample_file(tmp_path)
    assert client.post("/setup/prerequisites/sample/verify", json={"path": str(target)}).json()["ok"] is True

    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))

    component = {item["id"]: item for item in client.get("/setup/status").json()["prerequisites"]}["sample"]
    assert component["verification"]["current"] is False
    assert component["state"] == "unverified"
    assert "modified time changed" in component["detail"]


def test_a_missing_file_is_stale_not_verified(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    target = _sample_file(tmp_path)
    assert client.post("/setup/prerequisites/sample/verify", json={"path": str(target)}).json()["ok"] is True

    target.unlink()

    component = {item["id"]: item for item in client.get("/setup/status").json()["prerequisites"]}["sample"]
    assert component["state"] == "unverified"
    assert component["verified"] is False
    assert "missing" in component["detail"]


def test_a_failed_verify_is_recorded_but_never_verified(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    target = _sample_file(tmp_path, b"not-the-pinned-bytes")

    response = client.post("/setup/prerequisites/sample/verify", json={"path": str(target)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["verification"]["ok"] is False
    assert body["verification"]["current"] is True
    assert body["verification"]["sha256"] == hashlib.sha256(b"not-the-pinned-bytes").hexdigest()

    component = {item["id"]: item for item in client.get("/setup/status").json()["prerequisites"]}["sample"]
    assert component["state"] == "unverified"
    assert component["verified"] is False
    assert "failed" in component["detail"]
    assert component["verification"]["ok"] is False


# --- safety gates ------------------------------------------------------------------------------


def test_symlink_targets_are_rejected_before_any_bytes_are_read(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    target = _sample_file(tmp_path)
    monkeypatch.setattr(
        verify_service,
        "_lstat",
        lambda path: _fake_stat(mode=stat.S_IFLNK | 0o777, size=len(SAMPLE_PAYLOAD)),
    )

    response = client.post("/setup/prerequisites/sample/verify", json={"path": str(target)})
    assert response.status_code == 422
    assert response.json()["code"] == "VERIFY_SYMLINK"
    # A refused target never became a verification, so no record exists.
    assert _verifications() == {}


def test_reparse_points_are_rejected_even_when_the_mode_looks_regular(tmp_path: Path, monkeypatch):
    component = _manifest(_local_component()).get("sample")
    target = _sample_file(tmp_path)
    monkeypatch.setattr(
        verify_service,
        "_lstat",
        lambda path: _fake_stat(mode=stat.S_IFREG | 0o644, size=len(SAMPLE_PAYLOAD), reparse=0xA0000003),
    )
    with pytest.raises(verify_service.VerifyRejected) as caught:
        verify_service.verify_target(component, target)
    assert caught.value.code == "VERIFY_SYMLINK"


def test_a_directory_target_is_not_a_regular_file(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    response = client.post("/setup/prerequisites/sample/verify", json={"path": str(tmp_path)})
    assert response.status_code == 422
    assert response.json()["code"] == "VERIFY_NOT_REGULAR_FILE"


def test_targets_over_the_cap_are_rejected(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    target = _sample_file(tmp_path)
    monkeypatch.setattr(verify_service, "MAX_VERIFY_BYTES", 2)

    response = client.post("/setup/prerequisites/sample/verify", json={"path": str(target)})
    assert response.status_code == 422
    assert response.json()["code"] == "VERIFY_TOO_LARGE"


def test_verification_waits_for_the_module_lock_then_times_out(tmp_path: Path, monkeypatch):
    component = _manifest(_local_component()).get("sample")
    target = _sample_file(tmp_path)
    started = time.monotonic()
    with verify_service._VERIFY_LOCK, pytest.raises(verify_service.VerifyTimeout):
        verify_service.verify_target(component, target, deadline_s=0.05)
    assert time.monotonic() - started < 5


def test_the_deadline_stops_a_slow_hash(tmp_path: Path, monkeypatch):
    component = _manifest(_local_component()).get("sample")
    target = _sample_file(tmp_path)

    def slow_hash(path: Path) -> str:
        time.sleep(0.2)
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    with pytest.raises(verify_service.VerifyTimeout):
        verify_service.verify_target(component, target, deadline_s=0.05, hasher=slow_hash)


def test_a_timeout_is_a_504_and_leaves_no_half_written_record(tmp_path: Path, monkeypatch, client):
    _install_manifest(monkeypatch, _manifest(_local_component()))
    target = _sample_file(tmp_path)

    def wedge(*args, **kwargs):
        raise verify_service.VerifyTimeout("deadline exceeded by the test double")

    monkeypatch.setattr(verify_service, "verify_target", wedge)
    response = client.post("/setup/prerequisites/sample/verify", json={"path": str(target)})
    assert response.status_code == 504
    assert response.json()["code"] == "VERIFY_TIMEOUT"
    assert _verifications() == {}


# --- component honesty -------------------------------------------------------------------------


def test_ffmpeg_size_match_is_present_but_not_verified_until_verify_runs(tmp_path: Path, monkeypatch, client):
    payload = b"fake-ffmpeg-binary"
    target = tmp_path / "ffmpeg.exe"
    target.write_bytes(payload)
    _install_manifest(monkeypatch, _manifest(_local_component(component_id="ffmpeg", filename="ffmpeg.exe", payload=payload)))
    monkeypatch.setattr(
        detection_module,
        "detect_ffmpeg",
        lambda settings: detection_module.FfmpegFacts(True, True, str(target), "ffmpeg version fake", None),
    )

    before = {item["id"]: item for item in client.get("/setup/status").json()["prerequisites"]}["ffmpeg"]
    assert before["state"] == "ok"
    assert before["verified"] is False
    assert before["verification"] is None
    assert "integrity is unverified" in before["detail"]
    assert "not run until /verify succeeds" in before["detail"]

    assert client.post("/setup/prerequisites/ffmpeg/verify", json={"path": str(target)}).json()["ok"] is True

    after = {item["id"]: item for item in client.get("/setup/status").json()["prerequisites"]}["ffmpeg"]
    assert after["state"] == "verified"
    assert after["verified"] is True


def test_ollama_reports_installed_version_without_claiming_the_pinned_version(tmp_path: Path, monkeypatch, client):
    ollama_component = {
        "id": "ollama",
        "kind": "vendor-installer",
        "display_name": "Ollama fixture",
        "version": "0.40.18",
        "required": False,
        "acquisition": {
            "mode": "official-download",
            "url": "https://example.invalid/OllamaSetup.exe",
            "filename": "OllamaSetup.exe",
            "size_bytes": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
        },
        "signature": {"required": False, "expected_subject": None, "timestamp_required": False},
        "consent": {"required": True, "network_required": True, "summary": "fixture"},
        "license": {"name": "MIT"},
        "redistribution": {"approved_for_bundling": False, "detail": "not approved"},
    }
    _install_manifest(monkeypatch, _manifest(ollama_component))

    def facts(version: str):
        return detection_module.OllamaFacts(
            installed=True,
            install_path="C:/Ollama/ollama.exe",
            version=version,
            endpoint=detection_module.DEFAULT_ENDPOINT,
            reachable=True,
            models=(),
            error=None,
        )

    monkeypatch.setattr(detection_module, "detect_ollama", lambda endpoint=detection_module.DEFAULT_ENDPOINT: facts("0.1.0"))
    body = client.get("/setup/status").json()
    component = {item["id"]: item for item in body["prerequisites"]}["ollama"]
    checks = {check["id"]: check["state"] for check in body["checks"]}
    assert component["state"] == "ok"
    assert component["verified"] is False
    assert "version 0.1.0 does not match the pinned 0.40.18" in component["detail"]
    assert checks["ai.ollama.version"] == "warn"

    monkeypatch.setattr(detection_module, "detect_ollama", lambda endpoint=detection_module.DEFAULT_ENDPOINT: facts("0.40.18"))
    body = client.get("/setup/status").json()
    component = {item["id"]: item for item in body["prerequisites"]}["ollama"]
    checks = {check["id"]: check["state"] for check in body["checks"]}
    assert "version 0.40.18 matches the pinned 0.40.18" in component["detail"]
    assert checks["ai.ollama.version"] == "ok"


# --- completion integrity ----------------------------------------------------------------------


def test_missing_local_binary_names_the_expected_path_and_official_source(quiet_machine, monkeypatch, tmp_path, client):
    missing = tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    monkeypatch.setattr(setup_routes, "_local_path", lambda component, settings: missing)
    response = client.post("/setup/prerequisites/ffmpeg/verify", json={})
    assert response.status_code == 409, (response.status_code, response.text)
    message = response.json()["message"]
    assert response.json()["code"] == "NO_LOCAL_FILE"
    assert message.startswith("expected")
    assert "ffmpeg.exe" in message
    assert "tools" in message and "ffmpeg" in message
    assert "https://www.gyan.dev/ffmpeg/builds/" in message


def test_required_check_failure_blocks_completion_until_overridden(quiet_machine, monkeypatch, client):
    monkeypatch.setattr(
        detection_module,
        "detect_ffmpeg",
        lambda settings: detection_module.FfmpegFacts(False, False, None, None, "no ffmpeg.exe in the fixture"),
    )

    blocked = client.post("/setup/complete", json={})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "REQUIRED_CHECKS_FAILING"
    assert blocked.json()["message"] == "media.ffmpeg"
    guidance = blocked.json()["details"]["guidance"]
    assert "tools" in guidance and "ffmpeg" in guidance
    assert "gyan.dev" in guidance
    with session_scope() as db:
        assert state_service.read_state(db) is None

    consent = client.post("/setup/prerequisites/ollama/consent", json={"accepted": True})
    assert consent.status_code == 200, consent.text

    accepted = client.post("/setup/complete", json={"override_required_checks": True, "notes": "no media yet"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["setup_complete"] is True
    assert accepted.json()["first_run"]["version_changed"] is False

    with session_scope() as db:
        recorded = state_service.read_state(db)
        assert recorded is not None
        assert recorded["override_required_checks"] is True
        row = db.execute(
            sa.text(
                "select metadata_redacted_json from audit_event "
                "where action = 'setup.first_run.complete' order by created_at desc limit 1"
            )
        ).first()
    assert row is not None
    metadata = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    assert metadata["override_required_checks"] is True
    assert "media.ffmpeg" in metadata["failed_checks"]
    assert metadata["consents"]["ollama"]["accepted"] is True


def test_detection_does_not_execute_an_unverified_ffmpeg(tmp_path: Path):
    """Dropping a file at the pinned path must not run it before /verify hashes it."""
    from creatoros.core.settings import Settings

    bin_dir = tmp_path / "tools" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "ffmpeg.exe").write_bytes(b"MZ not a real executable")
    (bin_dir / "ffprobe.exe").write_bytes(b"MZ not a real executable")

    facts = detection_module.detect_ffmpeg(Settings(ffmpeg_dir=str(bin_dir)))

    assert facts.present is True
    assert facts.ffprobe_present is True
    assert facts.version is None
    assert facts.error is None


def test_completion_is_idempotent_and_keeps_the_first_decision(quiet_machine, monkeypatch, client):
    _test_settings(monkeypatch, "1.0.0")
    first = client.post("/setup/complete", json={"skipped_ai": True, "notes": "first decision"})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["already_completed"] is False
    assert first_body["first_run"]["version"] == "1.0.0"
    assert first_body["first_run"]["version_changed"] is False
    first_completed_at = first_body["first_run"]["completed_at"]

    _test_settings(monkeypatch, "2.0.0")
    second = client.post("/setup/complete", json={})
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["already_completed"] is True
    assert second_body["first_run"]["completed_at"] == first_completed_at
    assert second_body["first_run"]["version"] == "1.0.0"
    assert second_body["first_run"]["version_changed"] is True

    with session_scope() as db:
        recorded = state_service.read_state(db)
    assert recorded is not None
    assert recorded["completed_at"] == first_completed_at
    assert recorded["completed_version"] == "1.0.0"
    assert recorded["version"] == "1.0.0"
    assert recorded["notes"] == "first decision"

    third = client.post("/setup/complete", json={"notes": "replaced decision"})
    assert third.status_code == 200, third.text
    assert third.json()["first_run"]["completed_at"] == first_completed_at
    with session_scope() as db:
        recorded = state_service.read_state(db)
    assert recorded is not None
    assert recorded["notes"] == "replaced decision"
    assert recorded["completed_version"] == "1.0.0"


def test_version_changed_warns_without_reopening_the_wizard(quiet_machine, monkeypatch, client):
    _test_settings(monkeypatch, "1.0.0")
    assert client.post("/setup/complete", json={}).status_code == 200

    _test_settings(monkeypatch, "2.0.0")
    body = client.get("/setup/status").json()
    assert body["setup_complete"] is True
    assert body["first_run"]["completed"] is True
    assert body["first_run"]["version"] == "1.0.0"
    assert body["first_run"]["version_changed"] is True
    checks = {check["id"]: check for check in body["checks"]}
    assert checks["setup.version_changed"]["state"] == "warn"
    assert "does not reopen" in checks["setup.version_changed"]["detail"]
