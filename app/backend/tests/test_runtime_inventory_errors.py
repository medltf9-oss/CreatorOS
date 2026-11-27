"""Failure modes of scripts/runtime-inventory.py.

A missing payload or an unreadable file must be an explicit tooling error (exit 2), not a traceback
or a policy finding (exit 1). The script must also require the payload rather than falling back to a
stale release directory.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "runtime-inventory.py"


def _inventory_module():
    spec = importlib.util.spec_from_file_location("runtime_inventory_for_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_payload_is_required() -> None:
    result = _run("--no-sbom")
    assert result.returncode == 2
    assert "required" in result.stderr.lower()


def test_a_missing_payload_is_a_tooling_error_not_a_policy_finding(tmp_path: Path) -> None:
    result = _run("--payload", str(tmp_path / "absent"), "--no-sbom")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "payload" in result.stderr.lower()


def test_a_mid_walk_oserror_is_a_tooling_error(monkeypatch, tmp_path: Path) -> None:
    module = _inventory_module()

    def denied(*args, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(module.os, "walk", denied)
    with pytest.raises(module.InventoryError, match="cannot walk payload"):
        module._walk_payload(tmp_path)


def test_a_missing_repository_manifest_is_a_tooling_error(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / "Lib" / "site-packages").mkdir(parents=True)
    (payload / "python.exe").write_bytes(b"MZ")
    result = _run(
        "--payload",
        str(payload),
        "--repo-root",
        str(tmp_path),
        "--pyproject",
        str(REPO / "app" / "backend" / "pyproject.toml"),
        "--no-sbom",
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "manifest" in result.stderr.lower()
