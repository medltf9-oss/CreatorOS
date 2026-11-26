"""Tests for scripts/runtime-inventory.py.

The real-bundle tests read the packaged 0.8.0 payload and skip only when that
payload is absent. They assert what the packaged files prove, not what the
release notes claim: a defect in the payload is reported as data and never
hidden by loosening an expectation here.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "runtime-inventory.py"
BUNDLE = REPO_ROOT / "releases" / "0.8.0" / "win-unpacked" / "resources" / "backend"
SBOM = REPO_ROOT / "releases" / "0.8.0" / "sbom.cdx.json"
PYPROJECT = REPO_ROOT / "app" / "backend" / "pyproject.toml"


def load_module():
    spec = importlib.util.spec_from_file_location("runtime_inventory", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


runtime_inventory = load_module()


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def walk_files(root: Path) -> list[Path]:
    return sorted(item for item in root.rglob("*") if item.is_file())


def metadata_of(dist_info: Path) -> tuple[str, str]:
    raw = (dist_info / "METADATA").read_bytes()
    message = BytesParser(policy=policy.compat32).parsebytes(raw)
    return (message.get("Name") or "").strip(), (message.get("Version") or "").strip()


def record_row(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return name + ",sha256=" + digest + "," + str(len(data))


def write_metadata(dist_info: Path, lines: list[str]) -> None:
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_dist(root: Path, dist_info_name: str, options: dict) -> Path:
    dist_info = root / "Lib" / "site-packages" / dist_info_name
    write_metadata(dist_info, options["metadata"])
    if options.get("record_rows") is not None:
        rows = options["record_rows"]
        (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    for relative, data in (options.get("present") or {}).items():
        target = root / "Lib" / "site-packages" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dist_info


def make_payload(tmp_path: Path, *, with_python: bool = True) -> Path:
    root = tmp_path / "payload"
    (root / "Lib" / "site-packages").mkdir(parents=True)
    (root / "DLLs").mkdir()
    if with_python:
        (root / "python.exe").write_bytes(b"MZ fake interpreter")
    (root / "Lib" / "os.py").write_text("# stdlib stand-in\n", encoding="utf-8")
    return root


def write_pyproject(tmp_path: Path, dependencies: tuple[str, ...] = ()) -> Path:
    lines = [
        "[project]",
        "name = \"creatoros-backend\"",
        "version = \"0.8.0\"",
        "dependencies = [",
    ]
    lines.extend(f"  \"{item}\"," for item in dependencies)
    lines.append("]")
    path = tmp_path / "pyproject.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_inventory(
    payload: Path,
    *extra: str,
    pyproject: Path | None = None,
    sbom: Path | None = None,
) -> subprocess.CompletedProcess:
    command = [sys.executable, str(SCRIPT), "--payload", str(payload)]
    if pyproject is None:
        command.append("--no-sbom")
    else:
        command.extend(["--pyproject", str(pyproject)])
        if sbom is None:
            command.append("--no-sbom")
        else:
            command.extend(["--sbom", str(sbom)])
    command.extend(extra)
    return subprocess.run(command, capture_output=True, text=True, check=False)


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def simple_dist(name: str, version: str, relative: str = "widget.py") -> dict:
    data = ("# " + name + "\n").encode("utf-8")
    return {
        "metadata": [
            "Metadata-Version: 2.4",
            "Name: " + name,
            "Version: " + version,
            "License-Expression: MIT",
        ],
        "record_rows": [record_row(relative, data)],
        "present": {relative: data},
    }


def test_unrecorded_site_packages_file_is_fatal(tmp_path):
    payload = make_payload(tmp_path)
    write_dist(payload, 'widget-1.0.dist-info', simple_dist('widget', '1.0'))
    (payload / 'Lib' / 'site-packages' / 'README.txt').write_text('placeholder', encoding='utf-8')
    pyproject = write_pyproject(tmp_path, ('widget>=1.0',))
    output = tmp_path / 'inventory.json'
    result = run_inventory(payload, '--json-out', str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    assert report['provenance']['complete'] is False
    paths = [item['path'] for item in report['violations']['unrecorded_site_packages_files']]
    assert 'Lib/site-packages/README.txt' in paths
    assert any('unrecorded site-packages' in cause for cause in report['summary']['causes'])


def test_first_party_version_mismatch_is_fatal(tmp_path):
    payload = make_payload(tmp_path)
    write_dist(
        payload,
        'creatoros_backend-0.1.0.dist-info',
        simple_dist('creatoros-backend', '0.1.0', 'creatoros/__init__.py'),
    )
    pyproject = write_pyproject(tmp_path, ('widget>=1.0',))
    output = tmp_path / 'inventory.json'
    result = run_inventory(payload, '--json-out', str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    assert report['declared_runtime_dependencies']['first_party_version_mismatch'] is not None
    assert any('first-party version mismatch' in cause for cause in report['summary']['causes'])

# ---------------------------------------------------------------------------
# synthetic payloads
# ---------------------------------------------------------------------------


def test_clean_payload_exits_zero_and_balances(tmp_path):
    payload = make_payload(tmp_path)
    write_dist(payload, "widget-1.0.dist-info", simple_dist("widget", "1.0"))
    pyproject = write_pyproject(tmp_path, ("widget>=1.0",))
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 0, result.stderr
    report = read_report(output)
    assert report["schema"] == {"name": "creatoros-runtime-inventory", "version": 1}
    assert report["payload"]["file_count"] == len(walk_files(payload))
    assert report["payload"]["total_bytes"] == sum(item.stat().st_size for item in walk_files(payload))
    assert report["payload"]["exe_count"] == 1
    assert report["accounting"]["balanced"] is True
    assert (
        report["accounting"]["distinct_claimed_files"]
        + report["accounting"]["non_distribution_files"]
        == report["payload"]["file_count"]
    )
    distribution = report["distributions"][0]
    assert distribution["scope"] == "runtime"
    assert distribution["record_status"] == "ok"
    assert distribution["present_files"] == 1
    assert distribution["missing_files"] == []
    assert distribution["bytes"] == len(b"# widget\n")
    assert report["declared_runtime_dependencies"]["missing_from_bundle"] == []
    assert report["sbom_comparison"]["status"] == "not-run"
    assert report["summary"]["exit_code"] == 0
    assert report["violations"]["dev_only_distributions"] == []


def test_missing_record_is_fatal_and_reported(tmp_path):
    payload = make_payload(tmp_path)
    write_dist(
        payload,
        "broken-1.0.dist-info",
        {"metadata": ["Metadata-Version: 2.4", "Name: broken", "Version: 1.0"]},
    )
    pyproject = write_pyproject(tmp_path)
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    distribution = report["distributions"][0]
    assert distribution["record_status"] == "missing"
    assert distribution["files"] == []
    assert distribution["declared_files"] == 0
    assert report["violations"]["record_errors"] == [
        {"distribution": "broken-1.0.dist-info", "error": "no RECORD file"}
    ]
    assert "1 RECORD problem(s)" in report["summary"]["causes"]


def test_malformed_metadata_is_fatal_and_falls_back_to_directory_name(tmp_path):
    payload = make_payload(tmp_path)
    write_dist(
        payload,
        "broken-2.0.dist-info",
        {"metadata": ["Metadata-Version: 2.4", "Name: broken"], "record_rows": []},
    )
    pyproject = write_pyproject(tmp_path)
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    distribution = report["distributions"][0]
    assert distribution["metadata_status"] == "malformed"
    assert distribution["name"] == "broken"
    assert distribution["version"] == "2.0"
    joined = " ".join(item["error"] for item in report["violations"]["metadata_errors"])
    assert "Version" in joined
    assert "1 malformed METADATA file(s)" in report["summary"]["causes"]


def test_duplicate_distribution_names_are_fatal(tmp_path):
    payload = make_payload(tmp_path)
    write_dist(payload, "dup-1.0.dist-info", simple_dist("dup", "1.0"))
    write_dist(payload, "dup-2.0.dist-info", simple_dist("dup", "2.0"))
    pyproject = write_pyproject(tmp_path)
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    assert report["violations"]["duplicate_distribution_names"] == [
        {
            "name": "dup",
            "versions": ["1.0", "2.0"],
            "dist_infos": ["dup-1.0.dist-info", "dup-2.0.dist-info"],
        }
    ]
    assert len(report["distributions"]) == 2
    assert "1 duplicate distribution name(s)" in report["summary"]["causes"]


def test_record_entry_leaving_payload_is_fatal(tmp_path):
    payload = make_payload(tmp_path)
    escape = payload.parent / "outside.txt"
    escape.write_text("do not read me from a RECORD\n", encoding="utf-8")
    write_dist(
        payload,
        "escape-1.0.dist-info",
        {
            "metadata": ["Metadata-Version: 2.4", "Name: escape", "Version: 1.0"],
            "record_rows": ["../../../outside.txt,,11"],
        },
    )
    pyproject = write_pyproject(tmp_path)
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    entries = report["violations"]["out_of_payload_record_entries"]
    assert len(entries) == 1
    assert entries[0]["distribution"] == "escape-1.0.dist-info"
    assert entries[0]["entry"] == "../../../outside.txt"
    assert entries[0]["reason"] == "resolves outside the payload"
    assert entries[0]["resolved"].endswith("outside.txt")
    assert "1 RECORD entry/entries leave the payload" in report["summary"]["causes"]


@pytest.mark.parametrize(
    "entry",
    [
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "..\\..\\evil.txt",
        "widget.py\x00.txt",
        "widgets//empty.py",
    ],
)
def test_unsafe_record_paths_are_fatal(tmp_path, entry):
    payload = make_payload(tmp_path)
    write_dist(
        payload,
        "unsafe-1.0.dist-info",
        {
            "metadata": ["Metadata-Version: 2.4", "Name: unsafe", "Version: 1.0"],
            "record_rows": [entry + ",,0"],
        },
    )
    pyproject = write_pyproject(tmp_path)
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    assert report["violations"]["unsafe_record_paths"]
    assert report["violations"]["out_of_payload_record_entries"]
    assert report["violations"]["out_of_payload_record_entries"][0]["entry"] == entry


def test_parent_relative_entry_inside_payload_is_reported_not_fatal(tmp_path):
    payload = make_payload(tmp_path)
    script = payload / "Scripts" / "tool.exe"
    script.parent.mkdir()
    script.write_bytes(b"MZ script")
    write_dist(
        payload,
        "scripts-1.0.dist-info",
        {
            "metadata": ["Metadata-Version: 2.4", "Name: scripts", "Version: 1.0"],
            "record_rows": [record_row("../../Scripts/tool.exe", b"MZ script")],
        },
    )
    pyproject = write_pyproject(tmp_path)
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 0, result.stderr
    report = read_report(output)
    assert report["violations"]["out_of_payload_record_entries"] == []
    assert report["distributions"][0]["present_files"] == 1
    assert report["distributions"][0]["missing_files"] == []
    assert report["integrity"]["outside_site_packages"] == [
        {"distribution": "scripts-1.0.dist-info", "entry": "../../Scripts/tool.exe"}
    ]


def test_missing_interpreter_is_fatal(tmp_path):
    payload = make_payload(tmp_path, with_python=False)
    write_dist(payload, "widget-1.0.dist-info", simple_dist("widget", "1.0"))
    pyproject = write_pyproject(tmp_path)
    output = tmp_path / "inventory.json"
    result = run_inventory(payload, "--json-out", str(output), pyproject=pyproject)
    assert result.returncode == 1
    report = read_report(output)
    assert "payload has no python.exe" in report["violations"]["payload_errors"]
    assert report["interpreter"]["interpreter_executable"] is None


def test_output_is_deterministic(tmp_path):
    payload = make_payload(tmp_path)
    write_dist(payload, "widget-1.0.dist-info", simple_dist("widget", "1.0"))
    pyproject = write_pyproject(tmp_path, ("widget>=1.0",))
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert run_inventory(payload, "--json-out", str(first), pyproject=pyproject).returncode == 0
    assert run_inventory(payload, "--json-out", str(second), pyproject=pyproject).returncode == 0
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


# __REAL_TESTS__