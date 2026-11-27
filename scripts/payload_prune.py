"""Prune a copied site-packages tree to the distributions the runtime lock allows.

The build starts from the developer virtualenv for speed and then removes every distribution that is
not in requirements-runtime.lock (plus the first-party project). Removing only the dist-info would
leave orphaned modules, so each removed RECORD is replayed first. Any remaining file that no RECORD
claims is reported and fails the build.
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

PROJECT_DISTRIBUTIONS = frozenset({"creatoros-backend"})


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, _version = line.partition("==")
        if not separator:
            raise SystemExit(f"invalid lock line: {raw!r}")
        names.add(normalize(name))
    return names


def metadata_name(dist_info: Path) -> str:
    metadata = dist_info / "METADATA"
    if metadata.is_file():
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("name:"):
                return normalize(line.split(":", 1)[1].strip())
    return normalize(dist_info.name.split("-", 1)[0])


def record_paths(dist_info: Path) -> list[str]:
    record = dist_info / "RECORD"
    if not record.is_file():
        return []
    paths: list[str] = []
    for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = line.split(",", 1)[0].strip().replace("\\", "/")
        if entry and not entry.startswith("/") and ".." not in entry.split("/"):
            paths.append(entry)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    args = parser.parse_args(argv)
    site_packages = (args.payload / "Lib" / "site-packages").resolve()
    if not site_packages.is_dir():
        raise SystemExit(f"site-packages not found: {site_packages}")
    allowed = lock_names(args.lock) | PROJECT_DISTRIBUTIONS
    removed: list[str] = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        if not dist_info.is_dir():
            continue
        name = metadata_name(dist_info)
        if name in allowed:
            continue
        for relative in record_paths(dist_info):
            target = (site_packages / relative).resolve()
            if target.is_relative_to(site_packages) and target.is_file():
                target.unlink()
        shutil.rmtree(dist_info, ignore_errors=True)
        removed.append(dist_info.name)
    claimed: set[Path] = set()
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        for relative in record_paths(dist_info):
            target = (site_packages / relative).resolve()
            if target.is_relative_to(site_packages):
                claimed.add(target)
    unowned = sorted(
        path.relative_to(site_packages).as_posix()
        for path in site_packages.rglob("*")
        if path.is_file()
        and not any(part.endswith(".dist-info") for part in path.relative_to(site_packages).parts)
        and path.resolve() not in claimed
    )
    if unowned:
        print("unowned site-packages files remain after pruning:")
        for item in unowned:
            print("  " + item)
        return 1
    print(f"pruned {len(removed)} distribution(s) outside the lock: " + ", ".join(removed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())