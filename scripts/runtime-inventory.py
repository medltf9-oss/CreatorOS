#!/usr/bin/env python3
"""Inventory the packaged CreatorOS backend runtime from the files that ship.

The installer does not ship a frozen executable: it ships a signed CPython
tree (``python.exe``, ``python312.dll``, ``DLLs``, ``Lib``) plus the backend
package, migrations and an ``alembic.ini``.  This script reads that tree and
writes a deterministic JSON inventory plus a Markdown summary.  It does not
prune, fix or normalise anything: every discrepancy it can see is reported.

What it reads
  * ``Lib/site-packages/*.dist-info/METADATA`` for name, version and licence
    claims (``License-Expression``, ``License``, ``License ::`` classifiers).
  * ``Lib/site-packages/*.dist-info/RECORD`` for the declared file list, the
    declared sizes and the recorded hashes.
  * the payload tree itself for measured file sizes, the interpreter/DLL PE
    version resources and the non-distribution files (stdlib, extension
    modules, migrations, ``python.exe``).
  * ``app/backend/pyproject.toml`` for the declared runtime dependencies.
  * the artifact CycloneDX SBOM for the components the release paperwork
    claims, compared against what the payload actually contains.

Exit codes
  0  inventory completed, no policy violation
  1  inventory completed but found a policy violation or an integrity
     failure that makes the inventory untrustworthy
  2  the inventory could not be produced (missing payload, unreadable input)

Only the Python standard library is used.  Output is sorted and contains no
timestamps or machine-specific absolute paths below the repository root, so
two runs against the same payload produce byte-identical JSON.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import tomllib
from email import policy
from email.parser import BytesParser
from pathlib import Path

TOOL_VERSION = 1
TOOL_NAME = "creatoros-runtime-inventory"

DEFAULT_PAYLOAD = "releases/0.8.0/win-unpacked/resources/backend"
DEFAULT_SBOM = "releases/0.8.0/sbom.cdx.json"
DEFAULT_PYPROJECT = "app/backend/pyproject.toml"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

# The hard list from the release task: if any of these ships, the bundle fails.
FORBIDDEN_DISTRIBUTIONS = frozenset({
    "pytest",
    "ruff",
    "coverage",
    "pyinstaller",
    "pip",
    "setuptools",
    "wheel",
    "black",
    "mypy",
    "hypothesis",
})

# A wider development-only set: the task list plus the distributions that
# scripts/build-backend.ps1 removes (its $devDistributions table) and the
# specialised test/build helpers.  Scope "dev" is reported for all of them;
# only FORBIDDEN_DISTRIBUTIONS are named as the hard failure class.
DEV_ONLY_DISTRIBUTIONS = frozenset({
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "coverage",
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "altgraph",
    "pywin32-ctypes",
    "pefile",
    "ordlookup",
    "pygments",
    "packaging",
    "iniconfig",
    "ruff",
    "respx",
    "pluggy",
    "pip",
    "setuptools",
    "wheel",
    "black",
    "mypy",
    "hypothesis",
})

INTERPRETER_FILES = (
    "python.exe",
    "python3.dll",
    "python312.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)

RE_INTERPRETER_DLL = re.compile(r"^python(\d)(\d{1,2})\.dll$", re.IGNORECASE)
RE_SPECIFIER_CLAUSE = re.compile(r"^(===|==|!=|<=|>=|~=|<|>)\s*(.+)$")


class InventoryError(RuntimeError):
    """Raised when the inventory cannot be produced at all."""


def normalize_name(name: str) -> str:
    """PEP 503 normalisation, also used for dist-info directory names."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def display_path(path: Path, repo_root: Path) -> str:
    """Repository-relative POSIX path when possible (keeps output stable)."""
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except (ValueError, OSError):
        return path.as_posix()
    return relative.as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_release(text: str) -> tuple[int, ...] | None:
    """Return the numeric release tuple of a PEP 440 version, or None.

    Deliberately conservative: pre/dev/post/local versions are not compared
    (the caller reports them as not checked) so the tool never invents a
    comparison it cannot justify with the standard library alone.
    """
    candidate = text.strip()
    if candidate.startswith(("v", "V")):
        candidate = candidate[1:]
    if not candidate or not re.fullmatch(r"\d+(?:\.\d+)*", candidate):
        return None
    return tuple(int(part) for part in candidate.split("."))


def _compare_release(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _satisfies_clause(version: tuple[int, ...], operator: str, operand: str) -> bool | None:
    if operand.endswith(".*") and operator in {"==", "!="}:
        prefix = _version_release(operand[:-2])
        if prefix is None:
            return None
        matched = version[: len(prefix)] == prefix
        return matched if operator == "==" else not matched
    if operator == "!=":
        other = _version_release(operand)
        if other is None:
            return None
        return _compare_release(version, other) != 0
    if operator == "===":
        return None
    if operator == "~=":
        other = _version_release(operand)
        if other is None or len(other) < 2:
            return None
        lower = _compare_release(version, other) >= 0
        upper = _compare_release(version[: len(other) - 1], other[:-1]) == 0
        return lower and upper
    other = _version_release(operand)
    if other is None:
        return None
    comparison = _compare_release(version, other)
    return {
        "==": comparison == 0,
        ">=": comparison >= 0,
        "<=": comparison <= 0,
        ">": comparison > 0,
        "<": comparison < 0,
    }[operator]


def check_specifier(version: str, specifier: str) -> dict:
    """Evaluate a requirement specifier against a version.

    Returns {"status": "satisfied"|"not-satisfied"|"not-checked", "reason": str}.
    """
    result = {"status": "not-checked", "reason": ""}
    release = _version_release(version)
    if release is None:
        result["reason"] = f"packaged version {version!r} is not a numeric release"
        return result
    clauses = [part.strip() for part in specifier.split(",") if part.strip()]
    if not clauses:
        result["status"] = "satisfied"
        return result
    for clause in clauses:
        match = RE_SPECIFIER_CLAUSE.match(clause)
        if match is None:
            result["reason"] = f"unsupported specifier clause {clause!r}"
            return result
        operator, operand = match.group(1), match.group(2).strip()
        if operator == "===":
            result["reason"] = f"arbitrary equality {clause!r} is not evaluated"
            return result
        accepted = _satisfies_clause(release, operator, operand)
        if accepted is None:
            result["reason"] = f"cannot evaluate clause {clause!r}"
            return result
        if not accepted:
            result["status"] = "not-satisfied"
            result["reason"] = f"{version} does not satisfy {clause!r}"
            return result
    result["status"] = "satisfied"
    return result


def split_requirement(requirement: str) -> tuple[str, str, list[str], str | None]:
    """Split a PEP 508 requirement into name, specifier, extras and marker."""
    marker = None
    if ";" in requirement:
        requirement, marker = requirement.split(";", 1)
        marker = marker.strip() or None
    requirement = requirement.strip()
    extras: list[str] = []
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*)$", requirement)
    if match is None:
        raise InventoryError(f"cannot parse requirement {requirement!r}")
    name = match.group(1)
    if match.group(2):
        extras = [part.strip() for part in match.group(2).split(",") if part.strip()]
    return name, match.group(3).strip(), extras, marker


def evaluate_marker(marker: str | None, target: dict[str, str]) -> dict:
    """Evaluate the small marker subset used by the project, honestly.

    Only ``VAR == "value"`` / ``VAR != "value"`` clauses joined by ``and`` are
    evaluated.  Anything else returns "not-evaluated" so the caller never
    treats an unknown marker as applicable or as satisfied.
    """
    if marker is None:
        return {"status": "applicable", "reason": ""}
    clauses = [part.strip() for part in marker.split(" and ")]
    for clause in clauses:
        match = re.fullmatch(r"([A-Za-z_]+)\s*(==|!=)\s*(['\"])(.*?)\3", clause)
        if match is None:
            return {
                "status": "not-evaluated",
                "reason": f"marker {marker!r} uses syntax this tool does not evaluate",
            }
        variable, operator, _, expected = match.groups()
        actual = target.get(variable)
        if actual is None:
            return {
                "status": "not-evaluated",
                "reason": f"marker variable {variable!r} is not modelled",
            }
        matched = actual == expected
        if (operator == "==") is not matched:
            return {"status": "not-applicable", "reason": f"marker {marker!r} is false"}
    return {"status": "applicable", "reason": ""}


def read_metadata(path: Path) -> dict:
    """Read a dist-info METADATA file, reporting rather than hiding damage."""
    info = {
        "metadata_status": "ok",
        "metadata_errors": [],
        "name": None,
        "version": None,
        "metadata_version": None,
        "license_expression": None,
        "license_field": None,
        "license_classifiers": [],
        "license_files": [],
        "requires_dist": [],
    }
    try:
        raw = path.read_bytes()
    except OSError as error:
        info["metadata_status"] = "malformed"
        info["metadata_errors"].append(f"cannot read METADATA: {error}")
        return info
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(raw)
    except Exception as error:  # noqa: BLE001 - report parser damage as data
        info["metadata_status"] = "malformed"
        info["metadata_errors"].append(f"cannot parse METADATA: {error}")
        return info
    info["name"] = (message.get("Name") or "").strip() or None
    info["version"] = (message.get("Version") or "").strip() or None
    info["metadata_version"] = (message.get("Metadata-Version") or "").strip() or None
    info["license_expression"] = (message.get("License-Expression") or "").strip() or None
    info["license_field"] = (message.get("License") or "").strip() or None
    info["license_classifiers"] = [
        value.strip()
        for key, value in message.items()
        if key.lower() == "classifier" and value.strip().lower().startswith("license")
    ]
    info["license_files"] = [
        value.strip()
        for key, value in message.items()
        if key.lower() == "license-file" and value.strip()
    ]
    info["requires_dist"] = [
        value.strip() for key, value in message.items() if key.lower() == "requires-dist"
    ]
    if not info["name"]:
        info["metadata_errors"].append("METADATA has no Name header")
    if not info["version"]:
        info["metadata_errors"].append("METADATA has no Version header")
    if info["metadata_errors"]:
        info["metadata_status"] = "malformed"
    return info


def read_record(path: Path) -> tuple[list[list[str]], list[str]]:
    """Read RECORD rows; return (rows, errors)."""
    errors: list[str] = []
    if not path.is_file():
        return [], ["no RECORD file"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return [], [f"cannot read RECORD: {error}"]
    rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(text))
    try:
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) < 3:
                errors.append(f"RECORD row has {len(row)} columns: {row!r}")
                continue
            rows.append(row[:3])
    except csv.Error as error:
        errors.append(f"RECORD is not valid CSV: {error}")
    if not errors and not rows:
        errors.append("RECORD is empty")
    return rows, errors


def validate_record_entry(entry: str) -> tuple[list[str] | None, str | None]:
    """Split a RECORD path into safe components, or explain why it is unsafe."""
    if not entry:
        return None, "empty path"
    if "\x00" in entry:
        return None, "NUL byte in path"
    if "\\" in entry:
        return None, "backslash in RECORD path; wheel RECORDs use forward slashes"
    if entry.startswith(("/", "~")):
        return None, "absolute or home-relative path"
    if re.match(r"^[A-Za-z]:", entry):
        return None, "drive-letter path"
    parts = entry.split("/")
    if any(part in {"", "."} for part in parts):
        return None, "empty or current-directory component"
    # A parent component is legal in a wheel RECORD (venv console scripts are
    # recorded as ../../Scripts/x.exe) but only if the resolved path stays in
    # the payload; the caller enforces that containment check.
    return parts, None


def _is_within(root: str, target: str) -> bool:
    try:
        common = os.path.commonpath([os.path.normcase(root), os.path.normcase(target)])
    except ValueError:
        return False
    return common == os.path.normcase(root)


def _record_digest_matches(path: Path, recorded: str) -> bool | None:
    """Compare a wheel RECORD sha256 (base64url) with the file contents."""
    if not recorded.startswith("sha256="):
        return None
    encoded = recorded[len("sha256=") :]
    try:
        expected = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError):
        return None
    return hashlib.sha256(path.read_bytes()).digest() == expected


def read_pe_versions(path: Path) -> dict:
    """Read the PE version resource with ctypes (no third-party PE library)."""
    result = {
        "status": "unavailable",
        "reason": "PE version resources can only be read on Windows",
        "file_version": None,
        "product_version": None,
        "company_name": None,
        "file_description": None,
    }
    if sys.platform != "win32":
        return result
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as error:  # noqa: BLE001 - ctypes is optional at runtime
        result["reason"] = f"ctypes unavailable: {error}"
        return result
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        result["reason"] = "no windll on this interpreter"
        return result
    try:
        version = windll.version
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            result["status"] = "no-version-resource"
            result["reason"] = "file has no version resource"
            return result
        buffer = ctypes.create_string_buffer(size)
        version.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            result["status"] = "unreadable"
            result["reason"] = "GetFileVersionInfoW failed"
            return result
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version.VerQueryValueW.restype = wintypes.BOOL
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        root_query = "\\VarFileInfo\\Translation"
        if not version.VerQueryValueW(buffer, root_query, ctypes.byref(pointer), ctypes.byref(length)):
            result["status"] = "unreadable"
            result["reason"] = "no translation table"
            return result
        count = length.value // 2
        translations = list(ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))[:count])
        strings: dict[str, str] = {}
        for index in range(0, len(translations), 2):
            language, codepage = translations[index], translations[index + 1]
            language_id = f"{language:04x}{codepage:04x}"
            for key in ("FileVersion", "ProductVersion", "CompanyName", "FileDescription"):
                sub = "\\StringFileInfo\\" + language_id + "\\" + key
                value_pointer = ctypes.c_void_p()
                value_length = wintypes.UINT()
                if version.VerQueryValueW(
                    buffer, sub, ctypes.byref(value_pointer), ctypes.byref(value_length)
                ):
                    strings[key] = ctypes.wstring_at(value_pointer, value_length.value).rstrip("\x00")
        result.update(
            {
                "status": "ok",
                "reason": "",
                "file_version": strings.get("FileVersion"),
                "product_version": strings.get("ProductVersion"),
                "company_name": strings.get("CompanyName"),
                "file_description": strings.get("FileDescription"),
            }
        )
    except Exception as error:  # noqa: BLE001 - PE parsing must not crash inventory
        result["status"] = "unreadable"
        result["reason"] = str(error)
    return result


def _scope_for(name: str, runtime_names: set[str], first_party: set[str]) -> tuple[str, str]:
    normalized = normalize_name(name)
    if normalized in DEV_ONLY_DISTRIBUTIONS:
        return "dev", "development-only distribution (task list or build-backend prune set)"
    if normalized in runtime_names:
        return "runtime", "declared in [project].dependencies of app/backend/pyproject.toml"
    if normalized in first_party:
        return "runtime", "first-party application package declared by the project"
    return "transitive", "shipped but neither a declared runtime dependency nor dev tooling"


def _classify_non_dist(relative: str) -> str:
    parts = relative.split("/")
    name = parts[-1]
    if relative == "python.exe":
        return "interpreter_executable"
    if len(parts) == 1 and name.lower().endswith(".dll"):
        return "interpreter_library"
    if parts[0] == "DLLs":
        if name.lower().endswith(".pyd"):
            return "extension_module"
        if name.lower().endswith(".dll"):
            return "extension_library"
        return "extension_data"
    if parts[0] == "migrations":
        if "__pycache__" in parts or name.endswith(".pyc"):
            return "migration_bytecode"
        return "migration_source"
    if parts[0] == "Lib" and len(parts) > 1 and parts[1] == "site-packages":
        return "site_packages_unrecorded"
    if parts[0] == "Lib":
        if "__pycache__" in parts or name.endswith(".pyc"):
            return "stdlib_bytecode"
        return "stdlib_source"
    return "payload_root_file"


def _walk_payload(payload: Path) -> list[str]:
    """Return every file path relative to the payload, sorted, POSIX style."""
    paths: list[str] = []
    for current, directories, files in os.walk(payload):
        directories.sort()
        files.sort()
        for name in files:
            paths.append((Path(current) / name).relative_to(payload).as_posix())
    paths.sort()
    return paths


def _load_pyproject(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise InventoryError(f"cannot read pyproject {path}: {error}") from error


def _load_sbom(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot read SBOM {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        raise InventoryError(f"SBOM {path} has no components list")
    return data


def _sbom_python_components(sbom: dict) -> tuple[list[dict], list[dict]]:
    """Split SBOM components into Python components and everything else."""
    python_components: list[dict] = []
    other_components: list[dict] = []
    for component in sbom.get("components", []):
        if not isinstance(component, dict):
            continue
        purl = str(component.get("purl") or "")
        name = str(component.get("name") or "")
        version = str(component.get("version") or "")
        record = {
            "name": name,
            "version": version,
            "purl": purl,
            "type": component.get("type"),
        }
        is_first_party = name == "creatoros-backend" and component.get("type") == "application"
        if purl.startswith("pkg:pypi/") or is_first_party:
            python_components.append(record)
        else:
            other_components.append(record)
    return python_components, other_components


def _dist_label(item: dict) -> str:
    return "{name} {version}".format(**item)


def build_inventory(
    payload: Path,
    repo_root: Path,
    pyproject_path: Path,
    sbom_path: Path | None,
    verify_hashes: bool = True,
) -> dict:
    payload = payload.resolve()
    if not payload.is_dir():
        raise InventoryError(f"payload directory not found: {payload}")
    site_packages = payload / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise InventoryError(f"payload has no Lib/site-packages: {site_packages}")

    payload_real = os.path.realpath(payload)
    site_packages_real = os.path.realpath(site_packages)
    payload_display = display_path(payload, repo_root)

    pyproject = _load_pyproject(pyproject_path)
    project = pyproject.get("project") or {}
    project_name = str(project.get("name") or "")
    project_version = str(project.get("version") or "")
    requirement_texts = [str(item) for item in project.get("dependencies") or []]
    runtime_names = {normalize_name(split_requirement(text)[0]) for text in requirement_texts}
    first_party = {normalize_name(project_name)} if project_name else set()

    payload_files = _walk_payload(payload)
    file_bytes: dict[str, int] = {}
    total_bytes = 0
    for relative in payload_files:
        size = (payload / relative).stat().st_size
        file_bytes[relative] = size
        total_bytes += size

    distributions: list[dict] = []
    violations: dict[str, list] = {
        "dev_only_distributions": [],
        "forbidden_distributions": [],
        "out_of_payload_record_entries": [],
        "unsafe_record_paths": [],
        "duplicate_distribution_names": [],
        "metadata_errors": [],
        "record_errors": [],
        "payload_errors": [],
        "unrecorded_site_packages_files": [],
    }
    claimed: dict[str, dict] = {}
    claimed_bytes = 0
    overlap: list[dict] = []
    missing_pyc = 0
    missing_console_scripts = 0
    missing_optional_extensions = 0
    missing_other = 0
    outside_site_packages: list[dict] = []
    size_mismatches: list[dict] = []
    hash_mismatches: list[dict] = []

    for dist_info in sorted(site_packages.glob("*.dist-info"), key=lambda item: item.name):
        if not dist_info.is_dir():
            continue
        metadata = read_metadata(dist_info / "METADATA")
        stem = dist_info.name[: -len(".dist-info")]
        fallback_name, _, fallback_version = stem.partition("-")
        name = metadata["name"] or fallback_name
        version = metadata["version"] or fallback_version
        normalized = normalize_name(name)
        scope, scope_basis = _scope_for(name, runtime_names, first_party)
        rows, record_errors = read_record(dist_info / "RECORD")
        record_status = "ok"
        if record_errors:
            record_status = "missing" if record_errors == ["no RECORD file"] else "malformed"
            for message in record_errors:
                violations["record_errors"].append(
                    {"distribution": dist_info.name, "error": message}
                )
        for message in metadata["metadata_errors"]:
            violations["metadata_errors"].append(
                {"distribution": dist_info.name, "error": message}
            )

        files: list[str] = []
        missing: list[str] = []
        present_bytes = 0
        present_count = 0
        declared_bytes = 0
        dist_outside_site_packages: list[str] = []
        seen: set[str] = set()
        duplicate_entries: list[str] = []
        for row in rows:
            entry, recorded_hash, recorded_size = row[0], row[1].strip(), row[2].strip()
            files.append(entry)
            if recorded_size:
                try:
                    declared_bytes += int(recorded_size)
                except ValueError:
                    violations["record_errors"].append(
                        {
                            "distribution": dist_info.name,
                            "error": f"RECORD size is not an integer for {entry!r}: {recorded_size!r}",
                        }
                    )
            parts, problem = validate_record_entry(entry)
            if problem is not None:
                violations["unsafe_record_paths"].append(
                    {"distribution": dist_info.name, "entry": entry, "reason": problem}
                )
                violations["out_of_payload_record_entries"].append(
                    {
                        "distribution": dist_info.name,
                        "entry": entry,
                        "reason": problem,
                        "resolved": None,
                    }
                )
                continue
            resolved = os.path.realpath(os.path.join(site_packages_real, *parts))
            if not _is_within(payload_real, resolved):
                violations["out_of_payload_record_entries"].append(
                    {
                        "distribution": dist_info.name,
                        "entry": entry,
                        "reason": "resolves outside the payload",
                        "resolved": resolved,
                    }
                )
                continue
            if not _is_within(site_packages_real, resolved):
                outside_site_packages.append({"distribution": dist_info.name, "entry": entry})
                dist_outside_site_packages.append(entry)
            key = os.path.normcase(resolved)
            if key in seen:
                duplicate_entries.append(entry)
                continue
            seen.add(key)
            if not os.path.isfile(resolved):
                missing.append(entry)
                if entry.endswith(".pyc"):
                    missing_pyc += 1
                elif "/Scripts/" in entry or entry.startswith("Scripts/"):
                    missing_console_scripts += 1
                elif entry.endswith(".pyd") and Path(
                    re.sub(r"\.(?:cp\d+-[^.]+\.)?pyd$", ".py", resolved)
                ).is_file():
                    # A compiled accelerator with a pure-Python sibling is optional; Smart App
                    # Control blocks unsigned third-party extensions on this build machine.
                    missing_optional_extensions += 1
                else:
                    missing_other += 1
                continue
            present_count += 1
            actual_bytes = os.path.getsize(resolved)
            present_bytes += actual_bytes
            if key in claimed:
                overlap.append(
                    {
                        "path": entry,
                        "distributions": [claimed[key]["distribution"], dist_info.name],
                    }
                )
            else:
                claimed[key] = {"distribution": dist_info.name, "path": entry}
                claimed_bytes += actual_bytes
            relative_to_payload = Path(resolved).relative_to(payload_real).as_posix()
            actual_size = file_bytes.get(relative_to_payload)
            if recorded_size and actual_size is not None:
                try:
                    if int(recorded_size) != actual_size:
                        size_mismatches.append(
                            {
                                "distribution": dist_info.name,
                                "path": entry,
                                "declared": int(recorded_size),
                                "actual": actual_size,
                            }
                        )
                except ValueError:
                    pass
            if verify_hashes:
                matches = _record_digest_matches(Path(resolved), recorded_hash)
                if matches is False:
                    hash_mismatches.append(
                        {"distribution": dist_info.name, "path": entry, "recorded": recorded_hash}
                    )

        if normalized in FORBIDDEN_DISTRIBUTIONS:
            violations["forbidden_distributions"].append(
                {"name": name, "normalized_name": normalized, "version": version}
            )
        if scope == "dev":
            violations["dev_only_distributions"].append(
                {"name": name, "normalized_name": normalized, "version": version}
            )

        license_source = None
        if metadata["license_expression"]:
            license_source = "License-Expression"
        elif metadata["license_field"]:
            license_source = "License"
        elif metadata["license_classifiers"]:
            license_source = "Classifier"
        distributions.append(
            {
                "name": name,
                "normalized_name": normalized,
                "version": version,
                "dist_info": dist_info.name,
                "metadata_status": metadata["metadata_status"],
                "metadata_errors": metadata["metadata_errors"],
                "license": {
                    "expression": metadata["license_expression"],
                    "field": metadata["license_field"],
                    "classifiers": metadata["license_classifiers"],
                    "source": license_source,
                    "license_files": metadata["license_files"],
                },
                "scope": scope,
                "scope_basis": scope_basis,
                "record_status": record_status,
                "declared_files": len(files),
                "present_files": present_count,
                "bytes": present_bytes,
                "record_declared_bytes": declared_bytes,
                "files": files,
                "missing_files": missing,
                "outside_site_packages": dist_outside_site_packages,
                "duplicate_record_entries": duplicate_entries,
                "requires_dist": metadata["requires_dist"],
            }
        )

    grouped: dict[str, list[dict]] = {}
    for distribution in distributions:
        grouped.setdefault(distribution["normalized_name"], []).append(distribution)
    for normalized, group in sorted(grouped.items()):
        if len(group) > 1:
            violations["duplicate_distribution_names"].append(
                {
                    "name": normalized,
                    "versions": [item["version"] for item in group],
                    "dist_infos": [item["dist_info"] for item in group],
                }
            )

    categories: dict[str, dict] = {}
    non_dist_files: list[dict] = []
    for relative in payload_files:
        absolute = payload / relative
        key = os.path.normcase(os.path.realpath(absolute))
        if key in claimed:
            continue
        category = _classify_non_dist(relative)
        size = file_bytes[relative]
        bucket = categories.setdefault(category, {"files": [], "bytes": 0})
        bucket["files"].append(relative)
        bucket["bytes"] += size
        non_dist_files.append({"path": relative, "bytes": size, "category": category})
    for bucket in categories.values():
        bucket["files"].sort()
        bucket["file_count"] = len(bucket["files"])
    non_dist_bytes = sum(bucket["bytes"] for bucket in categories.values())
    unrecorded_top_level: dict[str, dict] = {}
    for item in non_dist_files:
        if item["category"] != "site_packages_unrecorded":
            continue
        parts = item["path"].split("/")
        if any(part.endswith(".dist-info") for part in parts):
            continue
        violations["unrecorded_site_packages_files"].append(item)
        top = parts[2] if len(parts) > 3 else "(loose files)"
        bucket = unrecorded_top_level.setdefault(top, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += item["bytes"]

    interpreter = {
        "python_major_minor": None,
        "interpreter_executable": None,
        "libraries": [],
        "extension_modules": {
            "count": categories.get("extension_module", {}).get("file_count", 0),
            "bytes": categories.get("extension_module", {}).get("bytes", 0),
        },
        "authenticode": "not-checked",
    }
    for name in INTERPRETER_FILES:
        candidate = payload / name
        if not candidate.is_file():
            continue
        record = {
            "path": name,
            "bytes": candidate.stat().st_size,
            "sha256": sha256_file(candidate),
            "pe": read_pe_versions(candidate),
        }
        if name == "python.exe":
            interpreter["interpreter_executable"] = record
        else:
            interpreter["libraries"].append(record)
        match = RE_INTERPRETER_DLL.match(name)
        if match:
            interpreter["python_major_minor"] = match.group(1) + "." + match.group(2)
    if interpreter["interpreter_executable"] is None:
        violations["payload_errors"].append("payload has no python.exe")

    target_environment = {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "os_name": "nt",
        "python_version": interpreter["python_major_minor"] or "3.12",
    }
    declared: list[dict] = []
    declared_missing: list[dict] = []
    version_mismatches: list[dict] = []
    unchecked: list[dict] = []
    for text in requirement_texts:
        name, specifier, extras, marker = split_requirement(text)
        marker_result = evaluate_marker(marker, target_environment)
        normalized = normalize_name(name)
        matched = [item for item in distributions if item["normalized_name"] == normalized]
        bundled_version = matched[0]["version"] if matched else None
        entry = {
            "requirement": text,
            "name": name,
            "normalized_name": normalized,
            "specifier": specifier,
            "extras": extras,
            "marker": marker,
            "marker_status": marker_result["status"],
            "marker_reason": marker_result["reason"],
            "bundled_version": bundled_version,
            "specifier_status": "not-checked",
            "specifier_reason": "",
        }
        if bundled_version is not None:
            if specifier:
                check = check_specifier(bundled_version, specifier)
                entry["specifier_status"] = check["status"]
                entry["specifier_reason"] = check["reason"]
                if check["status"] == "not-satisfied":
                    version_mismatches.append(
                        {
                            "name": name,
                            "specifier": specifier,
                            "bundled_version": bundled_version,
                            "reason": check["reason"],
                        }
                    )
                elif check["status"] == "not-checked":
                    unchecked.append({"name": name, "reason": check["reason"]})
            else:
                entry["specifier_status"] = "satisfied"
        if marker_result["status"] == "applicable" and bundled_version is None:
            declared_missing.append(
                {"name": name, "requirement": text, "reason": "not present in the payload"}
            )
        declared.append(entry)

    first_party_versions = [
        item["version"]
        for item in distributions
        if normalize_name(project_name) == item["normalized_name"]
    ]
    first_party_mismatch = None
    if project_version and first_party_versions and project_version not in first_party_versions:
        first_party_mismatch = {
            "name": project_name,
            "declared_version": project_version,
            "packaged_versions": first_party_versions,
        }

    if sbom_path is None:
        sbom_comparison = {
            "status": "not-run",
            "reason": "no artifact SBOM path was supplied",
            "source": None,
        }
    else:
        sbom = _load_sbom(sbom_path)
        python_components, other_components = _sbom_python_components(sbom)
        sbom_versions: dict[str, list[str]] = {}
        for component in python_components:
            sbom_versions.setdefault(normalize_name(component["name"]), []).append(component["version"])
        bundle_versions: dict[str, list[str]] = {}
        for distribution in distributions:
            bundle_versions.setdefault(distribution["normalized_name"], []).append(
                distribution["version"]
            )
        project_key = normalize_name(project_name) if project_name else ""
        first_party_versions_sbom = sbom_versions.pop(project_key, []) if project_key else []
        in_bundle_not_sbom: list[dict] = []
        in_sbom_not_bundle: list[dict] = []
        matched: list[str] = []
        for normalized, versions in sorted(bundle_versions.items()):
            if normalized == project_key:
                continue
            if normalized not in sbom_versions:
                for version in versions:
                    in_bundle_not_sbom.append(
                        {"name": normalized, "version": version, "kind": "absent-from-sbom"}
                    )
                continue
            missing_versions = [version for version in versions if version not in sbom_versions[normalized]]
            if missing_versions:
                for version in missing_versions:
                    in_bundle_not_sbom.append(
                        {
                            "name": normalized,
                            "version": version,
                            "kind": "version-mismatch",
                            "sbom_versions": sorted(sbom_versions[normalized]),
                        }
                    )
            else:
                matched.append(normalized)
        for normalized, versions in sorted(sbom_versions.items()):
            if normalized not in bundle_versions:
                for version in versions:
                    in_sbom_not_bundle.append(
                        {"name": normalized, "version": version, "kind": "absent-from-payload"}
                    )
        sbom_comparison = {
            "status": "compared",
            "reason": "",
            "source": display_path(sbom_path, repo_root),
            "sbom_format": sbom.get("bomFormat"),
            "sbom_spec_version": sbom.get("specVersion"),
            "sbom_pypi_component_count": len(python_components),
            "sbom_other_component_count": len(other_components),
            "sbom_other_component_names": sorted(
                str(component["name"]) for component in other_components
            ),
            "matched": matched,
            "in_bundle_not_in_sbom": in_bundle_not_sbom,
            "in_sbom_not_in_bundle": in_sbom_not_bundle,
            "first_party": {
                "name": project_name,
                "bundle_versions": sorted(bundle_versions.get(project_key, [])),
                "sbom_versions": sorted(first_party_versions_sbom),
            },
        }

    dev_count = len(violations["dev_only_distributions"])
    causes: list[str] = []
    if violations["forbidden_distributions"]:
        labels = ", ".join(_dist_label(item) for item in violations["forbidden_distributions"])
        causes.append("forbidden development-only distribution present: " + labels)
    if dev_count:
        causes.append(f"{dev_count} development-only distribution(s) present")
    if violations["out_of_payload_record_entries"]:
        causes.append(
            f"{len(violations['out_of_payload_record_entries'])} RECORD entry/entries leave the payload"
        )
    if violations["duplicate_distribution_names"]:
        causes.append(f"{len(violations['duplicate_distribution_names'])} duplicate distribution name(s)")
    if violations["metadata_errors"]:
        causes.append(f"{len(violations['metadata_errors'])} malformed METADATA file(s)")
    if violations["record_errors"]:
        causes.append(f"{len(violations['record_errors'])} RECORD problem(s)")
    if violations["payload_errors"]:
        causes.extend(violations["payload_errors"])
    if violations["unrecorded_site_packages_files"]:
        names = ", ".join(item["path"] for item in violations["unrecorded_site_packages_files"])
        causes.append(f"unrecorded site-packages file(s): {names}")
    if first_party_mismatch is not None:
        causes.append(
            "first-party version mismatch: pyproject says "
            + first_party_mismatch["declared_version"]
            + ", payload says "
            + ", ".join(first_party_mismatch["packaged_versions"])
        )
    if missing_other:
        causes.append(f"{missing_other} RECORD file(s) missing from the payload")
    if size_mismatches:
        causes.append(f"{len(size_mismatches)} RECORD size mismatch(es)")
    if hash_mismatches:
        causes.append(f"{len(hash_mismatches)} RECORD hash mismatch(es)")
    exit_code = EXIT_FINDINGS if causes else EXIT_OK

    scope_counts = {"runtime": 0, "dev": 0, "transitive": 0}
    for distribution in distributions:
        scope_counts[distribution["scope"]] += 1

    tree_digest = hashlib.sha256()
    for relative in payload_files:
        file_hash = sha256_file(payload / relative)
        tree_digest.update(relative.encode("utf-8") + b"\0" + file_hash.encode("ascii") + b"\n")
    provenance_complete = (
        not violations["unrecorded_site_packages_files"]
        and first_party_mismatch is None
        and missing_other == 0
        and not size_mismatches
        and not hash_mismatches
        and not violations["record_errors"]
        and not violations["out_of_payload_record_entries"]
    )
    report = {
        "schema": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "payload": {
            "path": payload_display,
            "file_count": len(payload_files),
            "total_bytes": total_bytes,
            "exe_count": sum(1 for item in payload_files if item.lower().endswith(".exe")),
            "tree_sha256": tree_digest.hexdigest(),
        },
        "provenance": {
            "complete": provenance_complete,
            "distribution_files": len(claimed),
            "non_distribution_categories": {
                key: categories[key]["file_count"] for key in sorted(categories)
            },
            "unrecorded_site_packages_files": violations["unrecorded_site_packages_files"],
            "first_party_version_mismatch": first_party_mismatch,
        },
        "interpreter": interpreter,
        "distributions": distributions,
        "distribution_summary": {
            "count": len(distributions),
            "runtime": scope_counts["runtime"],
            "dev": scope_counts["dev"],
            "transitive": scope_counts["transitive"],
            "present_files": sum(item["present_files"] for item in distributions),
            "bytes": sum(item["bytes"] for item in distributions),
            "distinct_claimed_files": len(claimed),
            "distinct_claimed_bytes": claimed_bytes,
        },
        "non_distribution_files": {
            "count": len(non_dist_files),
            "bytes": non_dist_bytes,
            "categories": {key: categories[key] for key in sorted(categories)},
            "site_packages_unrecorded_top_level": [
                {"path": key, "files": value["files"], "bytes": value["bytes"]}
                for key, value in sorted(unrecorded_top_level.items())
            ],
        },
        "accounting": {
            "payload_files": len(payload_files),
            "distinct_claimed_files": len(claimed),
            "non_distribution_files": len(non_dist_files),
            "overlapping_record_entries": overlap,
            "balanced": len(claimed) + len(non_dist_files) == len(payload_files),
        },
        "integrity": {
            "missing_pyc": missing_pyc,
            "missing_console_scripts": missing_console_scripts,
            "missing_optional_extensions": missing_optional_extensions,
            "missing_other": missing_other,
            "outside_site_packages": sorted(
                outside_site_packages, key=lambda item: (item["distribution"], item["entry"])
            ),
            "record_size_mismatches": size_mismatches,
            "record_hash_mismatches": hash_mismatches,
            "hashes_verified": verify_hashes,
        },
        "violations": violations,
        "declared_runtime_dependencies": {
            "source": display_path(pyproject_path, repo_root),
            "project_name": project_name,
            "project_version": project_version,
            "declared": declared,
            "missing_from_bundle": declared_missing,
            "version_mismatches": version_mismatches,
            "not_checked": unchecked,
            "first_party_version_mismatch": first_party_mismatch,
        },
        "sbom_comparison": sbom_comparison,
        "summary": {
            "dev_only_present": sorted(
                {_dist_label(item) for item in violations["dev_only_distributions"]}
            ),
            "exit_code": exit_code,
            "causes": causes,
        },
    }
    return report


def _license_text(distribution: dict) -> str:
    license_info = distribution["license"]
    text = ""
    if license_info["expression"]:
        text = license_info["expression"]
    elif license_info["field"]:
        text = license_info["field"]
    elif license_info["classifiers"]:
        text = "; ".join(license_info["classifiers"])
    text = " ".join(text.split())
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _dash(value, fallback="n/a") -> str:
    return str(value) if value else fallback


def render_markdown(report: dict) -> str:
    payload = report["payload"]
    summary = report["summary"]
    dist_summary = report["distribution_summary"]
    lines: list[str] = []
    lines.append("# Packaged runtime inventory")
    lines.append("")
    lines.append("- Payload: `" + payload["path"] + "`")
    lines.append(f"- Files: {payload['file_count']} ({payload['total_bytes']} bytes)")
    lines.append(f"- Executables: {payload['exe_count']}")
    lines.append(f"- Shipped distributions: {dist_summary['count']}")
    lines.append(f"- Exit code: {summary['exit_code']}")
    lines.append("")
    lines.append("## Verdict")
    if summary["causes"]:
        for cause in summary["causes"]:
            lines.append("- FINDING: " + cause)
    else:
        lines.append("- No policy violation found in the inspected files.")
    lines.append("")
    lines.append("## Interpreter and runtime DLLs")
    executable = report["interpreter"]["interpreter_executable"]
    if executable:
        lines.append("| file | bytes | file version | product version | company |")
        lines.append("| --- | ---: | --- | --- | --- |")
        rows = [executable] + report["interpreter"]["libraries"]
        for item in rows:
            pe = item["pe"]
            lines.append(
                "| " + item["path"]
                + " | " + str(item["bytes"])
                + " | " + _dash(pe["file_version"])
                + " | " + _dash(pe["product_version"])
                + " | " + _dash(pe["company_name"]) + " |"
            )
    else:
        lines.append("- No interpreter executable was found in the payload.")
    interpreter = report["interpreter"]
    lines.append("")
    lines.append("- Python major.minor: " + _dash(interpreter["python_major_minor"], "unknown"))
    extensions = interpreter["extension_modules"]
    lines.append(f"- Extension modules: {extensions['count']} ({extensions['bytes']} bytes)")
    lines.append("- Authenticode: not-checked (this inventory does not verify signatures)")
    lines.append("")
    lines.append("## Shipped distributions")
    lines.append("")
    lines.append(
        "| distribution | version | scope | files on disk | bytes | missing declared files | licence |"
    )
    lines.append("| --- | --- | --- | ---: | ---: | ---: | --- |")
    for distribution in report["distributions"]:
        lines.append(
            "| " + distribution["name"]
            + " | " + distribution["version"]
            + " | " + distribution["scope"]
            + " | " + str(distribution["present_files"])
            + " | " + str(distribution["bytes"])
            + " | " + str(len(distribution["missing_files"]))
            + " | " + _license_text(distribution) + " |"
        )
    lines.append("")
    lines.append("## Non-distribution files")
    lines.append("")
    lines.append("| category | files | bytes |")
    lines.append("| --- | ---: | ---: |")
    non_dist = report["non_distribution_files"]
    for category, bucket in non_dist["categories"].items():
        lines.append(f"| {category} | {bucket['file_count']} | {bucket['bytes']} |")
    unrecorded = non_dist["site_packages_unrecorded_top_level"]
    if unrecorded:
        lines.append("")
        lines.append("### site-packages files not claimed by any RECORD")
        lines.append("")
        lines.append("| top-level entry | files | bytes |")
        lines.append("| --- | ---: | ---: |")
        for item in unrecorded:
            lines.append(f"| {item['path']} | {item['files']} | {item['bytes']} |")
    lines.append("")
    lines.append("## Policy findings")
    lines.append("")
    violations = report["violations"]
    if violations["dev_only_distributions"]:
        lines.append("### Development-only distributions present")
        for item in violations["dev_only_distributions"]:
            lines.append("- " + _dist_label(item))
        lines.append("")
    if violations["out_of_payload_record_entries"]:
        lines.append("### RECORD entries outside the payload")
        for item in violations["out_of_payload_record_entries"]:
            lines.append(
                "- " + item["distribution"] + ": `" + item["entry"] + "` (" + item["reason"] + ")"
            )
        lines.append("")
    if violations["duplicate_distribution_names"]:
        lines.append("### Duplicate distribution names")
        for item in violations["duplicate_distribution_names"]:
            versions = ", ".join(item["versions"])
            dist_infos = ", ".join(item["dist_infos"])
            lines.append("- " + item["name"] + ": " + versions + " (" + dist_infos + ")")
        lines.append("")
    if violations["metadata_errors"]:
        lines.append("### Malformed METADATA")
        for item in violations["metadata_errors"]:
            lines.append("- " + str(item["distribution"]) + ": " + item["error"])
        lines.append("")
    if violations["record_errors"]:
        lines.append("### RECORD problems")
        for item in violations["record_errors"]:
            lines.append("- " + str(item["distribution"]) + ": " + item["error"])
        lines.append("")
    lines.append("## RECORD integrity (reported, not fatal)")
    integrity = report["integrity"]
    lines.append(f"- Missing declared .pyc files: {integrity['missing_pyc']}")
    lines.append(f"- Missing declared console scripts: {integrity['missing_console_scripts']}")
    lines.append(
        "- Missing optional compiled extensions (pure-Python fallback present): "
        + str(integrity["missing_optional_extensions"])
    )
    lines.append(f"- Missing declared files (other): {integrity['missing_other']}")
    lines.append(
        "- RECORD entries outside site-packages but inside the payload: "
        + str(len(integrity["outside_site_packages"]))
    )
    lines.append(f"- Declared size mismatches: {len(integrity['record_size_mismatches'])}")
    lines.append(
        f"- SHA-256 mismatches: {len(integrity['record_hash_mismatches'])} "
        + f"(hashes verified: {integrity['hashes_verified']})"
    )
    lines.append("")
    declared = report["declared_runtime_dependencies"]
    lines.append("## Declared runtime dependencies")
    lines.append("")
    lines.append(
        "- Source: `" + declared["source"] + "` (project "
        + declared["project_name"] + " " + declared["project_version"] + ")"
    )
    lines.append(f"- Declared requirements: {len(declared['declared'])}")
    lines.append(f"- Missing from bundle: {len(declared['missing_from_bundle'])}")
    for item in declared["missing_from_bundle"]:
        lines.append("  - " + item["name"] + ": " + item["requirement"])
    lines.append(f"- Specifier mismatches: {len(declared['version_mismatches'])}")
    for item in declared["version_mismatches"]:
        lines.append(
            "  - " + item["name"] + " " + item["bundled_version"]
            + " vs " + item["specifier"]
        )
    if declared["not_checked"]:
        lines.append(f"- Checks not evaluated: {len(declared['not_checked'])}")
    mismatch = declared["first_party_version_mismatch"]
    if mismatch:
        packaged = ", ".join(mismatch["packaged_versions"])
        lines.append(
            "- First-party version mismatch: pyproject says "
            + mismatch["declared_version"] + ", payload dist-info says " + packaged
        )
    lines.append("")
    sbom = report["sbom_comparison"]
    lines.append("## Artifact SBOM comparison")
    lines.append("")
    if sbom["status"] != "compared":
        lines.append("- NOT RUN: " + str(sbom["reason"]))
    else:
        lines.append(
            "- Source: `" + sbom["source"] + "` ("
            + str(sbom["sbom_format"]) + " " + str(sbom["sbom_spec_version"]) + ")"
        )
        lines.append(f"- Python components in SBOM: {sbom['sbom_pypi_component_count']}")
        other_names = ", ".join(sbom["sbom_other_component_names"])
        lines.append(
            f"- Non-Python components ignored: {sbom['sbom_other_component_count']} ({other_names})"
        )
        lines.append(f"- Matched distributions: {len(sbom['matched'])}")
        lines.append(
            f"- In bundle, absent or different in SBOM: {len(sbom['in_bundle_not_in_sbom'])}"
        )
        for item in sbom["in_bundle_not_in_sbom"]:
            extra = ""
            if item.get("sbom_versions"):
                extra = " (SBOM has " + ", ".join(item["sbom_versions"]) + ")"
            lines.append(
                "  - " + item["name"] + " " + item["version"] + ": " + item["kind"] + extra
            )
        lines.append(
            f"- In SBOM, absent from bundle: {len(sbom['in_sbom_not_in_bundle'])}"
        )
        for item in sbom["in_sbom_not_in_bundle"]:
            lines.append(
                "  - " + item["name"] + " " + item["version"] + ": " + item["kind"]
            )
        first_party = sbom["first_party"]
        bundle_versions = ", ".join(first_party["bundle_versions"]) or "absent"
        sbom_versions = ", ".join(first_party["sbom_versions"]) or "absent"
        lines.append(
            "- First-party " + first_party["name"] + ": payload "
            + bundle_versions + " vs SBOM " + sbom_versions
        )
    lines.append("")
    lines.append("## Not proven by this inventory")
    lines.append("")
    lines.append("- RECORD file lists are metadata claims; they do not prove import reachability.")
    lines.append("- The tool does not verify Authenticode signatures.")
    lines.append("- Licence values are copied from package metadata, not audited.")
    lines.append(
        "- This describes only the payload at the path above for project version "
        + declared["project_version"]
        + "."
    )
    lines.append("")
    return "\n".join(lines)


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base / candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory the packaged CreatorOS backend runtime.",
    )
    parser.add_argument("--payload", default=DEFAULT_PAYLOAD)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--pyproject", default=DEFAULT_PYPROJECT)
    parser.add_argument("--sbom", default=DEFAULT_SBOM)
    parser.add_argument("--no-sbom", action="store_true")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--markdown-out", default=None)
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    parser.add_argument("--no-verify-hashes", action="store_true")
    arguments = parser.parse_args(argv)

    repo_root = (
        Path(arguments.repo_root).resolve()
        if arguments.repo_root
        else Path(__file__).resolve().parent.parent
    )
    payload = _resolve(repo_root, arguments.payload)
    pyproject = _resolve(repo_root, arguments.pyproject)
    sbom = None if arguments.no_sbom else _resolve(repo_root, arguments.sbom)

    try:
        report = build_inventory(
            payload=payload,
            repo_root=repo_root,
            pyproject_path=pyproject,
            sbom_path=sbom,
            verify_hashes=not arguments.no_verify_hashes,
        )
    except InventoryError as error:
        print(f"runtime-inventory: {error}", file=sys.stderr)
        return EXIT_ERROR

    markdown = render_markdown(report)
    if arguments.json_out:
        output = _resolve(repo_root, arguments.json_out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if arguments.markdown_out:
        output = _resolve(repo_root, arguments.markdown_out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    if arguments.json:
        print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False))
    else:
        print(markdown)
    return int(report["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
