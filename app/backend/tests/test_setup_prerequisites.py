"""Pinned prerequisite manifest and byte/signature verification.

The properties under test are the ones a release depends on: a manifest that cannot silently
downgrade to http or drop its hash, and a verifier that reports "could not check" as a failure
rather than as a pass.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from creatoros.services.setup import manifest as manifest_module
from creatoros.services.setup.manifest import (
    SCHEMA_ID,
    Component,
    ManifestError,
    load_manifest,
    parse_manifest,
)
from creatoros.services.setup.verify import (
    SignatureInfo,
    find_candidate,
    sha256_file,
    subject_matches,
    verify_file,
)


def _document(**overrides: object) -> dict[str, object]:
    component: dict[str, object] = {
        "id": "sample",
        "kind": "vendor-installer",
        "display_name": "Sample installer",
        "version": "1.0",
        "required": False,
        "acquisition": {
            "mode": "official-download",
            "url": "https://example.invalid/sample.exe",
            "filename": "sample.exe",
            "size_bytes": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
        },
        "signature": {"required": False, "expected_subject": None, "timestamp_required": False},
        "install": {"technology": "none", "arguments": [], "silent": True, "elevates": False},
        "consent": {"required": True, "network_required": True, "summary": "sample"},
        "license": {"name": "MIT"},
        "redistribution": {"approved_for_bundling": False, "detail": "not approved"},
    }
    component.update(overrides)
    return {"schema": SCHEMA_ID, "updated": "2026-11-25", "source": "test", "components": [component]}


def _component(**overrides: object) -> Component:
    return parse_manifest(_document(**overrides)).components[0]  # type: ignore[arg-type]


def _sample(tmp_path: Path, payload: bytes = b"abc") -> Path:
    path = tmp_path / "sample.exe"
    path.write_bytes(payload)
    return path


def test_shipped_manifest_is_valid_and_pins_a_hash_for_every_component():
    manifest = load_manifest()
    assert manifest.schema == SCHEMA_ID
    assert manifest.components, "the shipped manifest must declare at least one component"
    for component in manifest.components:
        assert component.sha256 and len(component.sha256) == 64, component.id
        assert component.size_bytes and component.size_bytes > 0, component.id
        assert component.download_url is None or component.download_url.startswith("https://")
    ollama = manifest.get("ollama")
    assert ollama.version == "0.40.18"
    assert ollama.requires_signature is True
    assert ollama.needs_consent is True
    # The manifest must keep telling the truth about bundling: v1.0.0 has no approval.
    assert ollama.redistribution_approved is False


def test_manifest_rejects_a_plain_http_download():
    document = _document()
    document["components"][0]["acquisition"]["url"] = "http://example.invalid/sample.exe"  # type: ignore[index]
    with pytest.raises(ManifestError, match="https"):
        parse_manifest(document)  # type: ignore[arg-type]


def test_manifest_rejects_a_network_component_without_consent():
    document = _document()
    document["components"][0]["consent"] = {"required": False}  # type: ignore[index]
    with pytest.raises(ManifestError, match="consent"):
        parse_manifest(document)  # type: ignore[arg-type]


def test_manifest_rejects_duplicate_ids_and_missing_hashes():
    document = _document()
    document["components"].append(dict(document["components"][0]))  # type: ignore[union-attr]
    with pytest.raises(ManifestError, match="duplicate"):
        parse_manifest(document)  # type: ignore[arg-type]

    broken = _document()
    broken["components"][0]["acquisition"]["sha256"] = "not-a-hash"  # type: ignore[index]
    with pytest.raises(ManifestError, match="sha256"):
        parse_manifest(broken)  # type: ignore[arg-type]


def test_manifest_rejects_a_signature_requirement_without_an_expected_signer():
    document = _document()
    document["components"][0]["signature"] = {"required": True, "expected_subject": None}  # type: ignore[index]
    with pytest.raises(ManifestError, match="expected_subject"):
        parse_manifest(document)  # type: ignore[arg-type]


def test_verify_accepts_exactly_pinned_bytes(tmp_path: Path):
    component = _component()
    report = verify_file(component, _sample(tmp_path))
    assert report.ok is True
    assert report.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert report.size_bytes == 3
    assert {check.state for check in report.checks} == {"pass", "skipped"}


def test_verify_rejects_a_modified_file_and_names_both_hashes(tmp_path: Path):
    component = _component()
    report = verify_file(component, _sample(tmp_path, b"abd"))
    assert report.ok is False
    hashes = [check for check in report.checks if check.id == "file.sha256"]
    assert hashes and hashes[0].state == "fail"
    assert hashlib.sha256(b"abd").hexdigest() in hashes[0].detail
    assert component.sha256 in hashes[0].detail


def test_verify_reports_a_missing_file_without_raising(tmp_path: Path):
    report = verify_file(_component(), tmp_path / "absent.exe")
    assert report.ok is False
    assert report.checks[0].id == "file.exists"
    assert report.checks[0].state == "fail"


def test_verify_fails_closed_when_the_signature_cannot_be_read(tmp_path: Path):
    component = _component(
        signature={"required": True, "expected_subject": "CN=Sample Inc.", "timestamp_required": True}
    )

    def unreadable(_path: Path) -> SignatureInfo:
        return SignatureInfo(None, None, None, None, None, "no PowerShell host is available")

    report = verify_file(component, _sample(tmp_path), signature_reader=unreadable)
    assert report.ok is False
    unknown = [check for check in report.checks if check.id == "signature.read"]
    assert unknown and unknown[0].state == "unknown"


def test_verify_rejects_the_wrong_signer_even_when_the_signature_is_valid(tmp_path: Path):
    component = _component(
        signature={"required": True, "expected_subject": "CN=Sample Inc., O=Sample", "timestamp_required": True}
    )

    def wrong_signer(_path: Path) -> SignatureInfo:
        return SignatureInfo("Valid", "CN=Other Corp., O=Other", "AB", True, "CA")

    report = verify_file(component, _sample(tmp_path), signature_reader=wrong_signer)
    assert report.ok is False
    signer = [check for check in report.checks if check.id == "signature.signer"]
    assert signer and signer[0].state == "fail"

    def right_signer(_path: Path) -> SignatureInfo:
        return SignatureInfo("Valid", "CN=Sample Inc., O=Sample, C=CA", "AB", True, "CA")

    assert verify_file(component, _sample(tmp_path), signature_reader=right_signer).ok is True


def test_verify_requires_a_timestamp_when_the_manifest_does(tmp_path: Path):
    component = _component(
        signature={"required": True, "expected_subject": "CN=Sample Inc.", "timestamp_required": True}
    )

    def no_timestamp(_path: Path) -> SignatureInfo:
        return SignatureInfo("Valid", "CN=Sample Inc.", "AB", False, None)

    report = verify_file(component, _sample(tmp_path), signature_reader=no_timestamp)
    assert report.ok is False
    stamp = [check for check in report.checks if check.id == "signature.timestamp"]
    assert stamp and stamp[0].state == "fail"


def test_subject_matching_ignores_rdn_order_but_not_a_missing_component():
    expected = "CN=Ollama Inc., O=Ollama Inc., L=Toronto, S=Ontario, C=CA"
    assert subject_matches(expected, "C=CA, S=Ontario, L=Toronto, O=Ollama Inc., CN=Ollama Inc.")
    assert not subject_matches(expected, "CN=Ollama Inc., O=Ollama Inc., C=CA")
    assert not subject_matches(expected, None)


def test_find_candidate_only_returns_a_real_file(tmp_path: Path):
    component = _component()
    assert find_candidate(component, [tmp_path]) is None
    created = tmp_path / "sample.exe"
    created.write_bytes(b"abc")
    assert find_candidate(component, [tmp_path]) == created
    assert sha256_file(created) == hashlib.sha256(b"abc").hexdigest()


def test_manifest_cache_can_be_cleared_between_reads():
    first = load_manifest()
    manifest_module.clear_manifest_cache()
    second = load_manifest()
    assert first.ids() == second.ids()
