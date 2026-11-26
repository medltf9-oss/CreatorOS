"""The packaged first-party dist-info matches pyproject.toml and the shipped bytes."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "payload_metadata.py"
PYPROJECT = REPO_ROOT / "app" / "backend" / "pyproject.toml"
SETTINGS = REPO_ROOT / "app" / "backend" / "src" / "creatoros" / "core" / "settings.py"
MAIN = REPO_ROOT / "app" / "backend" / "src" / "creatoros" / "main.py"
ENTRY = REPO_ROOT / "app" / "backend" / "src" / "creatoros" / "__main__.py"
PACKAGE_JSON = REPO_ROOT / "app" / "desktop" / "package.json"


def load_module():
    spec = importlib.util.spec_from_file_location("payload_metadata", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


payload_metadata = load_module()


def project_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_every_product_version_literal_matches_pyproject():
    version = project_version()
    assert json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["version"] == version
    assert re.search(r'APP_VERSION = "([^"]+)"', ENTRY.read_text(encoding="utf-8")).group(1) == version
    assert re.search(r'version: str = "([^"]+)"', SETTINGS.read_text(encoding="utf-8")).group(1) == version
    assert re.search(r'version="([^"]+)"', MAIN.read_text(encoding="utf-8")).group(1) == version


def make_payload(tmp_path: Path) -> Path:
    site_packages = tmp_path / "payload" / "Lib" / "site-packages"
    package = site_packages / "creatoros"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text("print('creatoros')\n", encoding="utf-8")
    return tmp_path / "payload"


def test_writer_records_exactly_the_payload_files(tmp_path, capsys):
    payload = make_payload(tmp_path)
    version = project_version()
    assert payload_metadata.main([
        "--payload", str(payload), "--project", str(PYPROJECT.parent), "--version", version
    ]) == 0
    dist_info = payload / "Lib" / "site-packages" / f"creatoros_backend-{version}.dist-info"
    metadata = (dist_info / "METADATA").read_text(encoding="utf-8")
    assert f"Version: {version}" in metadata
    assert "License: Apache-2.0" in metadata
    assert "Provides-Extra: dev" in metadata
    expected = {
        path.relative_to(dist_info.parent).as_posix()
        for path in (dist_info.parent / "creatoros").rglob("*")
        if path.is_file()
    }
    rows = (dist_info / "RECORD").read_text(encoding="utf-8").splitlines()
    claimed = {row.split(",", 1)[0] for row in rows}
    assert expected <= claimed
    for row in rows:
        relative, recorded_hash, recorded_size = (row.split(",") + ["", "", ""])[:3]
        if not recorded_hash:
            continue
        target = dist_info.parent / relative
        assert target.stat().st_size == int(recorded_size)
        encoded = base64.urlsafe_b64encode(hashlib.sha256(target.read_bytes()).digest()).rstrip(b"=").decode()
        assert recorded_hash == "sha256=" + encoded
    assert payload_metadata.main(["--check", "--payload", str(payload), "--project", str(PYPROJECT.parent)]) == 0


def test_check_detects_tampered_payload_bytes(tmp_path):
    payload = make_payload(tmp_path)
    version = project_version()
    assert payload_metadata.main([
        "--payload", str(payload), "--project", str(PYPROJECT.parent), "--version", version
    ]) == 0
    (payload / "Lib" / "site-packages" / "creatoros" / "__main__.py").write_text("tampered\n", encoding="utf-8")
    assert payload_metadata.main(["--check", "--payload", str(payload), "--project", str(PYPROJECT.parent)]) == 1


def test_writer_refuses_bytecode_left_in_the_package(tmp_path):
    payload = make_payload(tmp_path)
    cache = payload / "Lib" / "site-packages" / "creatoros" / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"bytecode")
    with pytest.raises(SystemExit):
        payload_metadata.main([
            "--payload", str(payload), "--project", str(PYPROJECT.parent), "--version", project_version()
        ])