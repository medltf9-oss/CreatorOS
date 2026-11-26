"""The release SBOM generator, checked against the real repository.

`scripts/generate_sbom.py` is only useful if its output is tied to files that actually exist. These
tests load the script by path (the same pattern as test_live_verify_harness.py), call its public
functions directly, and assert against app/desktop/package.json, app/desktop/pnpm-lock.yaml, the
backend virtual environment and the real FFmpeg binaries rather than fixtures invented for the test.
Nothing here reaches the network and every file written stays under tmp_path.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / 'scripts' / 'generate_sbom.py'
FFMPEG_BIN = REPO_ROOT / 'tools' / 'ffmpeg' / 'bin'
VENV_SITE_PACKAGES = REPO_ROOT / 'app' / 'backend' / '.venv' / 'Lib' / 'site-packages'


def _load_generator():
    spec = importlib.util.spec_from_file_location('generate_sbom', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def sbom():
    return _load_generator()


def _by_name(document, name):
    return [component for component in document['components'] if component['name'] == name]


def _properties(component):
    return {item['name']: item['value'] for item in component['properties']}


@pytest.fixture(scope='module')
def document(sbom):
    components, errors = sbom.collect_components(REPO_ROOT)
    return sbom.build_document(
        components,
        sbom.read_desktop_version(REPO_ROOT),
        sbom.read_licence(REPO_ROOT),
        desktop_version=sbom.read_desktop_version(REPO_ROOT),
        collection_errors=errors,
        timestamp='2026-01-01T00:00:00Z',
    )


def test_desktop_version_matches_package_json(sbom, document):
    package = json.loads((REPO_ROOT / 'app' / 'desktop' / 'package.json').read_text(encoding='utf-8'))
    desktop = _by_name(document, 'creatoros-desktop')
    assert len(desktop) == 1
    assert desktop[0]['type'] == 'application'
    assert desktop[0]['version'] == package['version']
    assert document['metadata']['component']['version'] == package['version']


def test_react_is_resolved_exactly_once_from_the_lockfile(sbom, document):
    react = _by_name(document, 'react')
    assert len(react) == 1
    lockfile = (REPO_ROOT / 'app' / 'desktop' / 'pnpm-lock.yaml').read_text(encoding='utf-8')
    importer = sbom.parse_pnpm_lockfile(lockfile)['importers']['.']
    resolved = importer['dependencies']['react']['version'].split('(', 1)[0]
    assert react[0]['version'] == resolved
    assert react[0]['scope'] == 'required'
    assert react[0]['purl'] == f'pkg:npm/react@{resolved}'
    assert _properties(react[0])['creatoros:version-source'] == 'pnpm-lockfile'


def test_dev_dependencies_are_excluded_with_a_build_time_reason(document):
    electron = _by_name(document, 'electron')
    assert len(electron) == 1
    assert electron[0]['scope'] == 'excluded'
    assert 'build-time' in _properties(electron[0])['creatoros:scope-reason']


@pytest.mark.skipif(
    not (FFMPEG_BIN / 'ffmpeg.exe').is_file(),
    reason='tools/ffmpeg/bin is absent from this checkout',
)
def test_ffmpeg_hash_is_the_sha256_of_the_real_binary(document):
    binary = FFMPEG_BIN / 'ffmpeg.exe'
    expected = hashlib.sha256(binary.read_bytes()).hexdigest()
    ffmpeg = _by_name(document, 'ffmpeg')
    assert len(ffmpeg) == 1
    assert ffmpeg[0]['hashes'] == [{'alg': 'SHA-256', 'content': expected}]
    assert '9.1' in ffmpeg[0]['version']


@pytest.mark.skipif(
    not (FFMPEG_BIN / 'ffmpeg.exe').is_file(),
    reason='tools/ffmpeg/bin is absent from this checkout',
)
def test_ffmpeg_redistribution_status_is_not_upgraded(document):
    ffmpeg = _by_name(document, 'ffmpeg')
    assert len(ffmpeg) == 1
    status = _properties(ffmpeg[0])['creatoros:redistribution-status']
    assert status.startswith('Blocked for bundling.')


def test_components_are_sorted_and_carry_a_unique_bom_ref(document):
    keys = [(item['type'], item['name'], item.get('version', '')) for item in document['components']]
    assert keys == sorted(keys)
    refs = [item['bom-ref'] for item in document['components']]
    assert len(refs) == len(set(refs))


def test_serial_is_derived_from_the_component_list(sbom, document):
    assert document['serialNumber'] == sbom.compute_serial(document['components'])
    augmented = [dict(component) for component in document['components']]
    augmented.append({'type': 'library', 'bom-ref': 'pkg:npm/example@1.0.0', 'name': 'example'})
    assert sbom.compute_serial(augmented) != document['serialNumber']


def test_both_application_components_carry_the_apache_licence(document):
    for name in ('creatoros-desktop', 'creatoros-backend'):
        components = _by_name(document, name)
        assert len(components) == 1
        licences = components[0].get('licenses')
        assert licences, f'{name} has no licence entry'
        assert licences[0]['license']['id'] == 'Apache-2.0'


def test_check_accepts_the_generated_document_and_rejects_a_mutated_copy(sbom, document, tmp_path):
    assert sbom.check_document(document) == []
    duplicate = json.loads(json.dumps(document))
    duplicate['components'].append(dict(duplicate['components'][0]))
    assert any('duplicate bom-ref' in problem for problem in sbom.check_document(duplicate))
    unversioned = json.loads(json.dumps(document))
    for component in unversioned['components']:
        if component['name'] == 'creatoros-backend':
            component.pop('version', None)
    assert any('without a version' in problem for problem in sbom.check_document(unversioned))
    bad_hash = json.loads(json.dumps(document))
    bad_hash['components'][0]['hashes'] = [{'alg': 'SHA-256', 'content': 'not-a-hash'}]
    assert any('lowercase hex' in problem for problem in sbom.check_document(bad_hash))
    missing_serial = json.loads(json.dumps(document))
    missing_serial.pop('serialNumber')
    assert any('serialNumber' in problem for problem in sbom.check_document(missing_serial))
    mutated_path = tmp_path / 'mutated.cdx.json'
    mutated_path.write_text(json.dumps(duplicate), encoding='utf-8')
    assert sbom.check_file(mutated_path)


def test_two_generations_differ_only_in_the_timestamp(sbom, tmp_path):
    first_path = tmp_path / 'first.cdx.json'
    second_path = tmp_path / 'second.cdx.json'
    first = sbom.generate_sbom(REPO_ROOT, first_path, timestamp='2026-01-01T00:00:00Z')
    second = sbom.generate_sbom(REPO_ROOT, second_path, timestamp='2026-01-02T03:04:05Z')
    first_copy = json.loads(json.dumps(first))
    second_copy = json.loads(json.dumps(second))
    first_copy['metadata'].pop('timestamp')
    second_copy['metadata'].pop('timestamp')
    assert first_copy == second_copy
    assert first['serialNumber'] == second['serialNumber']
    assert first['metadata']['timestamp'] != second['metadata']['timestamp']
    assert first_path.read_bytes() != second_path.read_bytes()
    repeated = sbom.generate_sbom(
        REPO_ROOT, tmp_path / 'repeated.cdx.json', timestamp='2026-01-01T00:00:00Z'
    )
    assert repeated == first
    assert (tmp_path / 'repeated.cdx.json').read_bytes() == first_path.read_bytes()


def test_cli_writes_and_checks_the_document(sbom, tmp_path):
    output = tmp_path / 'cli.cdx.json'
    exit_code = sbom.main(
        ['--output', str(output), '--timestamp', '2026-01-01T00:00:00Z', '--check']
    )
    assert exit_code == 0
    written = json.loads(output.read_text(encoding='utf-8'))
    assert written['bomFormat'] == 'CycloneDX'
    assert written['specVersion'] == '1.5'
    assert sbom.check_file(output) == []


def test_artifact_binding_records_the_installer_hash(sbom, tmp_path):
    artifact = tmp_path / 'CreatorOS-Setup-0.0.0.exe'
    artifact.write_bytes(b'MZ' + b'\0' * 1024)
    output = tmp_path / 'bound.cdx.json'
    document = sbom.generate_sbom(
        REPO_ROOT,
        output,
        timestamp='2026-01-01T00:00:00Z',
        artifact=artifact,
    )
    product = document['metadata']['component']
    recorded = [item['content'] for item in product['hashes']]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() in recorded
    properties = {item['name']: item['value'] for item in product['properties']}
    assert properties['creatoros:artifact-name'] == artifact.name
    assert properties['creatoros:artifact-size-bytes'] == str(artifact.stat().st_size)
    assert sbom.check_file(output) == []


def test_artifact_binding_rejects_a_missing_file(sbom, tmp_path):
    with pytest.raises(sbom.SbomError, match='artifact is not a file'):
        sbom.generate_sbom(REPO_ROOT, tmp_path / 'x.cdx.json', artifact=tmp_path / 'absent.exe')


def test_a_missing_lockfile_is_a_hard_failure(sbom, tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'app' / 'desktop').mkdir(parents=True)
    (repo / 'app' / 'backend').mkdir(parents=True)
    package = {'name': 'creatoros-desktop', 'version': '1.2.3'}
    (repo / 'app' / 'desktop' / 'package.json').write_text(json.dumps(package), encoding='utf-8')
    pyproject = '[project]\nname = "creatoros-backend"\nversion = "1.2.3"\ndependencies = []\n'
    (repo / 'app' / 'backend' / 'pyproject.toml').write_text(pyproject, encoding='utf-8')
    (repo / 'LICENSE').write_text('Apache License\nVersion 2.0\n', encoding='utf-8')
    with pytest.raises(sbom.SbomError, match='lockfile'):
        sbom.generate_sbom(repo, tmp_path / 'out.cdx.json')


def test_python_fallback_records_specifiers_not_invented_versions(sbom, tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'app' / 'backend').mkdir(parents=True)
    pyproject = (
        '[project]\n'
        'name = "creatoros-backend"\n'
        'version = "1.2.3"\n'
        'dependencies = ["fastapi>=0.115", "tzdata>=2024.1; sys_platform == \'win32\'"]\n'
    )
    (repo / 'app' / 'backend' / 'pyproject.toml').write_text(pyproject, encoding='utf-8')
    parsed = sbom.read_backend_pyproject(repo)
    components, errors = sbom.collect_python_components(repo, parsed)
    by_name = {component['name']: component for component in components}
    assert by_name['fastapi']['version'] == '>=0.115'
    assert _properties(by_name['fastapi'])['creatoros:version-source'] == 'pyspecifier'
    assert any('venv is absent' in error for error in errors)


@pytest.mark.skipif(
    not VENV_SITE_PACKAGES.is_dir(),
    reason='app/backend/.venv is absent from this checkout',
)
def test_python_components_match_the_installed_venv(sbom):
    installed = {}
    for dist in importlib.metadata.distributions(path=[str(VENV_SITE_PACKAGES)]):
        name = dist.metadata.get('Name')
        if name:
            installed.setdefault(re.sub(r'[-_.]+', '-', name).lower(), dist.version)
    components, _errors = sbom.collect_python_components(
        REPO_ROOT, sbom.read_backend_pyproject(REPO_ROOT)
    )
    by_name = {component['name']: component for component in components}
    assert 'fastapi' in by_name
    assert by_name['fastapi']['version'] == installed['fastapi']
    assert _properties(by_name['fastapi'])['creatoros:version-source'] == 'venv'
    assert 'creatoros-backend' not in by_name


def test_ollama_is_only_added_from_a_prerequisites_manifest(sbom, document, tmp_path):
    assert not _by_name(document, 'ollama')
    manifest = tmp_path / 'prerequisites.json'
    manifest.write_text(
        json.dumps(
            {
                'ollama': {
                    'version': '0.40.18',
                    'size': 1953587080,
                    'sha256': 'AD2BB47AA969B21CF8DD81B2D9F9A6D5030270ED242EF55087118C37C510ACC2',
                    'signer_subject': 'CN=Ollama Inc., O=Ollama Inc., L=Toronto, S=Ontario, C=CA',
                }
            }
        ),
        encoding='utf-8',
    )
    components, errors = sbom.collect_prerequisite_components(REPO_ROOT, manifest)
    assert errors == []
    assert len(components) == 1
    ollama = components[0]
    assert ollama['name'] == 'ollama'
    assert ollama['version'] == '0.40.18'
    assert ollama['hashes'] == [
        {
            'alg': 'SHA-256',
            'content': 'ad2bb47aa969b21cf8dd81b2d9f9a6d5030270ed242ef55087118c37c510acc2',
        }
    ]
    properties = _properties(ollama)
    assert properties['creatoros:expected-signer-subject'].startswith('CN=Ollama Inc.')
    assert properties['creatoros:payload-size-bytes'] == '1953587080'

def test_the_shipped_prerequisites_manifest_is_what_the_generator_reads(sbom):
    """The manifest the wizard verifies against is the same file the SBOM is built from."""
    manifest = REPO_ROOT / 'app' / 'backend' / 'src' / 'creatoros' / 'resources' / 'prerequisites.v1.json'
    declared = json.loads(manifest.read_text(encoding='utf-8'))
    assert declared['schema'] == 'creatoros.prerequisites.v1'
    ollama_entry = next(entry for entry in declared['components'] if entry['id'] == 'ollama')

    components, errors = sbom.collect_prerequisite_components(REPO_ROOT, manifest)

    assert errors == []
    assert len(components) == 1
    ollama = components[0]
    assert ollama['scope'] == 'excluded'
    assert ollama['version'] == ollama_entry['version']
    assert ollama['hashes'] == [
        {'alg': 'SHA-256', 'content': ollama_entry['acquisition']['sha256']}
    ]
    properties = _properties(ollama)
    assert properties['creatoros:payload-size-bytes'] == str(ollama_entry['acquisition']['size_bytes'])
    assert properties['creatoros:expected-signer-subject'] == ollama_entry['signature']['expected_subject']
    assert 'creatoros:redistribution-status' in properties


def test_source_profile_excludes_prerequisites_and_bundled_binaries(sbom, tmp_path):
    """A source SBOM describes the checkout, not tools that may not be downloaded yet."""
    document = sbom.generate_sbom(
        REPO_ROOT,
        tmp_path / 'source.cdx.json',
        profile='source',
        timestamp='2026-11-26T00:00:00Z',
    )
    names = {component['name'] for component in document['components']}
    assert 'creatoros-desktop' in names
    assert 'creatoros-backend' in names
    assert 'ffmpeg' not in names and 'ffprobe' not in names
    assert not [
        item for item in document.get('properties', [])
        if item.get('name') == 'creatoros:collection-error'
    ]


def test_prerequisites_profile_uses_declared_hashes_without_reading_binaries(sbom, tmp_path):
    """The prerequisite SBOM must be reproducible on a machine with no FFmpeg download."""
    manifest = REPO_ROOT / 'app' / 'backend' / 'src' / 'creatoros' / 'resources' / 'prerequisites.v1.json'
    document = sbom.generate_sbom(
        REPO_ROOT,
        tmp_path / 'prerequisites.cdx.json',
        profile='prerequisites',
        prerequisites=manifest,
        timestamp='2026-11-26T00:00:00Z',
    )
    components = {component['name']: component for component in document['components']}
    assert set(components) == {'ffmpeg', 'ffprobe', 'ollama'}
    sections = sbom._manifest_sections(REPO_ROOT) or {}
    declared = sections['ffmpeg and ffprobe']
    for binary in ('ffmpeg', 'ffprobe'):
        component = components[binary]
        expected = sbom._plain_markdown(declared[f'{binary}.exe sha-256']).lower()
        assert component['hashes'] == [{'alg': 'SHA-256', 'content': expected}]
        assert component['licenses']
        properties = _properties(component)
        assert 'creatoros:redistribution-status' in properties
        assert 'creatoros:license-declaration' in properties
    assert not [
        item for item in document.get('properties', [])
        if item.get('name') == 'creatoros:collection-error'
    ]


def test_artifact_profile_binds_the_installer_and_keeps_the_full_inventory(sbom, tmp_path):
    installer = tmp_path / 'CreatorOS-Setup-test.exe'
    installer.write_bytes(b'installer bytes')
    document = sbom.generate_sbom(
        REPO_ROOT,
        tmp_path / 'artifact.cdx.json',
        profile='artifact',
        artifact=installer,
        timestamp='2026-11-26T00:00:00Z',
    )
    digest = hashlib.sha256(b'installer bytes').hexdigest()
    hashes = document['metadata']['component']['hashes']
    assert {'alg': 'SHA-256', 'content': digest} in hashes
    names = {component['name'] for component in document['components']}
    assert 'creatoros-desktop' in names and 'creatoros-backend' in names


def test_an_unknown_profile_is_rejected(sbom, tmp_path):
    with pytest.raises(sbom.SbomError, match='unknown SBOM profile'):
        sbom.generate_sbom(REPO_ROOT, tmp_path / 'bad.cdx.json', profile='everything')

