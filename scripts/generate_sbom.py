#!/usr/bin/env python3
"""Generate a CycloneDX 1.5 SBOM for CreatorOS from real repository inputs.

What the document proves
------------------------
Every component is derived from an artifact that exists in this checkout, or from the optional
prerequisites manifest named with --prerequisites: the desktop version from app/desktop/package.json,
the backend version from app/backend/pyproject.toml, Apache-2.0 from LICENSE, Node versions and pnpm
integrity digests from app/desktop/pnpm-lock.yaml, Python distributions from app/backend/.venv (the
tested environment) or pyproject specifiers when it is absent, the FFmpeg binaries from
tools/ffmpeg/bin with SHA-256 computed from their real
bytes and versions read from their real -version banner, and Ollama only when a prerequisites manifest
declares it. A component that cannot be collected is never invented: it is left out and the reason is
recorded in a creatoros:collection-error property.

What the document does not prove
--------------------------------
- It is not a legal review and does not establish licence compatibility.
- A hash proves which bytes were inspected on this machine at generation time. It says nothing about
  who built them, whether they were signed, or whether they may be redistributed.
- It does not cover model weights, user media, databases, provider credentials, the CPython
  interpreter payload, the Electron/Node runtimes, or any file not listed above.
- It does not resolve transitive Node dependencies, and it does not prove that the recorded Python
  environment is the one an installer ships.

Determinism
------------
Identical repository inputs produce byte-identical output except metadata.timestamp and any
--timestamp or --serial override. Components are sorted by (type, name, version) and JSON is written
with a fixed indent and LF newlines. Without --serial the serial number is urn:uuid:<UUIDv5> over the
canonical JSON of the sorted component list, so it changes whenever a component changes and never
otherwise.

Failure policy
--------------
Structural inputs are required and fail with exit code 2: a missing or version-less desktop
package.json, an unparseable or version-less app/backend/pyproject.toml, a missing or inconsistent
pnpm lockfile, or a --prerequisites file that cannot be read or parsed. External payloads are optional:
a missing FFmpeg binary, an unreadable -version banner, an absent virtual environment, or incomplete
Ollama metadata do not fail the run, but each one is recorded explicitly in a
creatoros:collection-error property so the document states what is missing. A missing or unrecognised
LICENSE records creatoros:license-status=unknown instead of guessing.

Usage
-----
    python scripts/generate_sbom.py --output releases/1.0.0/sbom.cdx.json --version 1.0.0 --check
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

BOM_FORMAT = 'CycloneDX'
SPEC_VERSION = '1.5'
SCHEMA_URL = 'http://cyclonedx.org/schema/bom-1.5.schema.json'
GENERATOR_NAME = 'creatoros-generate-sbom'
GENERATOR_VERSION = '1.0.0'
SERIAL_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, 'https://creatoros.dev/sbom/cyclonedx')
PACKAGE_JSON_PATH = Path('app') / 'desktop' / 'package.json'
LOCKFILE_PATH = Path('app') / 'desktop' / 'pnpm-lock.yaml'
PYPROJECT_PATH = Path('app') / 'backend' / 'pyproject.toml'
VENV_PATH = Path('app') / 'backend' / '.venv'
FFMPEG_DIR = Path('tools') / 'ffmpeg' / 'bin'
MANIFEST_PATH = Path('docs') / 'release' / 'DEPENDENCY_MANIFEST.md'
PREREQUISITES_SCHEMA = 'creatoros.prerequisites.v1'
PROFILES = frozenset({'source', 'prerequisites', 'artifact', 'all'})

SELF_DISTRIBUTIONS = frozenset({'creatoros', 'creatoros-backend'})

KNOWN_SPDX_LICENCES = frozenset({
    '0BSD',
    'AGPL-3.0-only',
    'AGPL-3.0-or-later',
    'Apache-2.0',
    'BSD-2-Clause',
    'BSD-3-Clause',
    'BSL-1.0',
    'CC0-1.0',
    'GPL-2.0-only',
    'GPL-2.0-or-later',
    'GPL-3.0-only',
    'GPL-3.0-or-later',
    'ISC',
    'LGPL-2.1-only',
    'LGPL-2.1-or-later',
    'LGPL-3.0-only',
    'LGPL-3.0-or-later',
    'MIT',
    'MPL-2.0',
    'PSF-2.0',
    'Python-2.0',
    'Unlicense',
    'Zlib',
})

_HASH_LENGTHS = {'SHA-256': 64, 'SHA-384': 96, 'SHA-512': 128, 'SHA-1': 40, 'MD5': 32}
_REQUIREMENT = re.compile(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*)$')
_SPDX_TOKEN = re.compile(r'[A-Za-z0-9.+-]+')
_VERSION_FROM_BANNER = re.compile(r'(?:^|\s)version\s+(\S+)')
_INTEGRITY_IN_FLOW = re.compile(r'integrity\s*:\s*([^,}\s]+)')
_MARKER_EXTRA = re.compile(r"extra\s*==\s*['\"]([^'\"]+)['\"]")
_HEX = re.compile(r'^[0-9a-f]+$')


class SbomError(RuntimeError):
    """A required repository input is missing, unreadable or contradictory."""


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        raise SbomError(f'cannot read {label} at {path}: {exc}') from exc
    except UnicodeDecodeError as exc:
        raise SbomError(f'{label} at {path} is not UTF-8: {exc}') from exc


def _read_json(path: Path, label: str) -> dict[str, Any]:
    text = _read_text(path, label)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SbomError(f'cannot parse {label} at {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise SbomError(f'{label} at {path} must contain a JSON object')
    return data


def read_desktop_version(repo_root: Path) -> str:
    """Return the version declared by app/desktop/package.json."""
    package = _read_json(repo_root / PACKAGE_JSON_PATH, 'desktop package.json')
    version = package.get('version')
    if not isinstance(version, str) or not version.strip():
        raise SbomError(f'{repo_root / PACKAGE_JSON_PATH} has no version string')
    return version.strip()


def read_backend_pyproject(repo_root: Path) -> dict[str, Any]:
    """Parse app/backend/pyproject.toml, raising SbomError when it cannot be read."""
    path = repo_root / PYPROJECT_PATH
    text = _read_text(path, 'backend pyproject.toml')
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SbomError(f'cannot parse backend pyproject.toml at {path}: {exc}') from exc
    return data


def read_backend_version(pyproject: dict[str, Any], repo_root: Path) -> str:
    """Return [project].version, raising SbomError when it is missing."""
    project = pyproject.get('project')
    version = project.get('version') if isinstance(project, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise SbomError(f'{repo_root / PYPROJECT_PATH} has no [project] version')
    return version.strip()


def read_licence(repo_root: Path) -> dict[str, Any] | None:
    """Return the Apache-2.0 licence entry when LICENSE really is the Apache-2.0 text."""
    path = repo_root / 'LICENSE'
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError):
        return None
    if 'Apache License' in text and 'Version 2.0' in text:
        return {'license': {'id': 'Apache-2.0'}}
    return None


def _unquote_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        return inner.replace('\\"', '"')
    return value


def _split_key_value(line: str) -> tuple[str, str]:
    key, separator, value = line.partition(':')
    if not separator:
        raise SbomError(f'pnpm-lock.yaml line is not a mapping entry: {line!r}')
    return _unquote_scalar(key.strip()), value.strip()


def parse_pnpm_lockfile(text: str) -> dict[str, Any]:
    """Parse the pnpm v9 lockfile subset this generator reads.

    Returns importers (importer -> group -> package -> fields), packages (package key -> integrity)
    and the set of snapshot keys. It is deliberately narrow: pnpm lockfiles are YAML, but only these
    three top-level sections and their scalar fields are needed, so no YAML dependency is added.
    """
    importers: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
    packages: dict[str, dict[str, str]] = {}
    snapshots: set[str] = set()
    section = ''
    importer = ''
    group = ''
    package = ''
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        if indent == 0:
            key, _value = _split_key_value(stripped)
            section = key
            importer = group = package = ''
            continue
        if section == 'importers':
            if indent == 2:
                importer, _value = _split_key_value(stripped)
                importers.setdefault(importer, {})
            elif indent == 4:
                group, _value = _split_key_value(stripped)
                importers.setdefault(importer, {}).setdefault(group, {})
            elif indent == 6:
                package, _value = _split_key_value(stripped)
                entries = importers.setdefault(importer, {}).setdefault(group, {})
                entries.setdefault(package, {})
            elif indent == 8:
                field, value = _split_key_value(stripped)
                importers[importer][group][package][field] = value
        elif section == 'packages':
            if indent == 2:
                package, _value = _split_key_value(stripped)
                packages.setdefault(package, {})
            elif indent == 4:
                field, value = _split_key_value(stripped)
                if field == 'resolution' and value.startswith('{'):
                    match = _INTEGRITY_IN_FLOW.search(value)
                    if match:
                        packages.setdefault(package, {})['integrity'] = _unquote_scalar(match.group(1))
        elif section == 'snapshots' and indent == 2:
            snapshot, _value = _split_key_value(stripped)
            snapshots.add(snapshot)
    return {'importers': importers, 'packages': packages, 'snapshots': snapshots}


def _lockfile_integrity(packages: dict[str, dict[str, str]], name: str, version: str) -> str | None:
    key = f'{name}@{version}'
    entry = packages.get(key)
    if entry and entry.get('integrity'):
        return entry['integrity']
    for candidate, fields in packages.items():
        if candidate.startswith(f'{key}(') and fields.get('integrity'):
            return fields['integrity']
    return None


def _lockfile_has_resolution(
    packages: dict[str, dict[str, str]], snapshots: set[str], name: str, version: str
) -> bool:
    key = f'{name}@{version}'
    if key in packages or key in snapshots:
        return True
    prefix = f'{key}('
    return any(candidate.startswith(prefix) for candidate in packages) or any(
        candidate.startswith(prefix) for candidate in snapshots
    )


def _npm_purl(name: str, version: str) -> str:
    if name.startswith('@') and '/' in name:
        scope, _slash, package = name.partition('/')
        path = quote(scope, safe='') + '/' + quote(package, safe='')
    else:
        path = quote(name, safe='')
    return f'pkg:npm/{path}@{quote(version, safe="")}'


def _pypi_purl(name: str, version: str) -> str:
    return f'pkg:pypi/{quote(name, safe="")}@{quote(version, safe="")}'


def _canonical_name(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).strip().lower()


def split_requirement(requirement: str) -> tuple[str, str]:
    """Split a PEP 508 requirement into (name, version specifier or direct reference)."""
    text = requirement.split(';', 1)[0].strip()
    match = _REQUIREMENT.match(text)
    if not match:
        raise SbomError(f'cannot parse dependency requirement: {requirement!r}')
    name = match.group(1).strip()
    specifier = match.group(3).strip()
    if specifier.startswith('@'):
        specifier = ''
    return name, specifier


def _requirement_names(requirements: Any, label: str) -> set[str]:
    if not isinstance(requirements, list):
        raise SbomError(f'{label} must be an array of requirement strings')
    names: set[str] = set()
    for requirement in requirements:
        name, _specifier = split_requirement(str(requirement))
        names.add(_canonical_name(name))
    return names


def _requested_extras(requirements: Any) -> dict[str, set[str]]:
    extras: dict[str, set[str]] = {}
    if not isinstance(requirements, list):
        return extras
    for requirement in requirements:
        text = str(requirement).split(';', 1)[0].strip()
        match = _REQUIREMENT.match(text)
        if not match:
            continue
        canonical = _canonical_name(match.group(1))
        requested = {item.strip() for item in (match.group(2) or '').split(',') if item.strip()}
        if requested:
            extras.setdefault(canonical, set()).update(requested)
    return extras


def _sorted_properties(properties: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(properties, key=lambda item: (item['name'], item['value']))


def _expression_entry(text: str) -> dict[str, Any]:
    cleaned = ' '.join(text.split())
    if cleaned in KNOWN_SPDX_LICENCES:
        return {'license': {'id': cleaned}}
    return {'expression': cleaned}


def spdx_licence_entry(text: str) -> dict[str, Any] | None:
    """Map a short licence string to a CycloneDX entry without inventing an SPDX identifier."""
    cleaned = ' '.join(text.split())
    if not cleaned:
        return None
    tokens = _SPDX_TOKEN.findall(cleaned)
    if not tokens:
        return None
    operators = {'AND', 'OR', 'WITH'}
    if all(token in KNOWN_SPDX_LICENCES or token in operators for token in tokens):
        return _expression_entry(cleaned)
    return {'license': {'name': cleaned}}


def _metadata_licence(metadata: importlib.metadata.Metadata) -> dict[str, Any] | None:
    expression = metadata.get('License-Expression')
    if expression:
        cleaned = ' '.join(str(expression).split())
        if cleaned:
            return _expression_entry(cleaned)
    raw = metadata.get('License')
    if raw:
        entry = spdx_licence_entry(str(raw))
        if entry is not None:
            name = entry.get('license', {}).get('name', '')
            if not name or len(str(name)) <= 200:
                return entry
    for classifier in metadata.get_all('Classifier') or []:
        if classifier.startswith('License ::'):
            name = classifier.split('::')[-1].strip()
            if name:
                return {'license': {'name': name}}
    return None


def _node_licence(repo_root: Path, name: str) -> tuple[dict[str, Any] | None, str]:
    node_modules = (repo_root / 'app' / 'desktop' / 'node_modules').resolve()
    package_json = (node_modules / Path(*name.split('/')) / 'package.json').resolve()
    if not package_json.is_relative_to(node_modules) or not package_json.is_file():
        return None, ''
    try:
        package = json.loads(package_json.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, ''
    if not isinstance(package, dict):
        return None, ''
    raw = package.get('license')
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        raw = raw.get('type')
    if not isinstance(raw, str) or not raw.strip():
        return None, ''
    entry = spdx_licence_entry(raw)
    source = f'app/desktop/node_modules/{name}/package.json'
    return entry, source


def collect_node_components(repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return direct Node dependencies resolved from app/desktop/pnpm-lock.yaml."""
    lock_path = repo_root / LOCKFILE_PATH
    if not lock_path.is_file():
        raise SbomError(f'missing required lockfile: {lock_path}')
    data = parse_pnpm_lockfile(_read_text(lock_path, 'desktop pnpm-lock.yaml'))
    importer = data['importers'].get('.')
    if not importer:
        raise SbomError(f'{lock_path} has no importers["."] entry')
    packages: dict[str, dict[str, str]] = data['packages']
    snapshots: set[str] = data['snapshots']
    components: list[dict[str, Any]] = []
    errors: list[str] = []
    for group, scope in (('dependencies', 'required'), ('devDependencies', 'excluded')):
        entries = importer.get(group, {})
        for name in sorted(entries):
            fields = entries[name]
            resolved = fields.get('version', '').strip()
            if not resolved:
                raise SbomError(f'pnpm-lock.yaml records no resolved version for {name} ({group})')
            if resolved.startswith(('link:', 'file:')):
                continue
            version = resolved.split('(', 1)[0].strip()
            if not version:
                raise SbomError(f'pnpm-lock.yaml records an empty version for {name} ({group})')
            if not _lockfile_has_resolution(packages, snapshots, name, version):
                raise SbomError(
                    f'pnpm-lock.yaml resolves {name} to {version} but records no package entry for it'
                )
            purl = _npm_purl(name, version)
            properties: list[dict[str, str]] = [
                {'name': 'creatoros:source', 'value': LOCKFILE_PATH.as_posix()},
                {'name': 'creatoros:specifier', 'value': fields.get('specifier', '')},
                {'name': 'creatoros:version-source', 'value': 'pnpm-lockfile'},
            ]
            if resolved != version:
                properties.append({'name': 'creatoros:lockfile-resolution', 'value': resolved})
            integrity = _lockfile_integrity(packages, name, version)
            if integrity:
                properties.append({'name': 'creatoros:pnpm-integrity', 'value': integrity})
            else:
                message = f'pnpm-lock.yaml records no integrity for {name}@{version}'
                errors.append(message)
                properties.append({'name': 'creatoros:collection-error', 'value': message})
            if group == 'devDependencies':
                properties.append({
                    'name': 'creatoros:scope-reason',
                    'value': 'build-time only; dev dependency, not shipped in the packaged application',
                })
            licence, licence_source = _node_licence(repo_root, name)
            component: dict[str, Any] = {
                'type': 'library',
                'bom-ref': purl,
                'name': name,
                'version': version,
                'scope': scope,
                'purl': purl,
            }
            if licence is not None:
                component['licenses'] = [licence]
                properties.append({'name': 'creatoros:license-source', 'value': licence_source})
            else:
                properties.append({'name': 'creatoros:license-status', 'value': 'unknown'})
            component['properties'] = _sorted_properties(properties)
            components.append(component)
    return components, errors


def _venv_site_packages(repo_root: Path) -> Path | None:
    venv = repo_root / VENV_PATH
    if not venv.is_dir():
        return None
    windows = venv / 'Lib' / 'site-packages'
    if windows.is_dir():
        return windows
    for candidate in sorted(venv.glob('lib/python*/site-packages')):
        if candidate.is_dir():
            return candidate
    return None


def _installed_distributions(
    site_packages: Path,
) -> list[importlib.metadata.Distribution]:
    try:
        return list(importlib.metadata.distributions(path=[str(site_packages)]))
    except Exception as exc:
        raise SbomError(f'cannot read installed distributions from {site_packages}: {exc}') from exc


def _dependency_closure(roots: Iterable[str], graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(sorted(graph.get(name, ())))
    return seen


def _python_components_from_specifiers(
    pyproject: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    project = pyproject.get('project') or {}
    if not isinstance(project, dict):
        raise SbomError('pyproject.toml [project] must be a table')
    requirements = project.get('dependencies') or []
    if not isinstance(requirements, list):
        raise SbomError('pyproject.toml [project].dependencies must be an array')
    chosen: dict[str, tuple[str, str]] = {}
    for requirement in requirements:
        name, specifier = split_requirement(str(requirement))
        canonical = _canonical_name(name)
        chosen.setdefault(canonical, (specifier, str(requirement)))
    components: list[dict[str, Any]] = []
    errors: list[str] = []
    for canonical, (specifier, original) in sorted(chosen.items()):
        properties = [
            {'name': 'creatoros:source', 'value': PYPROJECT_PATH.as_posix()},
            {'name': 'creatoros:version-source', 'value': 'pyspecifier'},
            {'name': 'creatoros:requirement', 'value': original},
            {'name': 'creatoros:license-status', 'value': 'unknown'},
        ]
        component: dict[str, Any] = {
            'type': 'library',
            'bom-ref': f'creatoros-python-specifier:{canonical}',
            'name': canonical,
            'scope': 'required',
            'properties': _sorted_properties(properties),
        }
        if specifier:
            component['version'] = specifier
        components.append(component)
    if not components:
        errors.append('pyproject.toml declares no runtime dependencies to record as specifiers')
    return components, errors


def collect_python_components(
    repo_root: Path, pyproject: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return Python components from the tested venv, or from pyproject specifiers as a fallback."""
    site_packages = _venv_site_packages(repo_root)
    if site_packages is None:
        components, errors = _python_components_from_specifiers(pyproject)
        errors.insert(
            0,
            'app/backend/.venv is absent, so Python versions are the pyproject specifiers and are not '
            'resolutions',
        )
        return components, errors
    project = pyproject.get('project') or {}
    if not isinstance(project, dict):
        raise SbomError('pyproject.toml [project] must be a table')
    optional = project.get('optional-dependencies') or {}
    if not isinstance(optional, dict):
        raise SbomError('pyproject.toml [project.optional-dependencies] must be a table')
    runtime_requirements = project.get('dependencies') or []
    dev_requirements = optional.get('dev') or []
    runtime_roots = _requirement_names(runtime_requirements, '[project].dependencies')
    dev_roots = _requirement_names(dev_requirements, '[project.optional-dependencies].dev')
    requested_extras = _requested_extras(runtime_requirements)
    for name, extras in _requested_extras(dev_requirements).items():
        requested_extras.setdefault(name, set()).update(extras)
    errors: list[str] = []
    installed: dict[str, importlib.metadata.Distribution] = {}
    for dist in _installed_distributions(site_packages):
        canonical = _canonical_name(dist.metadata.get('Name') or '')
        if not canonical or canonical in SELF_DISTRIBUTIONS:
            continue
        existing = installed.get(canonical)
        if existing is not None:
            if existing.version != dist.version:
                errors.append(
                    f'app/backend/.venv contains more than one version of {canonical}: '
                    f'{existing.version} and {dist.version}'
                )
                if dist.version < existing.version:
                    installed[canonical] = dist
            continue
        installed[canonical] = dist
    graph: dict[str, set[str]] = {}
    for canonical, dist in installed.items():
        edges: set[str] = set()
        for requirement in dist.requires or []:
            try:
                name, _specifier = split_requirement(str(requirement))
            except SbomError:
                continue
            marker = str(requirement).split(';', 1)[1] if ';' in str(requirement) else ''
            if re.search(r'\bextra\b', marker):
                gate = set(_MARKER_EXTRA.findall(marker))
                if not gate.intersection(requested_extras.get(canonical, set())):
                    continue
            edges.add(_canonical_name(name))
        graph[canonical] = edges
    for name in sorted(runtime_roots.difference(installed)):
        errors.append(f'declared runtime dependency {name} is not installed in app/backend/.venv')
    runtime_closure = _dependency_closure(runtime_roots, graph)
    dev_closure = _dependency_closure(dev_roots, graph)
    components: list[dict[str, Any]] = []
    for canonical in sorted(installed):
        dist = installed[canonical]
        version = dist.version
        purl = _pypi_purl(canonical, version)
        properties: list[dict[str, str]] = [
            {'name': 'creatoros:environment', 'value': VENV_PATH.as_posix()},
            {'name': 'creatoros:version-source', 'value': 'venv'},
        ]
        if canonical in runtime_closure:
            scope = 'required'
        else:
            scope = 'excluded'
            if canonical in dev_closure:
                reason = (
                    'test/development tooling declared in pyproject [project.optional-dependencies].dev; '
                    'not shipped in the packaged backend'
                )
            else:
                reason = (
                    'installed in the tested environment but not reachable from declared runtime or dev '
                    'dependencies'
                )
            properties.append({'name': 'creatoros:scope-reason', 'value': reason})
        licence = _metadata_licence(dist.metadata)
        if licence is not None:
            properties.append({
                'name': 'creatoros:license-source',
                'value': 'app/backend/.venv installed metadata',
            })
        else:
            properties.append({'name': 'creatoros:license-status', 'value': 'unknown'})
        component: dict[str, Any] = {
            'type': 'library',
            'bom-ref': purl,
            'name': canonical,
            'version': version,
            'scope': scope,
            'purl': purl,
            'properties': _sorted_properties(properties),
        }
        if licence is not None:
            component['licenses'] = [licence]
        components.append(component)
    return components, errors


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_version(path: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(path), '-version'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (completed.stdout or '').splitlines():
        if line.strip():
            match = _VERSION_FROM_BANNER.search(line)
            return match.group(1) if match else None
    return None


def _plain_markdown(value: str) -> str:
    return value.replace('**', '').replace('`', '').strip()


def _manifest_sections(repo_root: Path) -> dict[str, dict[str, str]] | None:
    path = repo_root / MANIFEST_PATH
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError):
        return None
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith('### '):
            current = line[4:].strip().lower()
            sections.setdefault(current, {})
        elif line.startswith('#'):
            current = None
        elif current is not None and line.lstrip().startswith('|'):
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            if len(cells) < 2:
                continue
            field = _plain_markdown(cells[0]).lower()
            if not field or set(field) <= {'-', ':', ' '}:
                continue
            sections[current][field] = ' '.join(cells[1].split())
    return sections


def collect_bundled_components(repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return FFmpeg/ffprobe components, hashed from the real binaries and described by the manifest."""
    components: list[dict[str, Any]] = []
    errors: list[str] = []
    sections = _manifest_sections(repo_root)
    ffmpeg_rows = (sections or {}).get('ffmpeg and ffprobe')
    declaration = ''
    redistribution = ''
    licence_name = ''
    manifest_hashes: dict[str, str] = {}
    if ffmpeg_rows is None:
        errors.append(
            'docs/release/DEPENDENCY_MANIFEST.md is missing or has no FFmpeg and ffprobe section; '
            'the licence and redistribution status are not recorded'
        )
    else:
        declaration = _plain_markdown(ffmpeg_rows.get('licence', ''))
        redistribution = _plain_markdown(ffmpeg_rows.get('redistribution status', ''))
        licence_name = declaration.split(' ', 1)[0].strip(' (),') if declaration else ''
        for field, value in ffmpeg_rows.items():
            if field.endswith('sha-256'):
                manifest_hashes[field] = _plain_markdown(value).lower()
        if not declaration:
            errors.append('docs/release/DEPENDENCY_MANIFEST.md records no FFmpeg licence')
        if not redistribution:
            errors.append('docs/release/DEPENDENCY_MANIFEST.md records no FFmpeg redistribution status')
    for binary in ('ffmpeg', 'ffprobe'):
        path = repo_root / FFMPEG_DIR / f'{binary}.exe'
        relative = f'{FFMPEG_DIR.as_posix()}/{binary}.exe'
        if not path.is_file():
            errors.append(f'{relative} not found; no {binary} component was recorded')
            continue
        version = _binary_version(path)
        if not version:
            errors.append(
                f'{relative} did not report a version from its -version banner; '
                f'no {binary} component was recorded'
            )
            continue
        digest = _sha256_file(path)
        properties: list[dict[str, str]] = [
            {'name': 'creatoros:binary-path', 'value': relative},
            {'name': 'creatoros:version-source', 'value': f'{relative} -version'},
            {
                'name': 'creatoros:scope-reason',
                'value': (
                    'external prerequisite recorded for completeness; '
                    'see creatoros:redistribution-status'
                ),
            },
        ]
        component: dict[str, Any] = {
            'type': 'application',
            'bom-ref': f'creatoros-binary:{binary}@{version}',
            'name': binary,
            'version': version,
            'scope': 'excluded',
            'hashes': [{'alg': 'SHA-256', 'content': digest}],
        }
        if licence_name:
            component['licenses'] = [{'license': {'name': licence_name}}]
            properties.append({
                'name': 'creatoros:license-source',
                'value': MANIFEST_PATH.as_posix(),
            })
            properties.append({'name': 'creatoros:license-declaration', 'value': declaration})
        else:
            properties.append({'name': 'creatoros:license-status', 'value': 'unknown'})
        if redistribution:
            properties.append({'name': 'creatoros:redistribution-status', 'value': redistribution})
        declared_digest = manifest_hashes.get(f'{binary}.exe sha-256')
        if declared_digest:
            if declared_digest != digest:
                message = (
                    f'{relative} SHA-256 {digest} does not match the manifest value {declared_digest}'
                )
                errors.append(message)
                properties.append({'name': 'creatoros:collection-error', 'value': message})
            else:
                properties.append({'name': 'creatoros:manifest-sha256', 'value': declared_digest})
        component['properties'] = _sorted_properties(properties)
        components.append(component)
    return components, errors


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _declared_ollama(data: dict[str, Any], manifest: Path) -> dict[str, Any] | None:
    """Normalise the Ollama facts from a prerequisites manifest.

    The shipped `creatoros.prerequisites.v1` document is the authoritative shape: a `components`
    list keyed by `id`. The earlier flat `{"ollama": {...}}` shape is still accepted so an older
    release procedure keeps working, but a different schema is an error rather than a silent skip.
    """
    schema = data.get('schema')
    if schema == PREREQUISITES_SCHEMA:
        components = data.get('components')
        if not isinstance(components, list):
            raise SbomError(f'prerequisites manifest {manifest} has no components list')
        for entry in components:
            if not isinstance(entry, dict) or entry.get('id') != 'ollama':
                continue
            acquisition = entry.get('acquisition') if isinstance(entry.get('acquisition'), dict) else {}
            signature = entry.get('signature') if isinstance(entry.get('signature'), dict) else {}
            licence = entry.get('license') if isinstance(entry.get('license'), dict) else {}
            return {
                'version': entry.get('version'),
                'sha256': acquisition.get('sha256'),
                'size': acquisition.get('size_bytes'),
                'signer_subject': signature.get('expected_subject'),
                'licence': licence.get('spdx') or licence.get('name'),
                'source': 'creatoros.prerequisites.v1',
            }
        return None
    if schema:
        raise SbomError(f'prerequisites manifest {manifest} declares unsupported schema {schema!r}')
    return data.get('ollama')


def collect_prerequisite_components(
    repo_root: Path, manifest: Path | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return Ollama only when the prerequisites manifest declares it."""
    if manifest is None:
        return [], []
    if not manifest.is_file():
        raise SbomError(f'prerequisites manifest not found: {manifest}')
    data = _read_json(manifest, 'prerequisites manifest')
    declaration = _declared_ollama(data, manifest)
    if declaration is None:
        return [], []
    if not isinstance(declaration, dict):
        raise SbomError(f'prerequisites manifest {manifest} has a non-object ollama entry')
    errors: list[str] = []
    version = declaration.get('version')
    if not isinstance(version, str) or not version.strip():
        raise SbomError('prerequisites manifest declares Ollama without a version')
    version = version.strip()
    digest = str(declaration.get('sha256', '')).strip().lower()
    if not _is_sha256_hex(digest):
        raise SbomError('prerequisites manifest declares Ollama without a valid SHA-256 digest')
    properties: list[dict[str, str]] = [
        {'name': 'creatoros:source', 'value': _display_path(manifest, repo_root)},
        {'name': 'creatoros:version-source', 'value': 'prerequisites manifest'},
        {
            'name': 'creatoros:scope-reason',
            'value': 'external prerequisite declared by the prerequisites manifest; not bundled',
        },
    ]
    size = declaration.get('size')
    if isinstance(size, int) and not isinstance(size, bool) and size > 0:
        properties.append({'name': 'creatoros:payload-size-bytes', 'value': str(size)})
    else:
        errors.append('prerequisites manifest declares Ollama without a positive byte size')
    signer = ''
    for key in ('signer_subject', 'expected_signer_subject', 'signer', 'expected_signer'):
        raw_signer = declaration.get(key)
        if isinstance(raw_signer, str) and raw_signer.strip():
            signer = raw_signer.strip()
            break
    if signer:
        properties.append({'name': 'creatoros:expected-signer-subject', 'value': signer})
    else:
        errors.append('prerequisites manifest declares Ollama without an expected signer subject')
    component: dict[str, Any] = {
        'type': 'application',
        'bom-ref': f'creatoros-prerequisite:ollama@{version}',
        'name': 'ollama',
        'version': version,
        'scope': 'excluded',
        'hashes': [{'alg': 'SHA-256', 'content': digest}],
    }
    licence_raw = declaration.get('licence') or declaration.get('license')
    licence = spdx_licence_entry(str(licence_raw)) if isinstance(licence_raw, str) else None
    if licence is not None:
        component['licenses'] = [licence]
        properties.append({
            'name': 'creatoros:license-source',
            'value': 'prerequisites manifest',
        })
    else:
        properties.append({'name': 'creatoros:license-status', 'value': 'unknown'})
    sections = _manifest_sections(repo_root) or {}
    ollama_rows = sections.get('ollama') or {}
    if ollama_rows:
        status = _plain_markdown(ollama_rows.get('redistribution status', ''))
        if status:
            properties.append({'name': 'creatoros:redistribution-status', 'value': status})
        repo_version = _plain_markdown(ollama_rows.get('version', ''))
        if repo_version and repo_version != version:
            errors.append(
                f'prerequisites manifest version {version} differs from '
                f'DEPENDENCY_MANIFEST.md {repo_version}'
            )
        repo_digest = _plain_markdown(ollama_rows.get('sha-256', '')).lower()
        if repo_digest and repo_digest != digest:
            errors.append('prerequisites manifest Ollama SHA-256 differs from DEPENDENCY_MANIFEST.md')
    component['properties'] = _sorted_properties(properties)
    return [component], errors


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and _HEX.fullmatch(value) is not None


def _application_component(
    name: str, version: str, source: Path, licence: dict[str, Any] | None, role: str
) -> dict[str, Any]:
    properties: list[dict[str, str]] = [
        {'name': 'creatoros:source', 'value': source.as_posix()},
        {'name': 'creatoros:role', 'value': role},
    ]
    component: dict[str, Any] = {
        'type': 'application',
        'bom-ref': f'{name}@{version}',
        'name': name,
        'version': version,
        'scope': 'required',
    }
    if licence is not None:
        component['licenses'] = [licence]
        properties.append({'name': 'creatoros:license-source', 'value': 'LICENSE'})
    else:
        properties.append({'name': 'creatoros:license-status', 'value': 'unknown'})
    component['properties'] = _sorted_properties(properties)
    return component


def _declared_ffmpeg_components(repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ffmpeg/ffprobe from the audit record, without reading a local binary.

    This is the prerequisite profile: it must be reproducible on a machine that has not downloaded
    the tools, and it records the declared hashes and licence status rather than re-hashing bytes
    that may be absent. The artifact profile still checks the real binaries.
    """
    sections = _manifest_sections(repo_root) or {}
    rows = sections.get('ffmpeg and ffprobe')
    if not rows:
        return [], [
            'docs/release/DEPENDENCY_MANIFEST.md has no FFmpeg and ffprobe section; '
            + 'no prerequisite component was recorded'
        ]
    version = _plain_markdown(rows.get('version', '')) or 'unknown'
    licence_declaration = _plain_markdown(rows.get('licence', ''))
    redistribution = _plain_markdown(rows.get('redistribution status', ''))
    licence_name = licence_declaration.split(' ', 1)[0].strip(' (),') if licence_declaration else ''
    errors: list[str] = []
    if not licence_declaration:
        errors.append('DEPENDENCY_MANIFEST.md records no FFmpeg licence')
    if not redistribution:
        errors.append('DEPENDENCY_MANIFEST.md records no FFmpeg redistribution status')
    components: list[dict[str, Any]] = []
    for binary in ('ffmpeg', 'ffprobe'):
        digest = _plain_markdown(rows.get(f'{binary}.exe sha-256', '')).lower()
        if not _is_sha256_hex(digest):
            errors.append(f'DEPENDENCY_MANIFEST.md has no valid {binary}.exe SHA-256')
            continue
        properties: list[dict[str, str]] = [
            {'name': 'creatoros:source', 'value': MANIFEST_PATH.as_posix()},
            {'name': 'creatoros:version-source', 'value': MANIFEST_PATH.as_posix()},
            {
                'name': 'creatoros:scope-reason',
                'value': 'external prerequisite declared by the dependency audit record; not bundled',
            },
        ]
        component: dict[str, Any] = {
            'type': 'application',
            'bom-ref': f'creatoros-prerequisite:{binary}@{version}',
            'name': binary,
            'version': version,
            'scope': 'excluded',
            'hashes': [{'alg': 'SHA-256', 'content': digest}],
        }
        if licence_name:
            component['licenses'] = [{'license': {'name': licence_name}}]
            properties.append(
                {'name': 'creatoros:license-declaration', 'value': licence_declaration}
            )
        else:
            properties.append({'name': 'creatoros:license-status', 'value': 'unknown'})
        if redistribution:
            properties.append({'name': 'creatoros:redistribution-status', 'value': redistribution})
        component['properties'] = _sorted_properties(properties)
        components.append(component)
    return components, errors


def collect_components(
    repo_root: Path,
    prerequisites: Path | None = None,
    profile: str = 'all',
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect components for one SBOM profile and return (components, collection errors).

    Profiles separate three different claims: `source` describes the code and its declared
    dependencies, `prerequisites` describes the external tools the user supplies, and `artifact`
    describes the distributable bytes (including the installer hash). `all` keeps the historical
    combined document for callers that have not migrated.
    """
    if profile not in PROFILES:
        raise SbomError(f'unknown SBOM profile {profile!r}; expected one of {sorted(PROFILES)}')
    repo_root = repo_root.resolve()
    desktop_version = read_desktop_version(repo_root)
    pyproject = read_backend_pyproject(repo_root)
    backend_version = read_backend_version(pyproject, repo_root)
    licence = read_licence(repo_root)
    errors: list[str] = []
    if licence is None:
        errors.append(
            'LICENSE is missing or is not the Apache-2.0 text; application licences are recorded as unknown'
        )
    components: list[dict[str, Any]] = [
        _application_component(
            'creatoros-desktop',
            desktop_version,
            PACKAGE_JSON_PATH,
            licence,
            'Electron desktop shell',
        ),
        _application_component(
            'creatoros-backend',
            backend_version,
            PYPROJECT_PATH,
            licence,
            'local FastAPI service',
        ),
    ]
    if profile == 'prerequisites':
        ffmpeg_components, ffmpeg_errors = _declared_ffmpeg_components(repo_root)
        ollama_components, ollama_errors = collect_prerequisite_components(repo_root, prerequisites)
        components = ffmpeg_components + ollama_components
        errors.extend(ffmpeg_errors)
        errors.extend(ollama_errors)
        return components, errors
    node_components, node_errors = collect_node_components(repo_root)
    python_components, python_errors = collect_python_components(repo_root, pyproject)
    components.extend(node_components)
    components.extend(python_components)
    errors.extend(node_errors)
    errors.extend(python_errors)
    if profile == 'source':
        return components, errors
    if profile == 'artifact':
        # The installer does not carry FFmpeg or Ollama. The artifact document binds the bytes it
        # does carry and describes the external prerequisites from the audit record, exactly as the
        # prerequisite document does, so the source inventory is a subset of the artifact inventory.
        ffmpeg_components, ffmpeg_errors = _declared_ffmpeg_components(repo_root)
        prerequisite_components, prerequisite_errors = collect_prerequisite_components(
            repo_root, prerequisites
        )
        components.extend(ffmpeg_components)
        components.extend(prerequisite_components)
        errors.extend(ffmpeg_errors)
        errors.extend(prerequisite_errors)
        return components, errors
    bundled_components, bundled_errors = collect_bundled_components(repo_root)
    prerequisite_components, prerequisite_errors = collect_prerequisite_components(
        repo_root, prerequisites
    )
    components.extend(bundled_components)
    components.extend(prerequisite_components)
    errors.extend(bundled_errors)
    errors.extend(prerequisite_errors)
    return components, errors


def _parse_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def normalise_timestamp(value: str | None) -> str:
    """Return an ISO 8601 UTC timestamp with second precision."""
    if value is None:
        return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise SbomError(f'--timestamp is not an ISO 8601 timestamp: {value!r}')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _component_sort_key(component: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(component.get('type', '')),
        str(component.get('name', '')),
        str(component.get('version', '')),
        str(component.get('bom-ref', '')),
    )


def compute_serial(components: Sequence[dict[str, Any]]) -> str:
    """Return the UUIDv5 serial derived from the canonical sorted component list."""
    ordered = sorted(components, key=_component_sort_key)
    payload = json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return 'urn:uuid:' + str(uuid.uuid5(SERIAL_NAMESPACE, payload))


def normalise_serial(serial: str | None, components: Sequence[dict[str, Any]]) -> str:
    """Return a urn:uuid serial, either the given UUID or the derived UUIDv5."""
    if serial is None:
        return compute_serial(components)
    candidate = serial.strip()
    if candidate.lower().startswith('urn:uuid:'):
        candidate = candidate[len('urn:uuid:'):]
    try:
        parsed = uuid.UUID(candidate)
    except ValueError as exc:
        raise SbomError(f'--serial is not a UUID: {serial!r}') from exc
    return f'urn:uuid:{parsed}'


def build_document(
    components: Sequence[dict[str, Any]],
    product_version: str,
    licence: dict[str, Any] | None,
    *,
    desktop_version: str | None = None,
    collection_errors: Iterable[str] = (),
    timestamp: str | None = None,
    serial: str | None = None,
    artifact: tuple[str, str, int] | None = None,
) -> dict[str, Any]:
    """Build the CycloneDX 1.5 document; components are sorted by (type, name, version)."""
    ordered = sorted(
        (dict(component) for component in components),
        key=_component_sort_key,
    )
    product_properties: list[dict[str, str]] = []
    product_component: dict[str, Any] = {
        'type': 'application',
        'bom-ref': f'creatoros@{product_version}',
        'name': 'CreatorOS',
        'version': product_version,
        'scope': 'required',
    }
    if licence is not None:
        product_component['licenses'] = [licence]
        product_properties.append({'name': 'creatoros:license-source', 'value': 'LICENSE'})
    else:
        product_properties.append({'name': 'creatoros:license-status', 'value': 'unknown'})
    if desktop_version is not None and desktop_version != product_version:
        product_properties.append(
            {'name': 'creatoros:desktop-package-version', 'value': desktop_version}
        )
    if artifact is not None:
        artifact_name, artifact_hash, artifact_size = artifact
        product_component['hashes'] = [{'alg': 'SHA-256', 'content': artifact_hash}]
        product_properties.append({'name': 'creatoros:artifact-name', 'value': artifact_name})
        product_properties.append(
            {'name': 'creatoros:artifact-size-bytes', 'value': str(artifact_size)}
        )
    product_component['properties'] = _sorted_properties(product_properties)
    document: dict[str, Any] = {
        '$schema': SCHEMA_URL,
        'bomFormat': BOM_FORMAT,
        'specVersion': SPEC_VERSION,
        'serialNumber': normalise_serial(serial, ordered),
        'version': 1,
        'metadata': {
            'timestamp': normalise_timestamp(timestamp),
            'tools': [
                {
                    'vendor': 'CreatorOS',
                    'name': GENERATOR_NAME,
                    'version': GENERATOR_VERSION,
                }
            ],
            'component': product_component,
        },
        'components': ordered,
    }
    errors = sorted({str(error) for error in collection_errors if str(error).strip()})
    if errors:
        document['properties'] = [
            {'name': 'creatoros:collection-error', 'value': error} for error in errors
        ]
    return document


def _check_component(component: dict[str, Any], label: str, problems: list[str]) -> None:
    name = component.get('name')
    if not isinstance(name, str) or not name.strip():
        problems.append(f'{label} has no name')
    component_type = component.get('type')
    if not isinstance(component_type, str) or not component_type.strip():
        problems.append(f'{label} has no type')
    bom_ref = component.get('bom-ref')
    if not isinstance(bom_ref, str) or not bom_ref.strip():
        problems.append(f'{label} has no bom-ref')
    if component_type == 'application':
        version = component.get('version')
        if not isinstance(version, str) or not version.strip():
            problems.append(f'{label} is an application without a version')
    hashes = component.get('hashes')
    if hashes is None:
        return
    if not isinstance(hashes, list):
        problems.append(f'{label}.hashes is not an array')
        return
    for index, item in enumerate(hashes):
        if not isinstance(item, dict):
            problems.append(f'{label}.hashes[{index}] is not an object')
            continue
        algorithm = item.get('alg')
        content = item.get('content')
        if not isinstance(algorithm, str) or algorithm not in _HASH_LENGTHS:
            problems.append(f'{label}.hashes[{index}] has unsupported alg {algorithm!r}')
            continue
        if not isinstance(content, str):
            problems.append(f'{label}.hashes[{index}] has no string content')
            continue
        expected = _HASH_LENGTHS[algorithm]
        if len(content) != expected or _HEX.fullmatch(content) is None:
            problems.append(
                f'{label}.hashes[{index}] is not lowercase hex {algorithm} ({expected} characters)'
            )


def check_document(document: Any) -> list[str]:
    """Return structural problems in a CycloneDX document; an empty list means it passed."""
    if not isinstance(document, dict):
        return ['document is not a JSON object']
    problems: list[str] = []
    for field in ('bomFormat', 'specVersion', 'serialNumber', 'version', 'metadata', 'components'):
        if field not in document:
            problems.append(f'missing top-level field: {field}')
    if document.get('bomFormat') != BOM_FORMAT:
        problems.append(f'bomFormat is {document.get("bomFormat")!r}, expected {BOM_FORMAT!r}')
    if document.get('specVersion') != SPEC_VERSION:
        problems.append(f'specVersion is {document.get("specVersion")!r}, expected {SPEC_VERSION!r}')
    serial = document.get('serialNumber')
    if 'serialNumber' in document:
        if not isinstance(serial, str):
            problems.append('serialNumber is not a string')
        else:
            prefix = 'urn:uuid:'
            if not serial.lower().startswith(prefix):
                problems.append(f'serialNumber does not start with {prefix}')
            else:
                try:
                    uuid.UUID(serial[len(prefix):])
                except ValueError:
                    problems.append(f'serialNumber is not a UUID: {serial!r}')
    revision = document.get('version')
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        problems.append('version must be a positive integer')
    metadata = document.get('metadata')
    if isinstance(metadata, dict):
        timestamp = metadata.get('timestamp')
        if not isinstance(timestamp, str) or _parse_timestamp(timestamp) is None:
            problems.append('metadata.timestamp is missing or not ISO 8601')
        tools = metadata.get('tools')
        if not isinstance(tools, list) or not tools:
            problems.append('metadata.tools must list at least one tool')
        product = metadata.get('component')
        if not isinstance(product, dict):
            problems.append('metadata.component is missing or not an object')
        else:
            _check_component(product, 'metadata.component', problems)
    elif 'metadata' in document:
        problems.append('metadata is not an object')
    components = document.get('components')
    if not isinstance(components, list):
        problems.append('components is not an array')
    elif not components:
        problems.append('no components')
    else:
        seen_refs: set[str] = set()
        for index, component in enumerate(components):
            label = f'components[{index}]'
            if not isinstance(component, dict):
                problems.append(f'{label} is not an object')
                continue
            _check_component(component, label, problems)
            bom_ref = component.get('bom-ref')
            if isinstance(bom_ref, str) and bom_ref:
                if bom_ref in seen_refs:
                    problems.append(f'{label} has duplicate bom-ref {bom_ref!r}')
                seen_refs.add(bom_ref)
    return problems


def check_file(path: Path) -> list[str]:
    """Re-read a written document and return its structural problems."""
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        return [f'cannot read {path}: {exc}']
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f'document is not valid JSON: {exc}']
    return check_document(document)


def write_document(document: dict[str, Any], output: Path) -> None:
    """Write the document as UTF-8 with LF newlines so Windows does not change the bytes."""
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, ensure_ascii=False) + '\n'
    output.write_text(text, encoding='utf-8', newline='\n')


def _artifact_binding(path: Path) -> tuple[str, str, int]:
    """Return (filename, lowercase SHA-256, size) for the artifact this SBOM describes."""
    if not path.is_file():
        raise SbomError(f'--artifact is not a file: {path}')
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return path.name, digest.hexdigest(), size


def generate_sbom(
    repo_root: Path,
    output: Path,
    *,
    version: str | None = None,
    prerequisites: Path | None = None,
    timestamp: str | None = None,
    serial: str | None = None,
    artifact: Path | None = None,
    profile: str = 'all',
) -> dict[str, Any]:
    """Collect, build and write the SBOM; returns the document that was written."""
    repo_root = repo_root.resolve()
    desktop_version = read_desktop_version(repo_root)
    pyproject = read_backend_pyproject(repo_root)
    read_backend_version(pyproject, repo_root)
    licence = read_licence(repo_root)
    product_version = version.strip() if version is not None else desktop_version
    if not product_version:
        raise SbomError('--version must not be empty')
    components, errors = collect_components(repo_root, prerequisites, profile=profile)
    binding = _artifact_binding(artifact) if artifact is not None else None
    document = build_document(
        components,
        product_version,
        licence,
        desktop_version=desktop_version,
        collection_errors=errors,
        timestamp=timestamp,
        serial=serial,
        artifact=binding,
    )
    write_document(document, output)
    return document


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog='generate_sbom.py',
        description='Generate a reproducible CycloneDX 1.5 SBOM for CreatorOS from repository inputs.',
    )
    parser.add_argument('--output', required=True, type=Path, help='CycloneDX JSON file to write')
    parser.add_argument(
        '--repo-root',
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help='repository root to read from (default: the checkout containing this script)',
    )
    parser.add_argument(
        '--version',
        default=None,
        help='product version for metadata.component (default: app/desktop/package.json)',
    )
    parser.add_argument(
        '--prerequisites',
        type=Path,
        default=None,
        help='JSON manifest declaring external prerequisites such as Ollama',
    )
    parser.add_argument(
        '--profile',
        choices=sorted(PROFILES),
        default='all',
        help='source: code and declared dependencies; prerequisites: external tools; artifact: distributable bytes (default: all)',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='re-read the written document and exit 1 on structural problems',
    )
    parser.add_argument(
        '--artifact',
        type=Path,
        default=None,
        help='installer file whose SHA-256 and size are recorded on metadata.component',
    )
    parser.add_argument('--timestamp', default=None, help='ISO 8601 timestamp instead of now')
    parser.add_argument('--serial', default=None, help='UUID to use as the bom serial number')
    args = parser.parse_args(argv)
    try:
        document = generate_sbom(
            args.repo_root,
            args.output,
            version=args.version,
            prerequisites=args.prerequisites,
            timestamp=args.timestamp,
            serial=args.serial,
            artifact=args.artifact,
            profile=args.profile,
        )
    except SbomError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except OSError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    for message in _document_collection_errors(document):
        print(f'warning: {message}', file=sys.stderr)
    component_count = len(document.get('components', []))
    if args.check:
        problems = check_file(args.output)
        if problems:
            print(f'check failed for {args.output}:', file=sys.stderr)
            for problem in problems:
                print(f'  - {problem}', file=sys.stderr)
            return 1
        print(
            f'wrote {args.output} ({component_count} components, profile {args.profile}, '
            f'{document["serialNumber"]}); check passed'
        )
        return 0
    print(f'wrote {args.output} ({component_count} components, {document["serialNumber"]})')
    return 0


def _document_collection_errors(document: dict[str, Any]) -> list[str]:
    properties = document.get('properties') or []
    return [
        str(item.get('value', ''))
        for item in properties
        if isinstance(item, dict) and item.get('name') == 'creatoros:collection-error'
    ]


if __name__ == '__main__':
    raise SystemExit(main())
