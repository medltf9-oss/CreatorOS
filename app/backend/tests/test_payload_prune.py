from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    path = REPO_ROOT / 'scripts' / 'payload_prune.py'
    spec = importlib.util.spec_from_file_location('payload_prune', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _dist(site: Path, name: str, files: list[str]) -> None:
    root = site / f'{name}-1.0.dist-info'
    root.mkdir(parents=True)
    (root / 'METADATA').write_text(f'Name: {name}\nVersion: 1.0\n', encoding='utf-8')
    rows = []
    for relative in files:
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative, encoding='utf-8')
        rows.append(f'{relative},,')
    (root / 'RECORD').write_text('\n'.join(rows) + '\n', encoding='utf-8')


def test_prune_removes_distributions_outside_the_lock(tmp_path):
    module = _load_module()
    payload = tmp_path / 'payload'
    site = payload / 'Lib' / 'site-packages'
    site.mkdir(parents=True)
    _dist(site, 'keep', ['keep.py'])
    _dist(site, 'drop', ['drop.py'])
    lock = tmp_path / 'lock.txt'
    lock.write_text('keep==1.0\n', encoding='utf-8')

    assert module.main(['--payload', str(payload), '--lock', str(lock)]) == 0
    assert (site / 'keep.py').is_file()
    assert not (site / 'drop.py').exists()
    assert not (site / 'drop-1.0.dist-info').exists()


def test_prune_fails_when_an_unowned_file_remains(tmp_path):
    module = _load_module()
    payload = tmp_path / 'payload'
    site = payload / 'Lib' / 'site-packages'
    site.mkdir(parents=True)
    _dist(site, 'keep', ['keep.py'])
    (site / 'orphan.py').write_text('orphan', encoding='utf-8')
    lock = tmp_path / 'lock.txt'
    lock.write_text('keep==1.0\n', encoding='utf-8')

    assert module.main(['--payload', str(payload), '--lock', str(lock)]) == 1


def test_prune_refuses_a_malformed_lock(tmp_path):
    module = _load_module()
    payload = tmp_path / 'payload'
    (payload / 'Lib' / 'site-packages').mkdir(parents=True)
    lock = tmp_path / 'lock.txt'
    lock.write_text('not a pin\n', encoding='utf-8')

    with pytest.raises(SystemExit):
        module.main(['--payload', str(payload), '--lock', str(lock)])