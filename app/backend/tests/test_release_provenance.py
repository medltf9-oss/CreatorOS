"""Release packaging has one entry point and one provenance stamp.

The D-5 bypass was a second command that could overwrite release bytes without the backend, SBOM,
inventory or stamp checks. These tests fail if that path returns or if the gate stops requiring the
stamp that build-release.ps1 writes.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_desktop_has_no_packaging_script_that_calls_electron_builder():
    scripts = json.loads((REPO / "app" / "desktop" / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert "package" not in scripts
    assert "package:dir" not in scripts
    assert not any("electron-builder" in str(command) for command in scripts.values())


def test_build_release_writes_the_stamp_and_gate_requires_it():
    build = (REPO / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
    gate = (REPO / "scripts" / "release-gate.ps1").read_text(encoding="utf-8")
    assert "release-provenance.json" in build
    assert "creatoros.release-provenance.v1" in build
    assert "artifact.build-provenance" in gate
    assert "release-provenance.json" in gate
    # The stamp must bind the artifact, commit, payload and runtime manifest, not just exist.
    for binding in ("artifact_sha256", "source_commit", "payload_tree_sha256", "runtime_manifest_sha256"):
        assert binding in build
        assert binding in gate
    # A dirty tracked tree cannot produce a provenance claim for a commit.
    assert "status --porcelain --untracked-files=no" in build


def test_readme_does_not_advertise_a_bypass_path():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "pnpm package" not in readme
    assert "npm run package" not in readme


def test_bundle_verifier_waits_for_the_exit_code():
    build = (REPO / "scripts" / "build-backend.ps1").read_text(encoding="utf-8")
    assert "ProcessStartInfo" in build
    assert "$proc.WaitForExit()" in build
    assert "$proc.ExitCode" in build
