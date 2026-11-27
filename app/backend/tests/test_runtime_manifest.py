"""The embedded interpreter is pinned by its own committed hash manifest.

A Python distribution RECORD cannot cover python.exe, python312.dll or the DLLs directory, so a
replaced interpreter would otherwise pass every package check. These tests exercise the manifest
that closes that gap and prove that a size match is not accepted as verification.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))

from runtime_manifest import (
    RuntimeManifestError,
    build_manifest,
    load_manifest,
    verify_tree,
)


def _runtime(root: Path, *, marker: bytes = b"python") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "python.exe").write_bytes(marker)
    (root / "python3.dll").write_bytes(b"python3")
    (root / "python312.dll").write_bytes(b"python312")
    (root / "vcruntime140.dll").write_bytes(b"vcruntime")
    (root / "vcruntime140_1.dll").write_bytes(b"vcruntime1")
    dlls = root / "DLLs"
    dlls.mkdir()
    (dlls / "_sqlite3.pyd").write_bytes(b"pyd")
    (dlls / "sqlite3.dll").write_bytes(b"dll")
    return root


def _write(root: Path, manifest: Path, **kwargs: object) -> dict:
    document = build_manifest(root, python_version="3.12.10")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return document


def test_a_clean_runtime_matches_its_generated_manifest(tmp_path: Path):
    root = _runtime(tmp_path / "runtime")
    manifest = tmp_path / "runtime-manifest.json"
    _write(root, manifest)
    findings = verify_tree(root, load_manifest(manifest), strict_extras=True)
    assert findings == []


def test_a_replaced_interpreter_is_detected_even_when_the_size_is_unchanged(tmp_path: Path):
    root = _runtime(tmp_path / "runtime")
    manifest = tmp_path / "runtime-manifest.json"
    _write(root, manifest)
    target = root / "python.exe"
    original_size = target.stat().st_size
    target.write_bytes(b"EVIL!!"[:original_size].ljust(original_size, b"!"))
    assert target.stat().st_size == original_size
    findings = verify_tree(root, load_manifest(manifest), strict_extras=True)
    assert any("SHA-256 mismatch for python.exe" in item for item in findings)


def test_an_extra_executable_or_dll_is_rejected(tmp_path: Path):
    root = _runtime(tmp_path / "runtime")
    manifest = tmp_path / "runtime-manifest.json"
    _write(root, manifest)
    (root / "DLLs" / "injected.dll").write_bytes(b"dll")
    (root / "helper.exe").write_bytes(b"exe")
    findings = verify_tree(root, load_manifest(manifest), strict_extras=True)
    assert any("unexpected runtime executable/DLL: helper.exe" in item for item in findings)
    assert any("unexpected runtime executable/DLL: DLLs/injected.dll" in item for item in findings)


def test_a_missing_runtime_file_is_reported(tmp_path: Path):
    root = _runtime(tmp_path / "runtime")
    manifest = tmp_path / "runtime-manifest.json"
    _write(root, manifest)
    (root / "DLLs" / "sqlite3.dll").unlink()
    findings = verify_tree(root, load_manifest(manifest), strict_extras=True)
    assert "missing runtime file: DLLs/sqlite3.dll" in findings


def test_a_malformed_manifest_fails_closed(tmp_path: Path):
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeManifestError):
        load_manifest(manifest)


def test_the_committed_manifest_covers_every_shipped_interpreter_file():
    committed = load_manifest(REPO / "app" / "backend" / "runtime-manifest.json")
    paths = {entry["path"] for entry in committed["files"]}
    assert {"python.exe", "python3.dll", "python312.dll"} <= paths
    assert any(path.startswith("DLLs/") for path in paths)
    assert all(entry["sha256"] for entry in committed["files"])
