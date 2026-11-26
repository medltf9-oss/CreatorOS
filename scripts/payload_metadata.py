"""Write and verify canonical first-party metadata inside a packaged backend payload.

The build copies a developer virtualenv, which can contain an editable install from an older version.
That dist-info describes the build machine, not the payload. This script removes it and writes
METADATA, WHEEL, INSTALLER, top_level.txt and a RECORD over the files actually shipped. It uses only
the standard library, so the payload interpreter can run it with -B after the final __pycache__ strip.

    python payload_metadata.py --payload <backend> --project <app/backend> --version <version>
    python payload_metadata.py --check --payload <backend> --project <app/backend>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import tomllib
from pathlib import Path

METADATA_NAME = "creatoros-backend"
DIST_PREFIX = "creatoros_backend-"


def digest(path: Path) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def record_line(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return f"{relative},{digest(path)},{path.stat().st_size}"


def rendered_metadata(project: dict, version: str) -> str:
    authors = ", ".join(str(item.get("name", "")) for item in project.get("authors") or [])
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {METADATA_NAME}",
        f"Version: {version}",
        f"Summary: {project.get("description", "CreatorOS Studio local backend")}",
    ]
    if authors:
        lines.append(f"Author: {authors}")
    licence = project.get("license") or {}
    if isinstance(licence, dict):
        licence = licence.get("text") or licence.get("file") or ""
    if licence:
        lines.append(f"License: {licence}")
    lines.append(f"Requires-Python: {project.get("requires-python", ">=3.12")}")
    for requirement in project.get("dependencies") or []:
        lines.append(f"Requires-Dist: {requirement}")
    for extra, requirements in sorted((project.get("optional-dependencies") or {}).items()):
        lines.append(f"Provides-Extra: {extra}")
        for requirement in requirements:
            lines.append(f"Requires-Dist: {requirement}; extra == \"{extra}\"")
    return "\n".join(lines) + "\n"


def rendered_wheel() -> str:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: creatoros-build-backend\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )


def expected_files(site_packages: Path, dist_info: Path) -> list[Path]:
    package = site_packages / "creatoros"
    if not (package / "__init__.py").is_file():
        raise SystemExit(f"creatoros package is missing from {site_packages}")
    if any(item.name == "__pycache__" for item in package.rglob("*")):
        raise SystemExit("payload still contains __pycache__; strip bytecode before writing metadata")
    files = sorted(item for item in package.rglob("*") if item.is_file())
    files.extend([
        dist_info / "METADATA",
        dist_info / "WHEEL",
        dist_info / "top_level.txt",
        dist_info / "INSTALLER",
    ])
    return files


def write_payload(payload: Path, project_dir: Path, version: str) -> int:
    site_packages = payload / "Lib" / "site-packages"
    pyproject = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project") or {}
    declared = str(project.get("version") or "")
    if declared != version:
        raise SystemExit(f"pyproject version {declared} does not match --version {version}")
    for stale in site_packages.glob(DIST_PREFIX + "*.dist-info"):
        if stale.is_dir():
            raise SystemExit(f"stale first-party metadata was not removed: {stale}")
    dist_info = site_packages / f"{DIST_PREFIX}{version}.dist-info"
    if dist_info.exists():
        raise SystemExit(f"first-party metadata already exists: {dist_info}")
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(rendered_metadata(project, version), encoding="utf-8", newline="\n")
    (dist_info / "WHEEL").write_text(rendered_wheel(), encoding="utf-8", newline="\n")
    (dist_info / "top_level.txt").write_text("creatoros\n", encoding="utf-8", newline="\n")
    (dist_info / "INSTALLER").write_text("CreatorOS build\n", encoding="utf-8", newline="\n")
    files = expected_files(site_packages, dist_info)
    lines = [record_line(path, site_packages) for path in files]
    record_path = dist_info / "RECORD"
    lines.append(f"{record_path.relative_to(site_packages).as_posix()},,")
    record_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {dist_info.name}: {len(files)} files, version {version}")
    return 0


def check_payload(payload: Path, project_dir: Path) -> int:
    site_packages = payload / "Lib" / "site-packages"
    pyproject = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project") or {}
    version = str(project.get("version") or "")
    dist_infos = sorted(site_packages.glob(DIST_PREFIX + "*.dist-info"))
    if len(dist_infos) != 1:
        print(f"expected exactly one first-party dist-info, found {len(dist_infos)}", file=sys.stderr)
        return 1
    dist_info = dist_infos[0]
    problems: list[str] = []
    if dist_info.name != f"{DIST_PREFIX}{version}.dist-info":
        problems.append(f"dist-info {dist_info.name} does not match project version {version}")
    expected_metadata = rendered_metadata(project, version)
    actual_metadata = (dist_info / "METADATA").read_text(encoding="utf-8")
    if actual_metadata != expected_metadata:
        problems.append("METADATA does not match pyproject")
    if (dist_info / "WHEEL").read_text(encoding="utf-8") != rendered_wheel():
        problems.append("WHEEL is not the canonical record")
    record_path = dist_info / "RECORD"
    if not record_path.is_file():
        problems.append("RECORD is missing")
    else:
        rows = [line for line in record_path.read_text(encoding="utf-8").splitlines() if line]
        recorded: set[str] = set()
        for line in rows:
            relative, recorded_hash, recorded_size = (line.split(",") + ["", "", ""])[:3]
            target = (site_packages / relative).resolve()
            if not target.is_relative_to(site_packages.resolve()) or not target.is_file():
                problems.append(f"RECORD entry is missing or unsafe: {relative}")
                continue
            recorded.add(target.relative_to(site_packages).as_posix())
            if recorded_size and target.stat().st_size != int(recorded_size):
                problems.append(f"RECORD size mismatch: {relative}")
            if recorded_hash and recorded_hash != digest(target):
                problems.append(f"RECORD hash mismatch: {relative}")
        actual = {
            item.relative_to(site_packages).as_posix()
            for item in site_packages.rglob("*")
            if item.is_file()
            and (item.is_relative_to(site_packages / "creatoros") or item.is_relative_to(dist_info))
        }
        if recorded != actual:
            problems.append("RECORD does not claim exactly the first-party files")
    if problems:
        print("first-party metadata check failed:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    print(f"first-party metadata check passed: {dist_info.name}, version {version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path, help="app/backend directory")
    parser.add_argument("--version")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check_payload(args.payload.resolve(), args.project.resolve())
    if not args.version:
        parser.error("--version is required unless --check is used")
    return write_payload(args.payload.resolve(), args.project.resolve(), args.version)


if __name__ == "__main__":
    raise SystemExit(main())