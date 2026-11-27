#!/usr/bin/env python3
"""Trusted manifest for the embedded CPython runtime.

The shipped backend is a signed CPython distribution plus a Python payload. A distribution
`RECORD` cannot cover the interpreter itself, so the interpreter needs its own manifest. This
module owns that manifest: the hash list is committed to git and is *never regenerated during a
build*. `build-backend.ps1` verifies the source interpreter, then the assembled payload, against
the committed list; `runtime-inventory.py` verifies the same list again before the artifact gate.

Trust model: the manifest is only as trustworthy as the commit that contains it. It detects a
replaced or modified interpreter file in the payload; it does not replace Authenticode and does not
protect a checkout where an attacker can rewrite both the payload and this manifest. The desktop
asar/signature and the release gate are the outer anchors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any

SCHEMA = "creatoros.runtime-manifest.v1"
MANIFEST_RELATIVE = Path("app") / "backend" / "runtime-manifest.json"
ROOT_FILES = (
    "python.exe",
    "python3.dll",
    "python312.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)
PROTECTED_SUFFIXES = frozenset({".exe", ".dll", ".pyd"})


class RuntimeManifestError(RuntimeError):
    """The manifest is missing, malformed, or does not match the runtime on disk."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> str:
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise RuntimeManifestError(f"unsafe manifest path: {value!r}")
    return candidate.as_posix()


def _runtime_files(root: Path) -> list[str]:
    files: list[str] = []
    for name in ROOT_FILES:
        candidate = root / name
        if candidate.is_file():
            files.append(name)
    dlls = root / "DLLs"
    if not dlls.is_dir():
        raise RuntimeManifestError(f"runtime has no DLLs directory: {dlls}")
    files.extend("DLLs/" + item.name for item in sorted(dlls.iterdir()) if item.is_file())
    return files


def build_manifest(root: Path, *, python_version: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    if not (root / "python.exe").is_file():
        raise RuntimeManifestError(f"no python.exe under {root}")
    files: list[dict[str, Any]] = []
    for relative in _runtime_files(root):
        path = root / relative
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0) or 0) & 0x400:
            raise RuntimeManifestError(f"refusing a symlink/reparse point: {relative}")
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeManifestError(f"not a regular file: {relative}")
        files.append({
            "path": relative,
            "size_bytes": info.st_size,
            "sha256": sha256_file(path),
        })
    version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "schema": SCHEMA,
        "python_version": version,
        "source": "Python Software Foundation CPython Windows installation; hashes recorded from the signed base interpreter at manifest creation time",
        "files": files,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeManifestError(f"cannot read runtime manifest {path}: {error}") from error
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise RuntimeManifestError(f"{path} is not a {SCHEMA} document")
    entries = data.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeManifestError(f"{path} has no files list")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeManifestError(f"{path} has a non-object file entry")
        relative = _safe_relative(str(entry.get("path") or ""))
        if relative in seen:
            raise RuntimeManifestError(f"{path} lists {relative!r} more than once")
        seen.add(relative)
    return data


def verify_tree(root: Path, manifest: dict[str, Any], *, strict_extras: bool = True) -> list[str]:
    """Return every mismatch. An empty list means the runtime matches the committed manifest."""
    root = root.resolve()
    findings: list[str] = []
    listed = {_safe_relative(str(item["path"])) for item in manifest["files"]}
    for entry in manifest["files"]:
        relative = _safe_relative(str(entry["path"]))
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            findings.append(f"missing runtime file: {relative}")
            continue
        except OSError as error:
            findings.append(f"cannot inspect {relative}: {error}")
            continue
        if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0) or 0) & 0x400:
            findings.append(f"runtime file is a symlink/reparse point: {relative}")
            continue
        if not stat.S_ISREG(info.st_mode):
            findings.append(f"runtime file is not regular: {relative}")
            continue
        expected_size = int(entry.get("size_bytes") or -1)
        if info.st_size != expected_size:
            findings.append(f"size mismatch for {relative}: expected {expected_size}, found {info.st_size}")
            continue
        try:
            actual = sha256_file(path)
        except OSError as error:
            findings.append(f"cannot hash {relative}: {error}")
            continue
        if actual != str(entry.get("sha256") or "").lower():
            findings.append(f"SHA-256 mismatch for {relative}")
    if strict_extras:
        for folder, prefix in ((root, ""), (root / "DLLs", "DLLs/")):
            if not folder.is_dir():
                continue
            for item in sorted(folder.iterdir()):
                if not item.is_file() or item.suffix.lower() not in PROTECTED_SUFFIXES:
                    continue
                relative = prefix + item.name
                if relative not in listed:
                    findings.append(f"unexpected runtime executable/DLL: {relative}")
    return findings


def assert_tree(root: Path, manifest_path: Path, *, strict_extras: bool = True) -> None:
    findings = verify_tree(root, load_manifest(manifest_path), strict_extras=strict_extras)
    if findings:
        raise RuntimeManifestError("; ".join(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or verify the embedded CPython runtime manifest.")
    parser.add_argument("--write", action="store_true", help="record hashes from --root")
    parser.add_argument("--check", action="store_true", help="verify --root against the committed manifest")
    parser.add_argument("--root", required=True, help="interpreter root or assembled payload")
    parser.add_argument("--manifest", default=None, help="default: app/backend/runtime-manifest.json")
    parser.add_argument("--strict-extras", action="store_true", help="reject unlisted .exe/.dll/.pyd files")
    args = parser.parse_args(argv)
    if args.write == args.check:
        parser.error("choose exactly one of --write or --check")
    repo = Path(__file__).resolve().parent.parent
    manifest_path = Path(args.manifest) if args.manifest else repo / MANIFEST_RELATIVE
    root = Path(args.root)
    try:
        if args.write:
            document = build_manifest(root)
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"wrote {manifest_path} ({len(document['files'])} files)")
        else:
            findings = verify_tree(root, load_manifest(manifest_path), strict_extras=args.strict_extras)
            if findings:
                for finding in findings:
                    print(f"runtime-manifest: {finding}", file=sys.stderr)
                return 1
            print(f"runtime manifest verified: {len(load_manifest(manifest_path)['files'])} files")
    except RuntimeManifestError as error:
        print(f"runtime-manifest: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())